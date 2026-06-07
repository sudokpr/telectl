from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

from telegram.ext import Application

from image_summary import ImageSummaryConfig, log, split_message
from memory_processor import save_memory


@dataclass(frozen=True)
class HttpIntakeConfig:
    enabled: bool
    host: str
    port: int
    token: str
    notify_telegram: bool
    fuel_csv_path: str
    owntracks_derived_dir: str


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
    loop: asyncio.AbstractEventLoop,
) -> type[BaseHTTPRequestHandler]:
    class IntakeHandler(BaseHTTPRequestHandler):
        server_version = "TelegramControlIntake/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
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
            match = parsed.path.removeprefix("/owntracks/map/").removesuffix(".html")
            if parsed.path.startswith("/owntracks/map/") and match:
                if not authorized(self, http_cfg.token):
                    write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                    return
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", match):
                    write_json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid date"})
                    return
                path = project_relative_path(http_cfg.owntracks_derived_dir) / f"activity-map-{match}.html"
                if not path.exists():
                    write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "map not found"})
                    return
                body = path.read_bytes()
                self.send_response(HTTPStatus.OK.value)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/memory":
                write_json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                return
            if not authorized(self, http_cfg.token):
                write_json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return

            try:
                data = read_json(self)
                text = str(data.get("text") or "").strip()
                if not text:
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
                log(cfg, f"http_intake_failed error={exc}")
                write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:
            log(cfg, "http_intake " + format % args)

    return IntakeHandler


def start_http_intake(
    application: Application,
    cfg: ImageSummaryConfig,
    http_cfg: HttpIntakeConfig,
    loop: asyncio.AbstractEventLoop,
) -> ThreadingHTTPServer | None:
    if not http_cfg.enabled:
        return None
    server = ThreadingHTTPServer(
        (http_cfg.host, http_cfg.port),
        make_handler(application, cfg, http_cfg, loop),
    )
    thread = threading.Thread(target=server.serve_forever, name="http-intake", daemon=True)
    thread.start()
    log(cfg, f"http_intake_started host={http_cfg.host} port={http_cfg.port}")
    return server
