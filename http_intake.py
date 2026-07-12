from __future__ import annotations

import asyncio
import mimetypes
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Any

from telegram.ext import Application

from image_summary import ImageSummaryConfig, log, split_message
from memory_processor import save_memory
from metrics import (
    HTTP_MEMORY_POSTS_TOTAL,
    MetricsConfig,
    metrics_response,
    observe_handler_error,
    observe_http_auth_failure,
    observe_http_request,
    observe_owntracks_ui_render,
)
from owntracks.digest import (
    generate_activity_dashboard,
    generate_hosted_map,
    generate_sample_heatmap,
    generate_search_aliases,
    generate_stop_index,
    generate_trips,
)
from owntracks.env import load_env
from owntracks.place_resolver import DEFAULT_OVERPASS_ENDPOINT, resolve_overpass
from owntracks.tagger import load_user_tags, save_user_tags
from spending_index import SpendingConfig, index_result_dict, index_scope, query_spending, recent_events


_OWNTRACKS_TAGS_LOCK = threading.Lock()


@dataclass(frozen=True)
class HttpIntakeConfig:
    enabled: bool
    host: str
    port: int
    token: str
    notify_telegram: bool
    fuel_csv_path: str
    owntracks_derived_dir: str
    owntracks_user_tags_path: str
    owntracks_media_dir: str


def build_http_config(env: dict[str, str]) -> HttpIntakeConfig:
    return HttpIntakeConfig(
        enabled=env.get("HTTP_INTAKE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        host=env.get("HTTP_INTAKE_HOST", "127.0.0.1"),
        port=int(env.get("HTTP_INTAKE_PORT", "8787")),
        token=env.get("HTTP_INTAKE_TOKEN", ""),
        notify_telegram=env.get("HTTP_INTAKE_NOTIFY_TELEGRAM", "true").strip().lower()
        in {"1", "true", "yes", "on"},
        fuel_csv_path=env.get("FUEL_CSV_PATH", "./data/fuel/fuel.csv"),
        owntracks_derived_dir=env.get("OWNTRACKS_DERIVED_DIR", "./data/owntracks/derived"),
        owntracks_user_tags_path=env.get("OWNTRACKS_USER_TAGS_PATH", "./data/owntracks/user_tags.json"),
        owntracks_media_dir=env.get("OWNTRACKS_MEDIA_DIR", "./data/owntracks/media"),
    )


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def write_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def authorized(handler: BaseHTTPRequestHandler, token: str) -> bool:
    if not token:
        return True
    supplied = handler.headers.get("X-Intake-Token", "")
    auth = handler.headers.get("Authorization", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    query_token = parse_qs(urlparse(handler.path).query).get("token", [""])[0]
    return supplied == token or bearer == token or query_token == token


def project_relative_path(raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else Path(__file__).resolve().parent / path


def content_type_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def safe_media_name(value: str) -> str:
    stem = Path(value or "upload").stem.lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip(".-")
    return stem[:80] or "upload"


def media_extension(filename: str, content_type: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    guessed = mimetypes.guess_extension(content_type or "")
    return guessed if guessed and re.fullmatch(r"\.[a-z0-9]{1,8}", guessed) else ".bin"


def read_multipart_form(handler: BaseHTTPRequestHandler, max_bytes: int = 30 * 1024 * 1024) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        raise ValueError("missing multipart body")
    if length > max_bytes:
        raise ValueError("upload too large")
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("Content-Type must be multipart/form-data")
    raw = handler.rfile.read(length)
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=email_policy).parsebytes(header + raw)
    fields: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is None:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        else:
            fields[name] = {
                "filename": filename,
                "content_type": part.get_content_type() or "application/octet-stream",
                "body": payload,
            }
    return fields


def stop_media_from_review(review: dict) -> list[dict]:
    media = review.setdefault("media", [])
    if not isinstance(media, list):
        media = []
        review["media"] = media
    return media


async def send_telegram_confirmation(
    application: Application,
    cfg: ImageSummaryConfig,
    saved_path: str,
    content: str,
) -> None:
    text = f"Saved memory from HTTP intake: `{saved_path}`\n\n{content}"
    for chunk in split_message(text, cfg.max_reply_chars):
        await application.bot.send_message(
            chat_id=cfg.chat_id,
            message_thread_id=cfg.topic_id,
            text=chunk,
        )


def make_handler(
    application: Application,
    cfg: ImageSummaryConfig,
    http_cfg: HttpIntakeConfig,
    metrics_cfg: MetricsConfig,
    spending_cfg: SpendingConfig,
    loop: asyncio.AbstractEventLoop,
) -> type[BaseHTTPRequestHandler]:
    class IntakeHandler(BaseHTTPRequestHandler):
        server_version = "TelegramControlIntake/0.1"

        def send_response(self, code: int, message: str | None = None) -> None:
            self._metrics_status = code
            super().send_response(code, message)

        def do_GET(self) -> None:
            start = time.monotonic()
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/metrics":
                    if not metrics_cfg.enabled:
                        write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                        return
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    status, content_type, body = metrics_response()
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/health":
                    write_json(self, HTTPStatus.OK, {"ok": True})
                    return
                if parsed.path == "/fuel.csv":
                    path = Path(http_cfg.fuel_csv_path)
                    if not path.exists():
                        self.send_response(HTTPStatus.NOT_FOUND.value)
                        self.end_headers()
                        return
                    body = path.read_bytes()
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="fuel.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path.startswith("/owntracks/media/"):
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    relative = unquote(parsed.path.removeprefix("/owntracks/media/"))
                    parts = [part for part in relative.split("/") if part]
                    if len(parts) != 2 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]) or "/" in parts[1] or "\\" in parts[1]:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid media path"})
                        return
                    media_root = project_relative_path(http_cfg.owntracks_media_dir).resolve()
                    path = (media_root / parts[0] / parts[1]).resolve()
                    if media_root not in path.parents or not path.exists() or not path.is_file():
                        write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                        return
                    body = path.read_bytes()
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", content_type_for(path))
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path in {"/owntracks/sample", "/owntracks/sample.html"}:
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    render_start = time.monotonic()
                    try:
                        _summary, html = generate_sample_heatmap()
                    except Exception as exc:
                        observe_owntracks_ui_render("sample", render_start, "error")
                        observe_handler_error("http_owntracks_sample", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
                    observe_owntracks_ui_render("sample", render_start, "success")
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/spending/events":
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        limit = int(parse_qs(parsed.query).get("limit", ["50"])[0])
                        write_json(self, HTTPStatus.OK, {"ok": True, "events": recent_events(spending_cfg, limit)})
                    except Exception as exc:
                        observe_handler_error("http_spending_events", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                if parsed.path in {"/owntracks/stops", "/owntracks/stops.html"}:
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        query = parse_qs(parsed.query)
                        start_text = (query.get("start") or [""])[0].strip()
                        end_text = (query.get("end") or [""])[0].strip()
                        for label, value in (("start", start_text), ("end", end_text)):
                            if value and not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
                                raise ValueError(f"invalid {label} date")
                        render_start = time.monotonic()
                        _summary, html = generate_stop_index(start_text or None, end_text or None)
                    except ValueError as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                        return
                    except Exception as exc:
                        observe_owntracks_ui_render("stops", render_start, "error")
                        observe_handler_error("http_owntracks_stop_index", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
                    observe_owntracks_ui_render("stops", render_start, "success")
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path in {"/owntracks/trips", "/owntracks/trips.html"}:
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        query = parse_qs(parsed.query)
                        date_text = (query.get("date") or [""])[0].strip()
                        origin_key = (query.get("from") or [""])[0].strip()
                        destination_key = (query.get("to") or [""])[0].strip()
                        if date_text and not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}|today|yesterday|\d{1,2}|\d{1,2}-\d{1,2}", date_text):
                            raise ValueError("invalid date")
                        render_start = time.monotonic()
                        _summary, html = generate_trips(date_text or None, origin_key or None, destination_key or None)
                    except ValueError as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                        return
                    except Exception as exc:
                        observe_owntracks_ui_render("trips", render_start, "error")
                        observe_handler_error("http_owntracks_trips", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
                    observe_owntracks_ui_render("trips", render_start, "success")
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path in {"/owntracks/dashboard", "/owntracks/dashboard.html"}:
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        query = parse_qs(parsed.query)
                        start_text = (query.get("start") or [""])[0].strip()
                        end_text = (query.get("end") or [""])[0].strip()
                        for label, value in (("start", start_text), ("end", end_text)):
                            if value and not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
                                raise ValueError(f"invalid {label} date")
                        render_start = time.monotonic()
                        _summary, html = generate_activity_dashboard(start_text or None, end_text or None)
                    except ValueError as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                        return
                    except Exception as exc:
                        observe_owntracks_ui_render("dashboard", render_start, "error")
                        observe_handler_error("http_owntracks_dashboard", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
                    observe_owntracks_ui_render("dashboard", render_start, "success")
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                match = parsed.path.removeprefix("/owntracks/map/").removesuffix(".html")
                if parsed.path.startswith("/owntracks/map/") and match:
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    if not re.fullmatch(r"\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?", match):
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid scope"})
                        return
                    try:
                        filter_text = (parse_qs(parsed.query).get("filter") or [""])[0].strip()
                        render_start = time.monotonic()
                        _plan, html = generate_hosted_map(match, filter_text=filter_text or None)
                    except Exception as exc:
                        observe_owntracks_ui_render("map", render_start, "error")
                        observe_handler_error("http_owntracks_map", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
                    observe_owntracks_ui_render("map", render_start, "success")
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            finally:
                observe_http_request("GET", self.path, getattr(self, "_metrics_status", 500), start)

        def do_POST(self) -> None:
            start = time.monotonic()
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/owntracks/search-aliases":
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        data = read_json(self)
                        start_text = str(data.get("start") or "").strip()
                        end_text = str(data.get("end") or "").strip()
                        for label, value in (("start", start_text), ("end", end_text)):
                            if value and not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
                                raise ValueError(f"invalid {label} date")
                        aliases, path = generate_search_aliases(start_text or None, end_text or None)
                        write_json(
                            self,
                            HTTPStatus.OK,
                            {
                                "ok": True,
                                "path": str(path),
                                "categories": len(aliases),
                                "terms": sum(len(terms) for terms in aliases.values()),
                            },
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    except Exception as exc:
                        observe_handler_error("http_owntracks_search_aliases", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                if parsed.path == "/spending/index":
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        data = read_json(self)
                        scope = str(data.get("scope") or "").strip() or None
                        result = index_scope(spending_cfg, cfg, scope)
                        write_json(self, HTTPStatus.OK, {"ok": True, **index_result_dict(result)})
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    except Exception as exc:
                        observe_handler_error("http_spending_index", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                if parsed.path == "/spending/query":
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        data = read_json(self)
                        question = str(data.get("question") or "").strip()
                        if not question:
                            raise ValueError("missing question")
                        answer = query_spending(spending_cfg, question)
                        write_json(self, HTTPStatus.OK, {"ok": True, "answer": answer})
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    except Exception as exc:
                        observe_handler_error("http_spending_query", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                if parsed.path == "/owntracks/media":
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        fields = read_multipart_form(self)
                        upload = fields.get("file")
                        if not isinstance(upload, dict) or not upload.get("body"):
                            raise ValueError("missing file")
                        date_text = str(fields.get("date") or "").strip()
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
                            raise ValueError("invalid date")
                        stop_id = str(fields.get("id") or "").strip()
                        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", stop_id):
                            raise ValueError("invalid stop id")
                        lat = float(fields.get("lat") or 0)
                        lon = float(fields.get("lon") or 0)
                        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                            raise ValueError("invalid coordinates")
                        caption = str(fields.get("caption") or "").strip()[:500]
                        original_name = str(upload.get("filename") or "upload")
                        content_type = str(upload.get("content_type") or "application/octet-stream")
                        body = bytes(upload.get("body") or b"")
                        media_id = f"{date_text.replace('-', '')}-{int(time.time())}-{secrets.token_hex(4)}"
                        filename = f"{media_id}-{safe_media_name(original_name)}{media_extension(original_name, content_type)}"
                        media_root = project_relative_path(http_cfg.owntracks_media_dir)
                        media_dir = media_root / date_text
                        media_dir.mkdir(parents=True, exist_ok=True)
                        media_path = media_dir / filename
                        media_path.write_bytes(body)
                        media = {
                            "id": media_id,
                            "kind": "image" if content_type.startswith("image/") else "file",
                            "filename": filename,
                            "original_name": original_name[:200],
                            "content_type": content_type[:120],
                            "size": len(body),
                            "caption": caption,
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        }
                        tags_path = project_relative_path(http_cfg.owntracks_user_tags_path)
                        with _OWNTRACKS_TAGS_LOCK:
                            tags = load_user_tags(tags_path)
                            stop_review = tags.setdefault(date_text, {}).setdefault("stops", {}).setdefault(stop_id, {})
                            stop_review.setdefault("lat", round(lat, 6))
                            stop_review.setdefault("lon", round(lon, 6))
                            stop_media_from_review(stop_review).append(media)
                            save_user_tags(tags_path, tags)
                        write_json(self, HTTPStatus.CREATED, {"ok": True, "media": media})
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    except Exception as exc:
                        observe_handler_error("http_owntracks_media", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                if parsed.path == "/owntracks/stops":
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        data = read_json(self)
                        date_text = str(data.get("date") or "")
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
                            raise ValueError("invalid date")
                        lat = float(data.get("lat"))
                        lon = float(data.get("lon"))
                        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                            raise ValueError("invalid coordinates")
                        requested_stop_id = str(data.get("id") or "").strip()
                        if requested_stop_id:
                            if not re.fullmatch(r"[A-Za-z0-9_.:-]+", requested_stop_id):
                                raise ValueError("invalid stop id")
                            ignored = bool(data.get("ignored"))
                            tags_path = project_relative_path(http_cfg.owntracks_user_tags_path)
                            if ignored:
                                saved_review = {
                                    "lat": round(lat, 6),
                                    "lon": round(lon, 6),
                                    "ignored": True,
                                    "note": str(data.get("note") or "").strip()[:2000],
                                }
                                with _OWNTRACKS_TAGS_LOCK:
                                    tags = load_user_tags(tags_path)
                                    existing = tags.setdefault(date_text, {}).setdefault("stops", {}).setdefault(requested_stop_id, {})
                                    existing.clear()
                                    existing.update(saved_review)
                                    save_user_tags(tags_path, tags)
                                write_json(self, HTTPStatus.OK, {"ok": True, "id": requested_stop_id, "ignored": True})
                                return
                            raw_tags = data.get("tags") or []
                            if not isinstance(raw_tags, list):
                                raise ValueError("tags must be a list")
                            saved_review = {
                                "lat": round(lat, 6),
                                "lon": round(lon, 6),
                                "tags": [str(tag).strip()[:100] for tag in raw_tags if str(tag).strip()][:50],
                                "note": str(data.get("note") or "").strip()[:2000],
                            }
                            name = str(data.get("name") or "").strip()
                            if name:
                                saved_review["name"] = name[:200]
                            entry_provided = "entry_time" in data or "entry" in data
                            exit_provided = "exit_time" in data or "exit" in data
                            radius_provided = "radius_m" in data
                            place_provided = "place" in data
                            entry_time = str(data.get("entry_time") or data.get("entry") or "").strip()
                            exit_time = str(data.get("exit_time") or data.get("exit") or "").strip()
                            if entry_time:
                                saved_review["entry_time"] = entry_time[:40]
                            if exit_time:
                                saved_review["exit_time"] = exit_time[:40]
                            if radius_provided and data.get("radius_m") not in (None, ""):
                                radius_m = float(data.get("radius_m"))
                                if not (10 <= radius_m <= 5000):
                                    raise ValueError("invalid radius")
                                saved_review["radius_m"] = round(radius_m)
                            place = bool(data.get("place"))
                            if place_provided and place:
                                saved_review["place"] = True
                            with _OWNTRACKS_TAGS_LOCK:
                                tags = load_user_tags(tags_path)
                                existing = tags.setdefault(date_text, {}).setdefault("stops", {}).setdefault(requested_stop_id, {})
                                if not name:
                                    existing.pop("name", None)
                                if entry_provided and not entry_time:
                                    existing.pop("entry_time", None)
                                if exit_provided and not exit_time:
                                    existing.pop("exit_time", None)
                                if radius_provided and data.get("radius_m") in (None, ""):
                                    existing.pop("radius_m", None)
                                if place_provided and not place:
                                    existing.pop("place", None)
                                existing.update(saved_review)
                                save_user_tags(tags_path, tags)
                            write_json(self, HTTPStatus.OK, {"ok": True, "id": requested_stop_id})
                            return
                        line = int(data.get("line"))
                        if line < 1:
                            raise ValueError("invalid line")
                        stop_id = f"manual-stop-{line}"
                        saved = {
                            "manual": True,
                            "lat": round(lat, 6),
                            "lon": round(lon, 6),
                            "line": line,
                            "timestamp": data.get("timestamp"),
                            "time": str(data.get("time") or ""),
                            "motion_mode": str(data.get("motion_mode") or "unknown"),
                        }
                        name = str(data.get("name") or "").strip()
                        if name:
                            saved["name"] = name[:200]
                        raw_tags = data.get("tags") or []
                        if not isinstance(raw_tags, list):
                            raise ValueError("tags must be a list")
                        saved_tags = [str(tag).strip()[:100] for tag in raw_tags if str(tag).strip()][:50]
                        if saved_tags:
                            saved["tags"] = saved_tags
                        note = str(data.get("note") or "").strip()
                        if note:
                            saved["note"] = note[:2000]
                        place = bool(data.get("place"))
                        if place:
                            saved["place"] = True
                        entry_time = str(data.get("entry_time") or data.get("entry") or "").strip()
                        exit_time = str(data.get("exit_time") or data.get("exit") or "").strip()
                        if entry_time:
                            saved["entry_time"] = entry_time[:40]
                        if exit_time:
                            saved["exit_time"] = exit_time[:40]
                        if data.get("radius_m") not in (None, ""):
                            radius_m = float(data.get("radius_m"))
                            if not (10 <= radius_m <= 5000):
                                raise ValueError("invalid radius")
                            saved["radius_m"] = round(radius_m)
                        tags_path = project_relative_path(http_cfg.owntracks_user_tags_path)
                        with _OWNTRACKS_TAGS_LOCK:
                            tags = load_user_tags(tags_path)
                            existing = tags.setdefault(date_text, {}).setdefault("stops", {}).setdefault(stop_id, {})
                            if not name:
                                existing.pop("name", None)
                            if not saved_tags:
                                existing.pop("tags", None)
                            if not note:
                                existing.pop("note", None)
                            if not place:
                                existing.pop("place", None)
                            if not entry_time:
                                existing.pop("entry_time", None)
                            if not exit_time:
                                existing.pop("exit_time", None)
                            if data.get("radius_m") in (None, ""):
                                existing.pop("radius_m", None)
                            existing.update(saved)
                            save_user_tags(tags_path, tags)
                        write_json(self, HTTPStatus.CREATED, {"ok": True, "id": stop_id})
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    except Exception as exc:
                        observe_handler_error("http_owntracks_manual_stop", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                if parsed.path == "/owntracks/resolve-place":
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        data = read_json(self)
                        lat = float(data.get("lat"))
                        lon = float(data.get("lon"))
                        radius_m = int(data.get("radius_m") or 120)
                        env = load_env()
                        if env.get("OWNTRACKS_PLACE_RESOLVER", "overpass").strip().lower() in {"0", "false", "off", "none", "disabled"}:
                            raise ValueError("place resolver is disabled")
                        endpoint = env.get("OWNTRACKS_OVERPASS_ENDPOINT", DEFAULT_OVERPASS_ENDPOINT)
                        timeout_seconds = int(env.get("OWNTRACKS_OVERPASS_TIMEOUT_SECONDS") or "25")
                        candidates = resolve_overpass(
                            lat,
                            lon,
                            radius_m=radius_m,
                            endpoint=endpoint,
                            timeout_seconds=timeout_seconds,
                        )
                        write_json(
                            self,
                            HTTPStatus.OK,
                            {
                                "ok": True,
                                "provider": "overpass",
                                "radius_m": max(20, min(radius_m, 1000)),
                                "candidates": candidates,
                            },
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    except Exception as exc:
                        observe_handler_error("http_owntracks_place_resolve", exc)
                        write_json(self, HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                    return
                if parsed.path != "/memory":
                    write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                    return
                if not authorized(self, http_cfg.token):
                    observe_http_auth_failure(self.path)
                    write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                    return

                try:
                    data = read_json(self)
                    text = str(data.get("text") or "").strip()
                    if not text:
                        HTTP_MEMORY_POSTS_TOTAL.labels(result="bad_request").inc()
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing text"})
                        return

                    saved = save_memory(
                        text,
                        cfg,
                        {
                            "source": data.get("source", "http_intake"),
                            "client": self.client_address[0],
                            "title": data.get("title"),
                        },
                    )
                    if http_cfg.notify_telegram:
                        future = asyncio.run_coroutine_threadsafe(
                            send_telegram_confirmation(application, cfg, str(saved.path), saved.content),
                            loop,
                        )
                        future.result(timeout=30)

                    HTTP_MEMORY_POSTS_TOTAL.labels(result="success").inc()
                    write_json(
                        self,
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "path": str(saved.path),
                            "content": saved.content,
                        },
                    )
                except Exception as exc:
                    HTTP_MEMORY_POSTS_TOTAL.labels(result="error").inc()
                    observe_handler_error("http_memory", exc)
                    log(cfg, f"http_intake_failed error={exc}")
                    write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            finally:
                observe_http_request("POST", self.path, getattr(self, "_metrics_status", 500), start)

        def do_DELETE(self) -> None:
            start = time.monotonic()
            try:
                parsed = urlparse(self.path)
                if parsed.path != "/owntracks/media":
                    write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                    return
                if not authorized(self, http_cfg.token):
                    observe_http_auth_failure(self.path)
                    write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                    return
                try:
                    data = read_json(self)
                    date_text = str(data.get("date") or "").strip()
                    stop_id = str(data.get("id") or "").strip()
                    media_id = str(data.get("media_id") or "").strip()
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
                        raise ValueError("invalid date")
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", stop_id):
                        raise ValueError("invalid stop id")
                    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", media_id):
                        raise ValueError("invalid media id")
                    tags_path = project_relative_path(http_cfg.owntracks_user_tags_path)
                    removed: dict | None = None
                    with _OWNTRACKS_TAGS_LOCK:
                        tags = load_user_tags(tags_path)
                        stop_review = tags.get(date_text, {}).get("stops", {}).get(stop_id, {})
                        media = stop_review.get("media") if isinstance(stop_review, dict) else []
                        media = media if isinstance(media, list) else []
                        remaining = []
                        for item in media:
                            if isinstance(item, dict) and str(item.get("id") or "") == media_id:
                                removed = item
                            else:
                                remaining.append(item)
                        if removed is None:
                            raise ValueError("media not found")
                        stop_review["media"] = remaining
                        save_user_tags(tags_path, tags)
                    filename = str(removed.get("filename") or "")
                    if filename and "/" not in filename and "\\" not in filename:
                        media_root = project_relative_path(http_cfg.owntracks_media_dir).resolve()
                        media_path = (media_root / date_text / filename).resolve()
                        if media_root in media_path.parents and media_path.exists():
                            media_path.unlink()
                    write_json(self, HTTPStatus.OK, {"ok": True, "media_id": media_id})
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    observe_handler_error("http_owntracks_media_delete", exc)
                    write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            finally:
                observe_http_request("DELETE", self.path, getattr(self, "_metrics_status", 500), start)

        def log_message(self, format: str, *args: Any) -> None:
            log(cfg, "http_intake " + format % args)

    return IntakeHandler


def start_http_intake(
    application: Application,
    cfg: ImageSummaryConfig,
    http_cfg: HttpIntakeConfig,
    metrics_cfg: MetricsConfig,
    spending_cfg: SpendingConfig,
    loop: asyncio.AbstractEventLoop,
) -> ThreadingHTTPServer | None:
    if not http_cfg.enabled:
        return None
    server = ThreadingHTTPServer(
        (http_cfg.host, http_cfg.port),
        make_handler(application, cfg, http_cfg, metrics_cfg, spending_cfg, loop),
    )
    thread = threading.Thread(target=server.serve_forever, name="http-intake", daemon=True)
    thread.start()
    log(cfg, f"http_intake_started host={http_cfg.host} port={http_cfg.port}")
    return server
