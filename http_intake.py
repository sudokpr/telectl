from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
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
)
from owntracks.digest import (
    generate_activity_dashboard,
    generate_hosted_map,
    generate_sample_heatmap,
    generate_search_aliases,
    generate_stop_index,
)
from owntracks.tagger import load_user_tags, save_user_tags


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
                if parsed.path in {"/owntracks/sample", "/owntracks/sample.html"}:
                    if not authorized(self, http_cfg.token):
                        observe_http_auth_failure(self.path)
                        write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                        return
                    try:
                        _summary, html = generate_sample_heatmap()
                    except Exception as exc:
                        observe_handler_error("http_owntracks_sample", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
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
                        _summary, html = generate_stop_index(start_text or None, end_text or None)
                    except ValueError as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                        return
                    except Exception as exc:
                        observe_handler_error("http_owntracks_stop_index", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
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
                        _summary, html = generate_activity_dashboard(start_text or None, end_text or None)
                    except ValueError as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                        return
                    except Exception as exc:
                        observe_handler_error("http_owntracks_dashboard", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
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
                        _plan, html = generate_hosted_map(match, filter_text=filter_text or None)
                    except Exception as exc:
                        observe_handler_error("http_owntracks_map", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                        return
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
                            tags_path = project_relative_path(http_cfg.owntracks_user_tags_path)
                            with _OWNTRACKS_TAGS_LOCK:
                                tags = load_user_tags(tags_path)
                                existing = tags.setdefault(date_text, {}).setdefault("stops", {}).setdefault(requested_stop_id, {})
                                if not name:
                                    existing.pop("name", None)
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
                        tags_path = project_relative_path(http_cfg.owntracks_user_tags_path)
                        with _OWNTRACKS_TAGS_LOCK:
                            tags = load_user_tags(tags_path)
                            existing = tags.setdefault(date_text, {}).setdefault("stops", {}).setdefault(stop_id, {})
                            existing.update(saved)
                            save_user_tags(tags_path, tags)
                        write_json(self, HTTPStatus.CREATED, {"ok": True, "id": stop_id})
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    except Exception as exc:
                        observe_handler_error("http_owntracks_manual_stop", exc)
                        write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
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

        def log_message(self, format: str, *args: Any) -> None:
            log(cfg, "http_intake " + format % args)

    return IntakeHandler


def start_http_intake(
    application: Application,
    cfg: ImageSummaryConfig,
    http_cfg: HttpIntakeConfig,
    metrics_cfg: MetricsConfig,
    loop: asyncio.AbstractEventLoop,
) -> ThreadingHTTPServer | None:
    if not http_cfg.enabled:
        return None
    server = ThreadingHTTPServer(
        (http_cfg.host, http_cfg.port),
        make_handler(application, cfg, http_cfg, metrics_cfg, loop),
    )
    thread = threading.Thread(target=server.serve_forever, name="http-intake", daemon=True)
    thread.start()
    log(cfg, f"http_intake_started host={http_cfg.host} port={http_cfg.port}")
    return server
