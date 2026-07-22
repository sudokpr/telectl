from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from image_summary import ImageSummaryConfig, log
from memory_processor import slugify
from owntracks.tagger import Event, build_plan, event_time, haversine_km, load_user_tags, parse_log, poi_context


IST = ZoneInfo("Asia/Kolkata")
CURRENCY_RE = re.compile(r"(?:\brs\.?\s*\d|\binr\s*\d|₹\s*\d)", re.IGNORECASE)
CAPTURE_ID_RE = re.compile(r"\bcapture[_ -]?id\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._:-]{5,127})", re.IGNORECASE)


@dataclass(frozen=True)
class PoiMemorySyncResult:
    scanned: int
    created: int
    updated: int
    unchanged: int
    errors: int


def resolved_poi_event(event: Event) -> Event:
    """Return a POI event whose time and position reflect structured capture data."""
    context = poi_context(event)
    payload = dict(event.payload)
    payload["poi"] = context["text"]
    payload["tst"] = int(context["recorded_at"].timestamp())
    if context["lat"] is not None and context["lon"] is not None:
        payload["lat"] = context["lat"]
        payload["lon"] = context["lon"]
    return Event(event.line_no, event.received_at, event.topic, payload, event.local_tz)


def event_matches_scope(event: Event, scope: str | None) -> bool:
    if not scope:
        return True
    day = event_time(event).date().isoformat()
    clean = scope.strip().lower()
    today = dt.datetime.now(IST).date()
    if clean == "today":
        return day == today.isoformat()
    if clean == "yesterday":
        return day == (today - dt.timedelta(days=1)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean):
        return day == clean
    if re.fullmatch(r"\d{4}-\d{2}", clean):
        return day.startswith(clean + "-")
    if re.fullmatch(r"\d{4}", clean):
        return day.startswith(clean + "-")
    return True


def stop_label(stop: dict[str, Any]) -> str:
    return str(stop.get("reviewed_name") or stop.get("name") or stop.get("alias") or stop.get("id") or "").strip()


def poi_location(
    event: Event,
    events: list[Event],
    user_tags: dict,
    plan_cache: dict[str, dict],
    nearest_radius_m: int,
    nearest_time_window_minutes: int,
) -> dict[str, Any]:
    current_time = event_time(event)
    map_date = current_time.date().isoformat()
    maps_url = (
        f"https://www.google.com/maps?q={event.lat:.6f},{event.lon:.6f}"
        if event.lat is not None and event.lon is not None
        else None
    )
    if event.lat is None or event.lon is None:
        return {"label": None, "maps_url": None, "map_date": map_date, "distance_m": None}

    plan = plan_cache.get(map_date)
    if plan is None:
        plan, _track_points = build_plan(events, current_time.date(), user_tags)
        plan_cache[map_date] = plan
    timestamp = int(current_time.timestamp())
    for stop in plan.get("candidate_stops", []):
        start_ts = stop.get("visit_start_timestamp", stop.get("start_timestamp"))
        end_ts = stop.get("visit_end_timestamp", stop.get("end_timestamp"))
        if not isinstance(start_ts, int) or not isinstance(end_ts, int) or not (start_ts <= timestamp <= end_ts):
            continue
        label = stop_label(stop)
        if label:
            distance_m = None
            if stop.get("lat") is not None and stop.get("lon") is not None:
                distance_m = round(haversine_km(event.lat, event.lon, float(stop["lat"]), float(stop["lon"])) * 1000, 1)
            return {"label": label, "maps_url": maps_url, "map_date": map_date, "distance_m": distance_m}

    for segment in plan.get("travel_segments", []):
        start_ts = segment.get("start_timestamp")
        end_ts = segment.get("end_timestamp")
        label = str(segment.get("label") or "").strip()
        if isinstance(start_ts, int) and isinstance(end_ts, int) and start_ts <= timestamp <= end_ts and label:
            return {"label": f"en route: {label}", "maps_url": maps_url, "map_date": map_date, "distance_m": None}

    best: tuple[float, float, str] | None = None
    for candidate in events:
        if not candidate.is_location or candidate.line_no == event.line_no or candidate.lat is None or candidate.lon is None:
            continue
        label = str(candidate.payload.get("desc") or "").strip()
        regions = [str(item) for item in candidate.payload.get("inregions") or [] if str(item).strip()]
        if not label and regions:
            label = regions[0]
        if not label:
            continue
        delta = abs((event_time(candidate) - current_time).total_seconds())
        if delta > nearest_time_window_minutes * 60:
            continue
        distance_m = haversine_km(event.lat, event.lon, candidate.lat, candidate.lon) * 1000
        if distance_m > nearest_radius_m:
            continue
        score = distance_m + delta / 60
        if best is None or score < best[0]:
            best = (score, distance_m, label)
    if best:
        return {"label": best[2], "maps_url": maps_url, "map_date": map_date, "distance_m": round(best[1], 1)}
    return {"label": "recorded POI coordinates", "maps_url": maps_url, "map_date": map_date, "distance_m": 0}


def capture_id(raw_poi: str) -> str | None:
    if raw_poi.strip().startswith("{"):
        try:
            decoded = json.loads(raw_poi)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            value = str(decoded.get("capture_id") or decoded.get("captureId") or "").strip()
            if value:
                return value.lower()
    match = CAPTURE_ID_RE.search(raw_poi)
    return match.group(1).lower() if match else None


def poi_memory_markdown(event: Event, raw_poi: str, location: dict[str, Any]) -> str:
    text = str(event.payload.get("poi") or "").strip()
    recorded_at = event_time(event)
    title = next((line.strip() for line in text.splitlines() if line.strip()), "OwnTracks POI")[:100]
    tags = ["poi", "owntracks"]
    if str(event.payload.get("image") or "").strip():
        tags.append("image")
    if CURRENCY_RE.search(text):
        tags.append("spending")
    source = {
        "source": "owntracks_poi",
        "owntracks_line": event.line_no,
        "topic": event.topic,
        "capture_id": capture_id(raw_poi),
        "image_name": str(event.payload.get("imagename") or "").strip() or None,
        "has_image": bool(str(event.payload.get("image") or "").strip()),
    }
    source = {key: value for key, value in source.items() if value is not None}
    place = str(location.get("label") or "").strip()
    summary = f"OwnTracks POI recorded on {recorded_at.strftime('%Y-%m-%d at %H:%M:%S')}"
    if place:
        summary += f" at {place}"
    summary += f". {text}"
    fields = {
        "date": recorded_at.date().isoformat(),
        "time": recorded_at.isoformat(timespec="seconds"),
        "place": place or None,
        "latitude": event.lat,
        "longitude": event.lon,
        "map": location.get("maps_url"),
        "owntracks_line": event.line_no,
        "capture_id": source.get("capture_id"),
    }

    lines = [
        "---",
        'category: "poi"',
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        f"source: {json.dumps(source, ensure_ascii=False)}",
        f"created_at: {json.dumps(recorded_at.isoformat(timespec='seconds'))}",
        "---",
        "",
        f"# {title}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Key Fields",
        "",
    ]
    for key, value in fields.items():
        if value is not None and value != "":
            lines.append(f"- {key}: {value}")
    lines.extend(["", "## Raw Text", "", "```text", text, "```", ""])
    return "\n".join(lines)


def sync_poi_memories(
    owntracks_log_path: Path,
    user_tags_path: Path,
    memory_dir: Path,
    scope: str | None = None,
    nearest_radius_m: int = 300,
    nearest_time_window_minutes: int = 180,
) -> PoiMemorySyncResult:
    events = parse_log(owntracks_log_path, IST)
    user_tags = load_user_tags(user_tags_path)
    plan_cache: dict[str, dict] = {}
    memory_dir.mkdir(parents=True, exist_ok=True)
    scanned = created = updated = unchanged = errors = 0
    for original in events:
        context = poi_context(original)
        if not original.is_location or not str(context["text"] or "").strip():
            continue
        event = resolved_poi_event(original)
        if not event_matches_scope(event, scope):
            continue
        scanned += 1
        try:
            location = poi_location(
                event,
                events,
                user_tags,
                plan_cache,
                nearest_radius_m,
                nearest_time_window_minutes,
            )
            stamp = event_time(event).strftime("%Y%m%d-%H%M%S")
            title = next((line.strip() for line in str(event.payload.get("poi") or "").splitlines() if line.strip()), "poi")
            path = memory_dir / f"owntracks-poi-{stamp}-{event.line_no}-{slugify(title)}.md"
            content = poi_memory_markdown(event, str(original.payload.get("poi") or ""), location)
            if not path.exists():
                path.write_text(content, encoding="utf-8")
                created += 1
            elif path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8")
                updated += 1
            else:
                unchanged += 1
        except Exception:
            errors += 1
    return PoiMemorySyncResult(scanned, created, updated, unchanged, errors)


async def poi_memory_loop(spending_cfg: Any, image_cfg: ImageSummaryConfig) -> None:
    while True:
        try:
            result = await asyncio.to_thread(
                sync_poi_memories,
                spending_cfg.owntracks_log_path,
                spending_cfg.user_tags_path,
                image_cfg.memory_dir,
                None,
                spending_cfg.nearest_stop_radius_m,
                spending_cfg.nearest_stop_time_window_minutes,
            )
            if result.created or result.updated or result.errors:
                log(
                    image_cfg,
                    "poi_memory_sync "
                    f"scanned={result.scanned} created={result.created} updated={result.updated} "
                    f"unchanged={result.unchanged} errors={result.errors}",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(image_cfg, f"poi_memory_sync_failed error={exc}")
        await asyncio.sleep(max(10, spending_cfg.poll_seconds))
