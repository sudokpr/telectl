from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


START_TIME = time.time()


class FeatureUsageCollector:
    """Expose durable usage totals from the local analytics database."""

    def __init__(self) -> None:
        self.analytics: Any = None

    def collect(self):
        enabled = GaugeMetricFamily(
            "telegram_control_feature_usage_enabled",
            "Whether durable local feature usage analytics are enabled.",
        )
        events = CounterMetricFamily(
            "telegram_control_feature_usage",
            "Durable lifetime feature interactions observed since tracking began.",
            labels=["feature", "surface", "category"],
        )
        last_used = GaugeMetricFamily(
            "telegram_control_feature_last_used_timestamp_seconds",
            "Unix timestamp of the most recent observed interaction; zero means never observed.",
            labels=["feature", "surface", "category"],
        )
        tracking_started = GaugeMetricFamily(
            "telegram_control_feature_usage_tracking_started_timestamp_seconds",
            "Unix timestamp when durable feature usage tracking began.",
        )
        if self.analytics is None:
            enabled.add_metric([], 0)
            tracking_started.add_metric([], 0)
        else:
            try:
                snapshot = self.analytics.prometheus_snapshot()
                enabled.add_metric([], 1 if snapshot.get("enabled") else 0)
                started = snapshot.get("tracking_started_at")
                tracking_started.add_metric([], datetime.fromisoformat(started).timestamp() if started else 0)
                for item in snapshot.get("features") or []:
                    labels = [str(item["feature"]), str(item["surface"]), str(item["category"])]
                    events.add_metric(labels, int(item.get("count") or 0))
                    last = item.get("last_used")
                    last_used.add_metric(labels, datetime.fromisoformat(last).timestamp() if last else 0)
            except Exception:
                enabled.add_metric([], 0)
                tracking_started.add_metric([], 0)
        yield enabled
        yield events
        yield last_used
        yield tracking_started


FEATURE_USAGE_COLLECTOR = FeatureUsageCollector()
REGISTRY.register(FEATURE_USAGE_COLLECTOR)


def set_usage_analytics(analytics: Any) -> None:
    FEATURE_USAGE_COLLECTOR.analytics = analytics


@dataclass(frozen=True)
class MetricsConfig:
    enabled: bool
    host: str
    port: int


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_metrics_config(env: dict[str, str]) -> MetricsConfig:
    return MetricsConfig(
        enabled=env_bool(env.get("PROMETHEUS_METRICS_ENABLED"), False),
        host=env.get("PROMETHEUS_METRICS_HOST", "127.0.0.1"),
        port=int(env.get("PROMETHEUS_METRICS_PORT", "8788")),
    )


INFO = Gauge(
    "telegram_control_build_info",
    "Static build information for the telegram-control process.",
    ["version"],
)
UPTIME_SECONDS = Gauge(
    "telegram_control_uptime_seconds",
    "Process uptime in seconds.",
)
CONFIG_ENABLED = Gauge(
    "telegram_control_config_enabled",
    "Whether a feature is enabled in configuration.",
    ["feature"],
)

UPDATES_TOTAL = Counter(
    "telegram_control_updates_total",
    "Telegram updates observed by type.",
    ["type"],
)
HANDLER_DURATION_SECONDS = Histogram(
    "telegram_control_handler_duration_seconds",
    "Feature handler duration in seconds.",
    ["handler", "result"],
)
HANDLER_ERRORS_TOTAL = Counter(
    "telegram_control_handler_errors_total",
    "Feature handler errors by handler and exception type.",
    ["handler", "error_type"],
)
REPLIES_TOTAL = Counter(
    "telegram_control_replies_total",
    "Telegram replies sent by kind.",
    ["kind"],
)
DOWNLOAD_DURATION_SECONDS = Histogram(
    "telegram_control_download_duration_seconds",
    "Telegram file download duration in seconds.",
    ["kind", "result"],
)
DOWNLOAD_ERRORS_TOTAL = Counter(
    "telegram_control_download_errors_total",
    "Telegram file download errors by kind and exception type.",
    ["kind", "error_type"],
)

IMAGE_JOBS_TOTAL = Counter(
    "telegram_control_image_jobs_total",
    "Image summary jobs by mode, label, and result.",
    ["mode", "label", "result"],
)
IMAGE_JOB_DURATION_SECONDS = Histogram(
    "telegram_control_image_job_duration_seconds",
    "Image summary job duration in seconds.",
    ["mode", "label"],
)
IMAGE_REPLY_CHUNKS_TOTAL = Counter(
    "telegram_control_image_reply_chunks_total",
    "Reply chunks emitted for image summary results.",
    ["mode"],
)

OLLAMA_REQUESTS_TOTAL = Counter(
    "telegram_control_ollama_requests_total",
    "Ollama requests by purpose, model, and result.",
    ["purpose", "model", "result"],
)
OLLAMA_REQUEST_DURATION_SECONDS = Histogram(
    "telegram_control_ollama_request_duration_seconds",
    "Ollama request duration in seconds.",
    ["purpose", "model"],
)
OCR_CHARS_TOTAL = Counter(
    "telegram_control_ocr_chars_total",
    "Characters extracted by OCR.",
)

MEMORY_EXTRACTIONS_TOTAL = Counter(
    "telegram_control_memory_extractions_total",
    "Memory extraction attempts by result.",
    ["result"],
)
MEMORY_EXTRACTION_DURATION_SECONDS = Histogram(
    "telegram_control_memory_extraction_duration_seconds",
    "Memory extraction duration in seconds.",
    ["result"],
)
MEMORY_QUERIES_TOTAL = Counter(
    "telegram_control_memory_queries_total",
    "Memory query attempts by result.",
    ["result"],
)
MEMORY_QUERY_DURATION_SECONDS = Histogram(
    "telegram_control_memory_query_duration_seconds",
    "Memory query duration in seconds.",
    ["model", "result"],
)
MEMORY_FILES_TOTAL = Gauge(
    "telegram_control_memory_files_total",
    "Number of saved markdown memory files.",
)
MEMORY_QUERY_CONTEXT_CHARS = Histogram(
    "telegram_control_memory_query_context_chars",
    "Approximate memory query context size in characters.",
)

FUEL_IMAGES_TOTAL = Counter(
    "telegram_control_fuel_images_total",
    "Fuel images received by result.",
    ["result"],
)
FUEL_EXTRACTIONS_TOTAL = Counter(
    "telegram_control_fuel_extractions_total",
    "Fuel extraction attempts by result.",
    ["result"],
)
FUEL_EXTRACTION_DURATION_SECONDS = Histogram(
    "telegram_control_fuel_extraction_duration_seconds",
    "Fuel extraction duration in seconds.",
    ["result"],
)
FUEL_APPROVALS_TOTAL = Counter(
    "telegram_control_fuel_approvals_total",
    "Fuel approval actions.",
    ["action"],
)
FUEL_PENDING_APPROVALS = Gauge(
    "telegram_control_fuel_pending_approvals",
    "Pending fuel approvals waiting for user action.",
)
FUEL_CSV_APPENDS_TOTAL = Counter(
    "telegram_control_fuel_csv_appends_total",
    "Fuel CSV append attempts by result.",
    ["result"],
)
FUEL_CORRECTIONS_TOTAL = Counter(
    "telegram_control_fuel_corrections_total",
    "Fuel correction attempts by result.",
    ["result"],
)

OWNTRACKS_DIGEST_TOTAL = Counter(
    "telegram_control_owntracks_digest_total",
    "OwnTracks digest generation attempts by scope and result.",
    ["scope", "result"],
)
OWNTRACKS_DIGEST_DURATION_SECONDS = Histogram(
    "telegram_control_owntracks_digest_duration_seconds",
    "OwnTracks digest generation duration in seconds.",
    ["scope", "result"],
)
OWNTRACKS_MAP_TOTAL = Counter(
    "telegram_control_owntracks_map_total",
    "OwnTracks map generation or delivery attempts by scope, delivery, and result.",
    ["scope", "delivery", "result"],
)
OWNTRACKS_MAP_DURATION_SECONDS = Histogram(
    "telegram_control_owntracks_map_duration_seconds",
    "OwnTracks map generation or delivery duration in seconds.",
    ["scope", "delivery", "result"],
)
OWNTRACKS_MAP_TILE_FETCHES_TOTAL = Counter(
    "telegram_control_owntracks_map_tile_fetches_total",
    "OwnTracks embedded map tile fetches by result.",
    ["result"],
)
OWNTRACKS_STOP_REVIEWS_TOTAL = Counter(
    "telegram_control_owntracks_stop_reviews_total",
    "OwnTracks stop review write attempts by action and result.",
    ["action", "result"],
)
OWNTRACKS_LOG_EVENTS_PROCESSED_TOTAL = Counter(
    "telegram_control_owntracks_log_events_processed_total",
    "OwnTracks log events processed while building digests or maps.",
)
OWNTRACKS_CANDIDATE_STOPS = Histogram(
    "telegram_control_owntracks_candidate_stops",
    "Candidate stop count in OwnTracks plans.",
)
OWNTRACKS_RIDE_SEGMENTS = Histogram(
    "telegram_control_owntracks_ride_segments",
    "Ride segment count in OwnTracks plans.",
)
OWNTRACKS_UI_RENDER_TOTAL = Counter(
    "telegram_control_owntracks_ui_render_total",
    "OwnTracks hosted UI render attempts by view and result.",
    ["view", "result"],
)
OWNTRACKS_UI_RENDER_DURATION_SECONDS = Histogram(
    "telegram_control_owntracks_ui_render_duration_seconds",
    "OwnTracks hosted UI server-side render duration in seconds.",
    ["view", "result"],
)

HTTP_REQUESTS_TOTAL = Counter(
    "telegram_control_http_requests_total",
    "HTTP requests by route, method, and status.",
    ["route", "method", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "telegram_control_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["route", "method"],
)
HTTP_AUTH_FAILURES_TOTAL = Counter(
    "telegram_control_http_auth_failures_total",
    "HTTP auth failures by route.",
    ["route"],
)
HTTP_MEMORY_POSTS_TOTAL = Counter(
    "telegram_control_http_memory_posts_total",
    "HTTP memory intake posts by result.",
    ["result"],
)


def result_label(ok: bool) -> str:
    return "success" if ok else "error"


def error_type(exc: BaseException) -> str:
    return type(exc).__name__


def update_type(update: object) -> str:
    for name in (
        "message",
        "edited_message",
        "callback_query",
        "channel_post",
        "edited_channel_post",
        "inline_query",
        "chosen_inline_result",
        "poll",
        "poll_answer",
        "my_chat_member",
        "chat_member",
    ):
        if getattr(update, name, None) is not None:
            return name
    return "unknown"


def observe_update(update: object) -> None:
    UPDATES_TOTAL.labels(type=update_type(update)).inc()


def observe_handler(handler: str, start: float, result: str) -> None:
    HANDLER_DURATION_SECONDS.labels(handler=handler, result=result).observe(time.monotonic() - start)


def observe_handler_error(handler: str, exc: BaseException) -> None:
    HANDLER_ERRORS_TOTAL.labels(handler=handler, error_type=error_type(exc)).inc()


def observe_download(kind: str, start: float, result: str, exc: BaseException | None = None) -> None:
    DOWNLOAD_DURATION_SECONDS.labels(kind=kind, result=result).observe(time.monotonic() - start)
    if exc is not None:
        DOWNLOAD_ERRORS_TOTAL.labels(kind=kind, error_type=error_type(exc)).inc()


def observe_plan(plan: dict) -> None:
    stats = plan.get("stats") if isinstance(plan.get("stats"), dict) else {}
    OWNTRACKS_LOG_EVENTS_PROCESSED_TOTAL.inc(float(stats.get("events_in_window") or stats.get("events_on_day") or 0))
    OWNTRACKS_CANDIDATE_STOPS.observe(len(plan.get("candidate_stops") or []))
    OWNTRACKS_RIDE_SEGMENTS.observe(len(plan.get("ride_segments") or []))


def observe_owntracks_ui_render(view: str, start: float, result: str) -> None:
    OWNTRACKS_UI_RENDER_TOTAL.labels(view=view, result=result).inc()
    OWNTRACKS_UI_RENDER_DURATION_SECONDS.labels(view=view, result=result).observe(time.monotonic() - start)


def set_config_enabled(*, image_summary: bool, memory: bool, fuel: bool, http_intake: bool, owntracks: bool) -> None:
    INFO.labels(version="0.1.0").set(1)
    UPTIME_SECONDS.set_function(lambda: max(0.0, time.time() - START_TIME))
    values = {
        "image_summary": image_summary,
        "memory": memory,
        "fuel": fuel,
        "http_intake": http_intake,
        "owntracks": owntracks,
    }
    for feature, enabled in values.items():
        CONFIG_ENABLED.labels(feature=feature).set(1 if enabled else 0)


def set_memory_files_gauge(memory_dir: Path) -> None:
    def count_files() -> float:
        try:
            return float(sum(1 for _path in memory_dir.glob("*.md")))
        except OSError:
            return 0.0

    MEMORY_FILES_TOTAL.set_function(count_files)


def http_route(path: str) -> str:
    parsed = urlparse(path)
    clean = parsed.path
    if clean == "/metrics":
        return "/metrics"
    if clean == "/health":
        return "/health"
    if clean == "/memory":
        return "/memory"
    if clean in {"/usage", "/usage.html", "/usage.json"}:
        return "/usage"
    if clean == "/usage/events":
        return "/usage/events"
    if clean == "/fuel.csv":
        return "/fuel.csv"
    if clean in {"/owntracks/sample", "/owntracks/sample.html"}:
        return "/owntracks/sample"
    if clean in {"/owntracks/stops", "/owntracks/stops.html"}:
        return "/owntracks/stops"
    if clean in {"/owntracks/trips", "/owntracks/trips.html"}:
        return "/owntracks/trips"
    if clean in {"/owntracks/dashboard", "/owntracks/dashboard.html"}:
        return "/owntracks/dashboard"
    if clean == "/owntracks/search-aliases":
        return "/owntracks/search-aliases"
    if clean == "/owntracks/media":
        return "/owntracks/media"
    if clean.startswith("/owntracks/media/"):
        return "/owntracks/media/:file"
    if clean.startswith("/owntracks/map/"):
        return "/owntracks/map/:scope"
    return "not_found"


def observe_http_request(method: str, path: str, status: int, start: float) -> None:
    route = http_route(path)
    HTTP_REQUESTS_TOTAL.labels(route=route, method=method, status=str(status)).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(route=route, method=method).observe(time.monotonic() - start)


def observe_http_auth_failure(path: str) -> None:
    HTTP_AUTH_FAILURES_TOTAL.labels(route=http_route(path)).inc()


def metrics_response() -> tuple[int, str, bytes]:
    return HTTPStatus.OK.value, CONTENT_TYPE_LATEST, generate_latest()


def start_standalone_metrics_server(config: MetricsConfig) -> None:
    if config.enabled:
        start_http_server(config.port, addr=config.host)
