from __future__ import annotations

import calendar
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
import base64
import json
import math
import re
import urllib.error
import urllib.request

from metrics import OWNTRACKS_MAP_TILE_FETCHES_TOTAL


LINE_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\{.*\})$")


@dataclass
class Event:
    line_no: int
    received_at: datetime | None
    topic: str
    payload: dict
    local_tz: ZoneInfo

    @property
    def recorded_at(self) -> datetime | None:
        tst = self.payload.get("tst")
        if tst is None:
            return self.received_at
        try:
            return datetime.fromtimestamp(int(tst), tz=timezone.utc).astimezone(self.local_tz)
        except (TypeError, ValueError, OSError):
            return self.received_at

    @property
    def lat(self) -> float | None:
        return as_float(self.payload.get("lat"))

    @property
    def lon(self) -> float | None:
        return as_float(self.payload.get("lon"))

    @property
    def kind(self) -> str:
        return str(self.payload.get("_type") or "")

    @property
    def motion(self) -> list[str]:
        value = self.payload.get("motionactivities") or []
        return [str(item) for item in value] if isinstance(value, list) else [str(value)]

    @property
    def speed_kmh(self) -> float | None:
        return as_float(self.payload.get("vel"))

    @property
    def is_location(self) -> bool:
        return self.kind == "location" and self.lat is not None and self.lon is not None


MOTION_MODES = ("all", "stationary", "walking", "cycling", "automotive", "moving")
MOTION_COLORS = {
    "stationary": "#6b7280",
    "walking": "#16a34a",
    "cycling": "#2563eb",
    "automotive": "#f97316",
    "moving": "#7c3aed",
    "unknown": "#64748b",
}


@dataclass(frozen=True)
class OwnTracksScope:
    kind: str
    value: str
    start_date: date
    end_date: date


@dataclass(frozen=True)
class HomeFilterConfig:
    enabled: bool
    region_names: tuple[str, ...]
    radius_m: float


@dataclass(frozen=True)
class StopJitterFilterConfig:
    enabled: bool
    radius_m: float
    min_dwell_minutes: int
    include_geofences: bool
    include_candidate_stops: bool


@dataclass(frozen=True)
class StopJitterAnchor:
    lat: float
    lon: float
    label: str
    kind: str


def as_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def build_home_filter_config(env: dict[str, str]) -> HomeFilterConfig:
    return HomeFilterConfig(
        enabled=env_bool(env.get("OWNTRACKS_HOME_FILTER_ENABLED"), False),
        region_names=env_list(env.get("OWNTRACKS_HOME_REGION_NAMES"), ("Home",)),
        radius_m=float(env.get("OWNTRACKS_HOME_FILTER_RADIUS_METERS") or 150),
    )


def build_stop_jitter_filter_config(env: dict[str, str]) -> StopJitterFilterConfig:
    return StopJitterFilterConfig(
        enabled=env_bool(env.get("OWNTRACKS_STOP_JITTER_FILTER_ENABLED"), False),
        radius_m=float(env.get("OWNTRACKS_STOP_JITTER_RADIUS_METERS") or 150),
        min_dwell_minutes=int(env.get("OWNTRACKS_STOP_JITTER_MIN_DWELL_MINUTES") or 10),
        include_geofences=env_bool(env.get("OWNTRACKS_STOP_JITTER_INCLUDE_GEOFENCES"), True),
        include_candidate_stops=env_bool(env.get("OWNTRACKS_STOP_JITTER_INCLUDE_CANDIDATE_STOPS"), True),
    )


def parse_received_at(value: str, local_tz: ZoneInfo) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(local_tz)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).astimezone(local_tz)
    except ValueError:
        return None


def parse_log(path: Path, local_tz: ZoneInfo) -> list[Event]:
    events: list[Event] = []
    if not path.exists():
        return events
    with path.open(encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            match = LINE_RE.match(raw_line.strip())
            if not match:
                continue
            received_raw, topic, payload_raw = match.groups()
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue
            events.append(Event(line_no, parse_received_at(received_raw, local_tz), topic, payload, local_tz))
    return events


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius_km = 6371.0088
    d_lat = math.radians(b_lat - a_lat)
    d_lon = math.radians(b_lon - a_lon)
    lat1 = math.radians(a_lat)
    lat2 = math.radians(b_lat)
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(h))


def event_time(event: Event) -> datetime:
    return event.recorded_at or event.received_at or datetime.min.replace(tzinfo=event.local_tz)


def event_date(event: Event) -> date | None:
    dt = event.recorded_at or event.received_at
    return dt.date() if dt else None


def fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z") if dt else "unknown"


def fmt_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def slug(value: object) -> str:
    text = str(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "unknown"


def maps_url(lat: float, lon: float) -> str:
    return f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"


def home_region_names(config: HomeFilterConfig | None) -> set[str]:
    if not config:
        return set()
    return {name.strip().casefold() for name in config.region_names if name.strip()}


def event_region_names(event: Event) -> set[str]:
    names: set[str] = set()
    for value in event.payload.get("inregions") or []:
        if str(value).strip():
            names.add(str(value).strip().casefold())
    desc = str(event.payload.get("desc") or "").strip()
    if desc:
        names.add(desc.casefold())
    return names


def home_anchors(events: list[Event], config: HomeFilterConfig | None) -> list[tuple[float, float, str]]:
    names = home_region_names(config)
    if not config or not config.enabled or not names:
        return []
    anchors: list[tuple[float, float, str]] = []
    seen: set[tuple[float, float, str]] = set()
    for event in events:
        if event.kind != "transition" or event.lat is None or event.lon is None:
            continue
        desc = str(event.payload.get("desc") or "").strip()
        if desc.casefold() not in names:
            continue
        key = (round(event.lat, 6), round(event.lon, 6), desc.casefold())
        if key in seen:
            continue
        seen.add(key)
        anchors.append((event.lat, event.lon, desc or "home"))
    return anchors


def is_home_area_point(
    event: Event,
    config: HomeFilterConfig | None,
    anchors: list[tuple[float, float, str]],
) -> bool:
    if not config or not config.enabled or not event.is_location:
        return False
    names = home_region_names(config)
    if names and event_region_names(event) & names:
        return True
    if event.lat is None or event.lon is None:
        return False
    return any(haversine_km(event.lat, event.lon, lat, lon) * 1000 <= config.radius_m for lat, lon, _name in anchors)


def filter_home_area_points(
    points: list[Event],
    config: HomeFilterConfig | None,
    anchors: list[tuple[float, float, str]],
) -> tuple[list[Event], int]:
    if not config or not config.enabled:
        return points, 0
    filtered = [event for event in points if not is_home_area_point(event, config, anchors)]
    return filtered, len(points) - len(filtered)


def stop_jitter_anchors(
    events: list[Event],
    candidate_stops: list[dict],
    config: StopJitterFilterConfig | None,
) -> list[StopJitterAnchor]:
    if not config or not config.enabled:
        return []
    anchors: list[StopJitterAnchor] = []
    seen: set[tuple[float, float, str]] = set()

    def add_anchor(lat: object, lon: object, label: object, kind: str) -> None:
        anchor_lat = as_float(lat)
        anchor_lon = as_float(lon)
        if anchor_lat is None or anchor_lon is None:
            return
        text = str(label or kind).strip() or kind
        key = (round(anchor_lat, 5), round(anchor_lon, 5), text.casefold())
        if key in seen:
            return
        seen.add(key)
        anchors.append(StopJitterAnchor(anchor_lat, anchor_lon, text, kind))

    if config.include_geofences:
        for event in events:
            if event.kind != "transition":
                continue
            add_anchor(event.lat, event.lon, event.payload.get("desc"), "geofence")

    if config.include_candidate_stops:
        for stop in candidate_stops:
            if int(stop.get("duration_minutes") or 0) < config.min_dwell_minutes:
                continue
            label = stop.get("reviewed_name") or stop.get("name") or stop.get("alias") or stop.get("id")
            add_anchor(stop.get("lat"), stop.get("lon"), label, "candidate_stop")
    return anchors


def is_stop_jitter_point(
    event: Event,
    config: StopJitterFilterConfig | None,
    anchors: list[StopJitterAnchor],
) -> bool:
    if not config or not config.enabled or not event.is_location or event.lat is None or event.lon is None:
        return False
    return any(
        haversine_km(event.lat, event.lon, anchor.lat, anchor.lon) * 1000 <= config.radius_m
        for anchor in anchors
    )


def filter_stop_jitter_points(
    points: list[Event],
    config: StopJitterFilterConfig | None,
    anchors: list[StopJitterAnchor],
    preserve_lines: set[int] | None = None,
) -> tuple[list[Event], int]:
    if not config or not config.enabled:
        return points, 0
    preserve_lines = preserve_lines or set()
    jitter_flags = [is_stop_jitter_point(event, config, anchors) for event in points]
    keep_indices = {index for index, is_jitter in enumerate(jitter_flags) if not is_jitter}

    index = 0
    while index < len(points):
        if not jitter_flags[index]:
            index += 1
            continue
        start = index
        while index + 1 < len(points) and jitter_flags[index + 1]:
            index += 1
        end = index
        has_previous_route = start > 0 and not jitter_flags[start - 1]
        has_next_route = end + 1 < len(points) and not jitter_flags[end + 1]
        if has_previous_route:
            keep_indices.add(start)
        if has_next_route:
            keep_indices.add(end)
        for preserve_index in range(start, end + 1):
            event = points[preserve_index]
            if event.line_no in preserve_lines or event.payload.get("t") == "c":
                keep_indices.add(preserve_index)
        index += 1

    filtered = [event for index, event in enumerate(points) if index in keep_indices]
    return filtered, len(points) - len(filtered)


def normalized_motion_modes(event: Event) -> set[str]:
    return {str(item).strip().lower() for item in event.motion if str(item).strip()}


def motion_mode(event: Event) -> str:
    if not event.is_location:
        return "unknown"
    modes = normalized_motion_modes(event)
    speed = event.speed_kmh or 0
    if "automotive" in modes or "driving" in modes:
        return "automotive"
    if "cycling" in modes:
        return "cycling"
    if "walking" in modes:
        return "walking"
    if "stationary" in modes:
        return "stationary"
    if speed >= 8:
        return "moving"
    if speed <= 1:
        return "stationary"
    return "moving"


def motion_summary(points: list[Event]) -> dict:
    counts: Counter[str] = Counter()
    distances: Counter[str] = Counter()
    previous: Event | None = None
    for event in points:
        mode = motion_mode(event)
        counts[mode] += 1
        if previous and previous.lat is not None and previous.lon is not None and event.lat is not None and event.lon is not None:
            segment = haversine_km(previous.lat, previous.lon, event.lat, event.lon)
            if segment <= 5:
                distances[mode] += segment
        previous = event
    return {
        "counts": dict(counts),
        "distance_km": {mode: round(distance, 2) for mode, distance in distances.items()},
        "dominant": counts.most_common(1)[0][0] if counts else "unknown",
    }


def point_dict(event: Event) -> dict:
    dt = event_time(event)
    altitude = event.payload.get("alt")
    if altitude is None:
        altitude = event.payload.get("ele")
    return {
        "line": event.line_no,
        "time": fmt_dt(dt),
        "timestamp": int(dt.timestamp()) if dt.tzinfo is not None else None,
        "lat": event.lat,
        "lon": event.lon,
        "alt_m": as_float(altitude),
        "motion": event.motion,
        "motion_mode": motion_mode(event),
        "speed_kmh": event.speed_kmh,
        "accuracy_m": event.payload.get("acc"),
        "battery": event.payload.get("batt"),
        "regions": event.payload.get("inregions") or [],
        "maps": maps_url(event.lat, event.lon) if event.lat is not None and event.lon is not None else None,
    }


def is_moving_ride_point(event: Event) -> bool:
    if not event.is_location:
        return False
    motion = normalized_motion_modes(event)
    speed = event.speed_kmh or 0
    if "automotive" in motion or "driving" in motion:
        return False
    if "cycling" in motion:
        return True
    if "walking" in motion or "stationary" in motion:
        return False
    return speed >= 8 and "Home" not in (event.payload.get("inregions") or [])


def infer_ride_window(events: list[Event]) -> tuple[datetime | None, datetime | None, str]:
    ride_points = [event for event in events if is_moving_ride_point(event)]
    if not ride_points:
        return None, None, "no cycling/high-speed points found"
    first_ride = event_time(ride_points[0])
    last_ride = event_time(ride_points[-1])
    home_events = [event for event in events if event.kind == "transition" and event.payload.get("desc") == "Home"]
    start = first_ride
    end = last_ride
    reason = "first/last inferred ride point"
    leaves = [event_time(event) for event in home_events if event.payload.get("event") == "leave"]
    enters = [event_time(event) for event in home_events if event.payload.get("event") == "enter"]
    prior_leaves = [dt for dt in leaves if dt <= first_ride]
    later_enters = [dt for dt in enters if dt >= last_ride]
    if prior_leaves:
        start = max(prior_leaves)
        reason = "Home leave to Home enter around ride points"
    if later_enters:
        end = min(later_enters)
        reason = "Home leave to Home enter around ride points"
    return start, end, reason


def in_window(event: Event, start: datetime | None, end: datetime | None) -> bool:
    dt = event_time(event)
    return not ((start and dt < start) or (end and dt > end))


def summarize_distance(points: list[Event]) -> float:
    distance = 0.0
    previous: Event | None = None
    for point in points:
        if previous and previous.lat is not None and previous.lon is not None and point.lat is not None and point.lon is not None:
            segment = haversine_km(previous.lat, previous.lon, point.lat, point.lon)
            if segment <= 5:
                distance += segment
        previous = point
    return distance


def summarize_elevation(points: list[Event]) -> dict:
    min_alt: float | None = None
    max_alt: float | None = None
    ascent = 0.0
    descent = 0.0
    samples = 0
    previous_alt: float | None = None
    for point in points:
        alt = as_float(point.payload.get("alt"))
        if alt is None:
            alt = as_float(point.payload.get("ele"))
        if alt is None:
            continue
        samples += 1
        min_alt = alt if min_alt is None else min(min_alt, alt)
        max_alt = alt if max_alt is None else max(max_alt, alt)
        if previous_alt is not None:
            delta = alt - previous_alt
            if delta > 0:
              ascent += delta
            else:
                descent += abs(delta)
        previous_alt = alt
    return {
        "samples": samples,
        "min_alt_m": round(min_alt, 1) if min_alt is not None else None,
        "max_alt_m": round(max_alt, 1) if max_alt is not None else None,
        "ascent_m": round(ascent, 1),
        "descent_m": round(descent, 1),
        "range_m": round((max_alt - min_alt), 1) if min_alt is not None and max_alt is not None else None,
    }


def best_segment_speed_kmh(previous: Event, current: Event) -> tuple[float | None, float | None, str]:
    reported = current.speed_kmh
    derived: float | None = None
    seconds = (event_time(current) - event_time(previous)).total_seconds()
    if (
        seconds > 0
        and previous.lat is not None
        and previous.lon is not None
        and current.lat is not None
        and current.lon is not None
    ):
        distance_km = haversine_km(previous.lat, previous.lon, current.lat, current.lon)
        candidate = distance_km / (seconds / 3600)
        if math.isfinite(candidate) and candidate <= 160:
            derived = candidate
    if derived is None:
        return reported, derived, "OwnTracks vel" if reported is not None else "unknown"
    if reported is None:
        return derived, derived, "GPS distance/time"
    if reported <= 1 and derived > 3:
        return derived, derived, "GPS distance/time"
    if reported < derived * 0.4 and derived > 5:
        return derived, derived, "GPS distance/time"
    return reported, derived, "OwnTracks vel"


def ride_segment_stats(points: list[Event], start_ts: int, end_ts: int, label: str, start_name: str, end_name: str, index: int) -> dict | None:
    segment_points = [
        point
        for point in points
        if point.is_location
        and point.recorded_at is not None
        and start_ts <= int(point.recorded_at.timestamp()) <= end_ts
    ]
    if len(segment_points) < 2:
        return None

    distance_km = 0.0
    moving_seconds = 0.0
    speeds: list[float] = []
    derived_speeds: list[float] = []
    speed_sources: Counter[str] = Counter()
    motion_counts: Counter[str] = Counter()
    moving_motion_counts: Counter[str] = Counter()
    previous: Event | None = None
    for point in segment_points:
        motion_counts[motion_mode(point)] += 1
        if previous is not None:
            seconds = (event_time(point) - event_time(previous)).total_seconds()
            segment_distance = 0.0
            if seconds > 0 and previous.lat is not None and previous.lon is not None and point.lat is not None and point.lon is not None:
                segment_distance = haversine_km(previous.lat, previous.lon, point.lat, point.lon)
                if segment_distance <= 5:
                    distance_km += segment_distance
            best_speed, derived_speed, source = best_segment_speed_kmh(previous, point)
            current_mode = motion_mode(point)
            if seconds > 0 and (current_mode in {"walking", "cycling", "automotive", "moving"} or (best_speed is not None and best_speed > 3) or segment_distance > 0.03):
                moving_seconds += seconds
                moving_motion_counts[current_mode if current_mode in {"walking", "cycling", "automotive", "moving"} else "moving"] += 1
            if best_speed is not None:
                speeds.append(best_speed)
            if derived_speed is not None:
                derived_speeds.append(derived_speed)
            speed_sources[source] += 1
        previous = point

    duration_seconds = max(0, end_ts - start_ts)
    if duration_seconds <= 0 or distance_km < 0.05:
        return None
    average_speed = distance_km / (duration_seconds / 3600)
    moving_modes = {"walking", "cycling", "automotive", "moving"}
    moving_points = sum(count for mode, count in motion_counts.items() if mode in moving_modes)
    dominant_motion = motion_counts.most_common(1)[0][0] if motion_counts else "unknown"
    dominant_moving_motion = moving_motion_counts.most_common(1)[0][0] if moving_motion_counts else dominant_motion
    return {
        "id": f"r{index}",
        "label": label,
        "start_name": start_name,
        "end_name": end_name,
        "start_time": fmt_dt(datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(segment_points[0].local_tz)),
        "end_time": fmt_dt(datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone(segment_points[0].local_tz)),
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "duration_seconds": duration_seconds,
        "duration": fmt_duration(round(duration_seconds / 60)),
        "moving_duration_seconds": round(moving_seconds),
        "moving_duration": fmt_duration(round(moving_seconds / 60)),
        "distance_km": round(distance_km, 2),
        "average_speed_kmh": round(average_speed, 1),
        "moving_average_speed_kmh": round(distance_km / (moving_seconds / 3600), 1) if moving_seconds > 0 else None,
        "mean_point_speed_kmh": round(statistics_mean(speeds), 1) if speeds else None,
        "max_speed_kmh": round(max(speeds), 1) if speeds else None,
        "max_derived_speed_kmh": round(max(derived_speeds), 1) if derived_speeds else None,
        "point_count": len(segment_points),
        "moving_points": moving_points,
        "dominant_motion": dominant_motion,
        "dominant_moving_motion": dominant_moving_motion,
        "motion_counts": dict(motion_counts),
        "moving_motion_counts": dict(moving_motion_counts),
        "speed_sources": dict(speed_sources),
        "start_lat": segment_points[0].lat,
        "start_lon": segment_points[0].lon,
        "end_lat": segment_points[-1].lat,
        "end_lon": segment_points[-1].lon,
    }


def statistics_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_ride_segments(points: list[Event], stops: list[dict], places: list[dict]) -> list[dict]:
    anchors: list[dict] = []
    for place in places:
        timestamp = place.get("timestamp")
        if not isinstance(timestamp, int):
            continue
        name = str(place.get("name") or "place")
        anchors.append(
            {
                "entry": timestamp,
                "exit": timestamp,
                "name": name,
                "kind": f"geofence:{place.get('action') or 'event'}",
            }
        )
    for stop in stops:
        start_ts = stop.get("start_timestamp")
        end_ts = stop.get("end_timestamp")
        if not isinstance(start_ts, int) or not isinstance(end_ts, int):
            continue
        name = str(stop.get("reviewed_name") or stop.get("name") or stop.get("alias") or "stop")
        anchors.append(
            {
                "entry": start_ts,
                "exit": end_ts,
                "name": name,
                "kind": "stop",
            }
        )

    deduped: list[dict] = []
    for anchor in sorted(anchors, key=lambda item: (item["entry"], item["exit"], item["name"])):
        if deduped and abs(anchor["entry"] - deduped[-1]["entry"]) <= 30 and anchor["name"] == deduped[-1]["name"]:
            deduped[-1]["exit"] = max(deduped[-1]["exit"], anchor["exit"])
            deduped[-1]["kind"] = f"{deduped[-1]['kind']}+{anchor['kind']}"
            continue
        deduped.append(anchor)

    segments: list[dict] = []
    for previous_anchor, next_anchor in zip(deduped, deduped[1:]):
        start_ts = previous_anchor["exit"]
        end_ts = next_anchor["entry"]
        if end_ts <= start_ts:
            continue
        start_name = previous_anchor["name"]
        end_name = next_anchor["name"]
        segment = ride_segment_stats(points, start_ts, end_ts, f"{start_name} to {end_name}", str(start_name), str(end_name), len(segments) + 1)
        if segment is not None:
            segment["anchor_type"] = f"{previous_anchor['kind']} to {next_anchor['kind']}"
            segments.append(segment)
    return segments


def named_place_events(events: list[Event]) -> list[dict]:
    places = []
    for event in events:
        if event.kind != "transition":
            continue
        desc = event.payload.get("desc")
        if not desc:
            continue
        lat = event.lat
        lon = event.lon
        places.append(
            {
                "id": f"{slug(desc)}-{event.payload.get('event')}-{event.line_no}",
                "name": desc,
                "action": event.payload.get("event"),
                "time": fmt_dt(event_time(event)),
                "timestamp": int(event_time(event).timestamp()) if event_time(event).tzinfo is not None else None,
                "lat": lat,
                "lon": lon,
                "line": event.line_no,
                "tags": [f"place:{slug(desc)}", f"geofence:{event.payload.get('event')}"],
                "maps": maps_url(lat, lon) if lat is not None and lon is not None else None,
            }
        )
    return places


def is_stop_candidate_event(event: Event) -> bool:
    if not event.is_location:
        return False
    if "Home" in (event.payload.get("inregions") or []):
        return False
    speed = event.speed_kmh
    if speed is not None and speed > 3:
        return False
    mode = motion_mode(event)
    if mode in {"stationary", "automotive", "moving"}:
        return True
    return speed is not None and speed <= 3


def candidate_stops(events: list[Event], min_minutes: int = 10, radius_m: int = 180) -> list[dict]:
    sparse_same_place_radius_m = min(50, radius_m)
    low_motion = [
        event
        for event in events
        if is_stop_candidate_event(event)
    ]
    clusters: list[list[Event]] = []
    current: list[Event] = []
    for event in low_motion:
        if not current:
            current = [event]
            continue
        center_lat = sum(item.lat or 0 for item in current) / len(current)
        center_lon = sum(item.lon or 0 for item in current) / len(current)
        dt_gap = (event_time(event) - event_time(current[-1])).total_seconds()
        dist_m = haversine_km(center_lat, center_lon, event.lat or 0, event.lon or 0) * 1000
        if dist_m <= radius_m and (dt_gap <= 45 * 60 or dist_m <= sparse_same_place_radius_m):
            current.append(event)
        else:
            clusters.append(current)
            current = [event]
    if current:
        clusters.append(current)

    stops = []
    for index, cluster in enumerate(clusters, start=1):
        start = event_time(cluster[0])
        end = event_time(cluster[-1])
        duration_minutes = max(0, round((end - start).total_seconds() / 60))
        if duration_minutes < min_minutes and len(cluster) < 3:
            continue
        lat = sum(item.lat or 0 for item in cluster) / len(cluster)
        lon = sum(item.lon or 0 for item in cluster) / len(cluster)
        motions = Counter(motion for item in cluster for motion in item.motion)
        regions = Counter(region for item in cluster for region in (item.payload.get("inregions") or []))
        motion_modes = Counter(motion_mode(item) for item in cluster)
        dominant_motion = motion_modes.most_common(1)[0][0] if motion_modes else "unknown"
        name = regions.most_common(1)[0][0] if regions else f"unnamed-stop-{index}"
        stop_id = f"{slug(name)}-{cluster[0].line_no}"
        stops.append(
            {
                "id": stop_id,
                "name": name,
                "start": fmt_dt(start),
                "end": fmt_dt(end),
                "start_line": cluster[0].line_no,
                "end_line": cluster[-1].line_no,
                "start_timestamp": int(start.timestamp()) if start.tzinfo is not None else None,
                "end_timestamp": int(end.timestamp()) if end.tzinfo is not None else None,
                "duration_minutes": duration_minutes,
                "duration": fmt_duration(duration_minutes),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "points": len(cluster),
                "motion": ", ".join(f"{name}:{count}" for name, count in motions.most_common()) or "unknown",
                "motion_mode": dominant_motion,
                "motion_modes": ", ".join(f"{name}:{count}" for name, count in motion_modes.most_common()) or "unknown",
                "tags": [f"stop:{stop_id}", "candidate:stop"],
                "maps": maps_url(lat, lon),
            }
        )
    return stops


def heatmap_visit_clusters(events: list[Event], min_minutes: int = 10, radius_m: int = 180, max_gap_minutes: int = 45) -> list[dict]:
    low_motion = [
        event
        for event in events
        if event.is_location
        and event.lat is not None
        and event.lon is not None
        and motion_mode(event) != "automotive"
        and (event.speed_kmh is None or event.speed_kmh <= 3)
    ]
    clusters: list[list[Event]] = []
    current: list[Event] = []
    for event in low_motion:
        if not current:
            current = [event]
            continue
        center_lat = sum(item.lat or 0 for item in current) / len(current)
        center_lon = sum(item.lon or 0 for item in current) / len(current)
        dt_gap = (event_time(event) - event_time(current[-1])).total_seconds()
        dist_m = haversine_km(center_lat, center_lon, event.lat or 0, event.lon or 0) * 1000
        if dist_m <= radius_m and dt_gap <= max_gap_minutes * 60:
            current.append(event)
        else:
            clusters.append(current)
            current = [event]
    if current:
        clusters.append(current)

    visits = []
    for cluster in clusters:
        start = event_time(cluster[0])
        end = event_time(cluster[-1])
        duration_minutes = max(0, round((end - start).total_seconds() / 60))
        if duration_minutes < min_minutes and len(cluster) < 3:
            continue
        visits.append(
            {
                "lat": sum(item.lat or 0 for item in cluster) / len(cluster),
                "lon": sum(item.lon or 0 for item in cluster) / len(cluster),
                "duration_minutes": duration_minutes,
                "mode": Counter(motion_mode(item) for item in cluster).most_common(1)[0][0],
            }
        )
    return visits


def load_user_tags(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_user_tags(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def location_override_for(stop: dict, user_tags: dict, current_date: str, radius_m: int = 150) -> dict:
    stop_lat = as_float(stop.get("lat"))
    stop_lon = as_float(stop.get("lon"))
    if stop_lat is None or stop_lon is None:
        return {}
    best_key: tuple[bool, str, float] | None = None
    best_override: dict | None = None
    for date_key, day_tags in user_tags.items():
        if not isinstance(day_tags, dict):
            continue
        if date_key > current_date:
            continue
        for saved_stop in day_tags.get("stops", {}).values():
            if not isinstance(saved_stop, dict) or not saved_stop.get("name"):
                continue
            saved_lat = as_float(saved_stop.get("lat"))
            saved_lon = as_float(saved_stop.get("lon"))
            if saved_lat is None or saved_lon is None:
                continue
            distance_m = haversine_km(stop_lat, stop_lon, saved_lat, saved_lon) * 1000
            if distance_m > radius_m:
                continue
            candidate_key = (date_key == current_date, date_key, -distance_m)
            if best_key is None or candidate_key > best_key:
                best_key = candidate_key
                best_override = {**saved_stop, "_date": date_key, "_distance_m": round(distance_m)}
    if best_override is None:
        return {}
    override = best_override.copy()
    override.pop("note", None)
    override.pop("_date", None)
    return override


def apply_user_tags(plan: dict, user_tags: dict) -> dict:
    day_tags = user_tags.get(plan["date"], {})
    global_tags = day_tags.get("activity", day_tags.get("ride", {})).get("tags", [])
    plan["recommended_tags"] = sorted(set(plan["recommended_tags"] + global_tags))
    stop_overrides = day_tags.get("stops", {})
    for stop in plan["candidate_stops"]:
        override = merge_stop_overrides(
            [
                location_override_for(stop, user_tags, plan["date"]),
                stop_override_for(stop, stop_overrides),
            ]
        )
        if override.get("name"):
            stop["reviewed_name"] = override["name"]
        if override.get("tags"):
            stop["user_tags"] = override["tags"]
            plan["recommended_tags"] = sorted(set(plan["recommended_tags"] + override["tags"]))
        if override.get("note"):
            stop["user_note"] = override["note"]
    return plan


def stop_override_for(stop: dict, stop_overrides: dict) -> dict:
    stop_id = stop["id"]
    matches = []
    for fallback_id in fallback_stop_ids(stop_id):
        if fallback_id in stop_overrides:
            matches.append(stop_overrides[fallback_id])
    old_range_re = re.compile(rf"^{re.escape(stop_id)}-\d+$")
    for saved_id, override in stop_overrides.items():
        if old_range_re.fullmatch(saved_id):
            matches.append(override)
    if stop_id in stop_overrides:
        matches.append(stop_overrides[stop_id])
    return merge_stop_overrides(matches)


def merge_stop_overrides(overrides: list[dict]) -> dict:
    merged: dict = {}
    merged_tags: list[str] = []
    for override in overrides:
        if not isinstance(override, dict):
            continue
        if override.get("name"):
            merged["name"] = override["name"]
        if override.get("note"):
            merged["note"] = override["note"]
        raw_tags = override.get("tags") or []
        tags = raw_tags if isinstance(raw_tags, list) else [raw_tags]
        for tag in tags:
            tag_text = str(tag).strip()
            if tag_text and tag_text not in merged_tags:
                merged_tags.append(tag_text)
    if merged_tags:
        merged["tags"] = merged_tags
    return merged


def fallback_stop_ids(stop_id: str) -> list[str]:
    values = []
    legacy_id = legacy_stop_id(stop_id)
    if legacy_id:
        values.append(legacy_id)
    stable_match = re.fullmatch(r"(.+-\d+)-\d+", stop_id)
    if stable_match and stable_match.group(1) not in values:
        values.append(stable_match.group(1))
    return values


def legacy_stop_id(stop_id: str) -> str | None:
    match = re.fullmatch(r"(unnamed-stop-\d+)(?:-\d+){1,2}", stop_id)
    return match.group(1) if match else None


def build_geojson(points: list[Event], places: list[dict], stops: list[dict]) -> dict:
    features = []
    for event in points:
        if event.lat is None or event.lon is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [event.lon, event.lat]},
                "properties": {
                    "kind": "track",
                    "time": fmt_dt(event_time(event)),
                    "motion": ",".join(event.motion),
                    "speed_kmh": event.speed_kmh,
                    "line": event.line_no,
                },
            }
        )
    for place in places:
        if place["lat"] is None or place["lon"] is None:
            continue
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [place["lon"], place["lat"]]}, "properties": {"kind": "named-place", **place}})
    for stop in stops:
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [stop["lon"], stop["lat"]]}, "properties": {"kind": "candidate-stop", **stop}})
    return {"type": "FeatureCollection", "features": features}


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def lat_to_tile_y(lat: float, zoom: int) -> int:
    lat_rad = math.radians(clamp(lat, -85.05112878, 85.05112878))
    return math.floor((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * (2**zoom))


def lon_to_tile_x(lon: float, zoom: int) -> int:
    return math.floor((lon + 180) / 360 * (2**zoom))


def tile_lon(x: int, zoom: int) -> float:
    return x / (2**zoom) * 360 - 180


def tile_lat(y: int, zoom: int) -> float:
    n = math.pi - 2 * math.pi * y / (2**zoom)
    return math.degrees(math.atan(0.5 * (math.exp(n) - math.exp(-n))))


def fetch_tile_data_uri(zoom: int, x: int, y: int, cache_dir: Path | None) -> str | None:
    if cache_dir is None:
        return None
    cache_path = cache_dir / str(zoom) / str(x) / f"{y}.png"
    try:
        if cache_path.exists():
            data = cache_path.read_bytes()
            OWNTRACKS_MAP_TILE_FETCHES_TOTAL.labels(result="cache_hit").inc()
        else:
            url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "telegram-control-owntracks-map/0.1"},
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                data = response.read()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
            OWNTRACKS_MAP_TILE_FETCHES_TOTAL.labels(result="downloaded").inc()
    except (OSError, urllib.error.URLError, TimeoutError):
        OWNTRACKS_MAP_TILE_FETCHES_TOTAL.labels(result="error").inc()
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def render_map_html(plan: dict, tile_cache_dir: Path | None = None) -> str:
    title = f"OwnTracks map - {plan['date']}"
    track_points = [
        {
            "lat": point["lat"],
            "lon": point["lon"],
            "motion_mode": point.get("motion_mode") or "moving",
            "speed_kmh": point.get("speed_kmh"),
        }
        for point in plan.get("sampled_track", [])
        if point.get("lat") is not None and point.get("lon") is not None
    ]
    track = [
        [point["lat"], point["lon"]]
        for point in track_points
    ]
    stops = [
        {
            "alias": stop.get("alias", ""),
            "name": stop.get("reviewed_name") or stop.get("name") or "unnamed stop",
            "id": stop.get("id", ""),
            "lat": stop.get("lat"),
            "lon": stop.get("lon"),
            "start": stop.get("start", ""),
            "end": stop.get("end", ""),
            "duration": stop.get("duration", ""),
            "points": stop.get("points", ""),
            "maps": stop.get("maps", ""),
            "tags": stop.get("user_tags", []),
            "note": stop.get("user_note", ""),
        }
        for stop in plan.get("candidate_stops", [])
        if stop.get("lat") is not None and stop.get("lon") is not None
    ]
    named_places = [
        {
            "name": place.get("name", ""),
            "action": place.get("action", ""),
            "lat": place.get("lat"),
            "lon": place.get("lon"),
            "time": place.get("time", ""),
        }
        for place in plan.get("named_places", [])
        if place.get("lat") is not None and place.get("lon") is not None
    ]
    motion_summary = plan.get("motion_summary") or {}
    motion_counts = motion_summary.get("counts") or {}
    motion_dom = motion_summary.get("dominant") or "unknown"
    motion_chips = ['<button type="button" class="motion-chip all active" data-motion-mode="all"><span class="dot"></span>all</button>']
    for mode in ("stationary", "walking", "cycling", "automotive", "moving"):
        count = motion_counts.get(mode)
        if count:
            motion_chips.append(
                f'<button type="button" class="motion-chip {escape(mode)}" data-motion-mode="{escape(mode)}"><span class="dot"></span>{escape(mode)}: {count}</button>'
            )
    if motion_chips:
        motion_chips.insert(1, f'<span class="motion-chip dominant"><span class="dot"></span>dominant: {escape(motion_dom)}</span>')
    all_points = [
        *track,
        *[[stop["lat"], stop["lon"]] for stop in stops],
        *[[place["lat"], place["lon"]] for place in named_places],
    ]

    def finite_point(point: list[object]) -> tuple[float, float] | None:
        try:
            lat = float(point[0])
            lon = float(point[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not math.isfinite(lat) or not math.isfinite(lon):
            return None
        return lat, lon

    finite_points = [point for point in (finite_point(point) for point in all_points) if point is not None]
    center_lat = sum(point[0] for point in finite_points) / len(finite_points) if finite_points else 0.0
    cos_lat = max(0.2, math.cos(math.radians(center_lat)))
    projected = [(lon * cos_lat, lat) for lat, lon in finite_points] or [(0.0, 0.0)]
    min_x = min(point[0] for point in projected)
    max_x = max(point[0] for point in projected)
    min_y = min(point[1] for point in projected)
    max_y = max(point[1] for point in projected)
    span_x = max(max_x - min_x, 0.001)
    span_y = max(max_y - min_y, 0.001)
    svg_width = 1000
    svg_height = 700
    svg_padding = 70
    svg_scale = min((svg_width - svg_padding * 2) / span_x, (svg_height - svg_padding * 2) / span_y)

    def project_point(lat: object, lon: object) -> tuple[float, float] | None:
        point = finite_point([lat, lon])
        if point is None:
            return None
        lat_float, lon_float = point
        x = (
            svg_padding
            + ((lon_float * cos_lat - min_x) * svg_scale)
            + ((svg_width - svg_padding * 2 - span_x * svg_scale) / 2)
        )
        y = (
            svg_height
            - svg_padding
            - ((lat_float - min_y) * svg_scale)
            - ((svg_height - svg_padding * 2 - span_y * svg_scale) / 2)
        )
        return x, y

    route_parts = []
    fallback_segments = []
    fallback_track_markers = []
    previous_track_point: dict | None = None
    for index, point in enumerate(track_points):
        xy = project_point(point["lat"], point["lon"])
        if xy is None:
            continue
        route_parts.append(f"{'L' if index else 'M'} {xy[0]:.1f} {xy[1]:.1f}")
        if len(track_points) == 1:
            fallback_track_markers.append(
                f'<g class="track-point"><circle cx="{xy[0]:.1f}" cy="{xy[1]:.1f}" r="11"></circle>'
                f'<text class="place-label" x="{xy[0] + 14:.1f}" y="{xy[1] - 12:.1f}">location point</text></g>'
            )
        if previous_track_point is not None:
            prev_xy = project_point(previous_track_point["lat"], previous_track_point["lon"])
            if prev_xy is not None:
                mode = point.get("motion_mode") or previous_track_point.get("motion_mode") or "moving"
                fallback_segments.append(
                    f'<line class="route-segment motion-{escape(str(mode))}" x1="{prev_xy[0]:.1f}" y1="{prev_xy[1]:.1f}" '
                    f'x2="{xy[0]:.1f}" y2="{xy[1]:.1f}"></line>'
                )
        previous_track_point = point
    fallback_route = " ".join(route_parts)
    tile_images = []
    embed_tiles = tile_cache_dir is not None
    if finite_points:
        lon_span = max(0.00001, (max_x - min_x) / cos_lat)
        tile_zoom = int(clamp(round(math.log2(5 * 360 / lon_span)), 1, 19))
        max_tile = (2**tile_zoom) - 1
        min_lon = min(point[1] for point in finite_points)
        max_lon = max(point[1] for point in finite_points)
        min_lat = min(point[0] for point in finite_points)
        max_lat = max(point[0] for point in finite_points)
        while True:
            max_tile = (2**tile_zoom) - 1
            min_tile_x = int(clamp(lon_to_tile_x(min_lon, tile_zoom) - 1, 0, max_tile))
            max_tile_x = int(clamp(lon_to_tile_x(max_lon, tile_zoom) + 1, 0, max_tile))
            min_tile_y = int(clamp(lat_to_tile_y(max_lat, tile_zoom) - 1, 0, max_tile))
            max_tile_y = int(clamp(lat_to_tile_y(min_lat, tile_zoom) + 1, 0, max_tile))
            tile_count = (max_tile_x - min_tile_x + 1) * (max_tile_y - min_tile_y + 1)
            if tile_count <= 36 or tile_zoom <= 1:
                break
            tile_zoom -= 1
        for tile_x in range(min_tile_x, max_tile_x + 1):
            for tile_y in range(min_tile_y, max_tile_y + 1):
                xy1 = project_point(tile_lat(tile_y, tile_zoom), tile_lon(tile_x, tile_zoom))
                xy2 = project_point(tile_lat(tile_y + 1, tile_zoom), tile_lon(tile_x + 1, tile_zoom))
                if xy1 is None or xy2 is None:
                    continue
                href = fetch_tile_data_uri(tile_zoom, tile_x, tile_y, tile_cache_dir)
                if href is None and not embed_tiles:
                    href = f"https://tile.openstreetmap.org/{tile_zoom}/{tile_x}/{tile_y}.png"
                if href is None:
                    continue
                tile_images.append(
                    f'<image class="tile" href="{href}" x="{min(xy1[0], xy2[0]):.1f}" y="{min(xy1[1], xy2[1]):.1f}" '
                    f'width="{abs(xy2[0] - xy1[0]):.1f}" height="{abs(xy2[1] - xy1[1]):.1f}" preserveAspectRatio="none"></image>'
                )
    embedded_tile_images = embed_tiles
    fallback_places = []
    for place in named_places:
        xy = project_point(place["lat"], place["lon"])
        if xy is None:
            continue
        label = escape(f"{place['action'] or ''} {place['name']}".strip())
        fallback_places.append(
            f'<g><circle class="place" cx="{xy[0]:.1f}" cy="{xy[1]:.1f}" r="6"></circle>'
            f'<text class="place-label" x="{xy[0] + 9:.1f}" y="{xy[1] - 9:.1f}">{label}</text></g>'
        )
    fallback_stops = []
    stop_positions = []
    for index, stop in enumerate(stops):
        xy = project_point(stop["lat"], stop["lon"])
        if xy is None:
            continue
        close_count = sum(1 for existing_x, existing_y in stop_positions if math.hypot(xy[0] - existing_x, xy[1] - existing_y) < 42)
        stop_positions.append(xy)
        angle = close_count * 1.7
        label_dx = 16 + (22 * close_count)
        label_dy = -31 - (14 * close_count)
        dot_dx = math.cos(angle) * min(18 * close_count, 56)
        dot_dy = math.sin(angle) * min(18 * close_count, 56)
        draw_x = xy[0] + dot_dx
        draw_y = xy[1] + dot_dy
        label = f"{stop['alias']}: {stop['name']}"
        label_width = max(58, min(260, len(label) * 8 + 16))
        fallback_stops.append(
            f'<g class="stop" data-alias="{escape(str(stop["alias"]))}" transform="translate({draw_x:.1f}, {draw_y:.1f})">'
            f'<line class="leader" x1="0" y1="0" x2="{xy[0] - draw_x:.1f}" y2="{xy[1] - draw_y:.1f}"></line>'
            f'<circle r="10"></circle><rect class="label-bg" x="{label_dx}" y="{label_dy}" width="{label_width}" height="24"></rect>'
            f'<text class="label-text" x="{label_dx + 8}" y="{label_dy + 17}">{escape(label)}</text></g>'
        )
    payload = json.dumps(
        {
            "date": plan["date"],
            "track": track,
            "sampledTrack": plan.get("sampled_track", []),
            "stops": stops,
            "namedPlaces": named_places,
            "motionSummary": plan.get("motion_summary") or {},
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    escaped_title = escape(title)
    has_map_data = bool(finite_points)
    empty_svg = (
        ""
        if has_map_data
        else (
            '<g id="emptyState">'
            '<rect x="250" y="275" width="500" height="150" rx="8" fill="white" opacity="0.94"></rect>'
            f'<text x="500" y="333" text-anchor="middle" class="empty-title">No OwnTracks points for {escape(plan["date"])}</text>'
            '<text x="500" y="365" text-anchor="middle" class="empty-copy">Use /otm yesterday, /otm DD, /otm MM-DD, or a date with logged points.</text>'
            "</g>"
        )
    )
    empty_panel = (
        ""
        if has_map_data
        else (
            '<section class="panel empty-panel">'
            f'<strong>No location data for {escape(plan["date"])}</strong>'
            '<p class="hint">This map has no track points, stops, or named places. If you typed a future date, try yesterday or a date that has OwnTracks logs.</p>'
            "</section>"
        )
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    * {{
      box-sizing: border-box;
    }}
    html, body {{
      margin: 0;
      min-height: 100%;
    }}
    body {{
      background: #f8fafc;
      color: #111827;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .app {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      min-height: 100vh;
    }}
    .map-wrap {{
      background: #e5eef7;
      min-height: 62vh;
      position: relative;
    }}
    svg {{
      display: block;
      height: 100%;
      min-height: 62vh;
      touch-action: none;
      width: 100%;
    }}
    .side {{
      background: white;
      border-left: 1px solid #d1d5db;
      display: flex;
      flex-direction: column;
      max-height: 100vh;
    }}
    .panel {{
      border-bottom: 1px solid #e5e7eb;
      padding: 12px;
    }}
    h1 {{
      font-size: 17px;
      line-height: 1.25;
      margin: 0 0 8px;
    }}
    label {{
      color: #374151;
      display: block;
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 5px;
    }}
    input, textarea {{
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font: inherit;
      padding: 8px;
      width: 100%;
    }}
    textarea {{
      min-height: 138px;
      resize: vertical;
    }}
    button {{
      align-items: center;
      background: #111827;
      border: 0;
      border-radius: 6px;
      color: white;
      cursor: pointer;
      display: inline-flex;
      font: inherit;
      font-size: 13px;
      font-weight: 700;
      justify-content: center;
      min-height: 34px;
      padding: 7px 10px;
    }}
    button.secondary {{
      background: #e5e7eb;
      color: #111827;
    }}
    button.warning {{
      background: #b91c1c;
    }}
    .row {{
      display: flex;
      gap: 8px;
    }}
    .row > * {{
      flex: 1;
    }}
    .hint {{
      color: #4b5563;
      font-size: 12px;
      line-height: 1.35;
      margin-top: 8px;
    }}
    .stops {{
      overflow: auto;
      padding: 8px 12px 14px;
    }}
    .stop-row {{
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      margin-bottom: 8px;
      padding: 8px;
    }}
    .stop-row.selected {{
      border-color: #dc2626;
      box-shadow: 0 0 0 1px #dc2626 inset;
    }}
    .stop-head {{
      align-items: center;
      display: flex;
      gap: 8px;
      margin-bottom: 7px;
    }}
    .stop-head input {{
      width: auto;
    }}
    .alias {{
      background: #111827;
      border-radius: 4px;
      color: white;
      font-size: 12px;
      font-weight: 800;
      min-width: 34px;
      padding: 3px 5px;
      text-align: center;
    }}
    .meta {{
      color: #6b7280;
      font-size: 11px;
      line-height: 1.35;
      margin-top: 5px;
    }}
    .map-controls {{
      display: flex;
      gap: 6px;
      left: 10px;
      position: absolute;
      top: 10px;
      z-index: 2;
    }}
    .map-controls button {{
      min-height: 32px;
      min-width: 36px;
      padding: 6px 8px;
    }}
    .map-status {{
      background: rgb(255 255 255 / 0.92);
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      bottom: 10px;
      box-shadow: 0 2px 10px rgb(15 23 42 / 0.14);
      color: #111827;
      font-size: 12px;
      font-weight: 700;
      left: 10px;
      line-height: 1.35;
      padding: 6px 8px;
      position: absolute;
      z-index: 2;
    }}
    .route-segment {{
      fill: none;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 5;
    }}
    .route-segment.motion-stationary {{
      stroke: {MOTION_COLORS["stationary"]};
    }}
    .route-segment.motion-walking {{
      stroke: {MOTION_COLORS["walking"]};
    }}
    .route-segment.motion-cycling {{
      stroke: {MOTION_COLORS["cycling"]};
    }}
    .route-segment.motion-automotive {{
      stroke: {MOTION_COLORS["automotive"]};
    }}
    .route-segment.motion-moving {{
      stroke: {MOTION_COLORS["moving"]};
    }}
    .route-segment.motion-unknown {{
      stroke: {MOTION_COLORS["unknown"]};
    }}
    .route-arrow-marker {{
      background: transparent;
      border: 0;
    }}
    .route-arrow {{
      color: rgba(15, 23, 42, 0.92);
      -webkit-text-stroke: 2px white;
      font-size: 20px;
      font-weight: 900;
      line-height: 1;
      text-shadow: 0 1px 4px rgb(15 23 42 / 0.35);
      transform-origin: center;
    }}
    .tile {{
      image-rendering: auto;
      pointer-events: none;
    }}
    .place {{
      fill: #2563eb;
      stroke: white;
      stroke-width: 3;
    }}
    .track-point circle {{
      fill: #7c3aed;
      stroke: white;
      stroke-width: 4;
    }}
    .motion-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }}
    .motion-chip {{
      align-items: center;
      appearance: none;
      border-radius: 999px;
      border: 0;
      cursor: pointer;
      color: white;
      display: inline-flex;
      font-size: 12px;
      font-weight: 800;
      gap: 6px;
      padding: 5px 9px;
    }}
    .motion-chip:hover {{
      filter: brightness(1.08);
    }}
    .motion-chip.active {{
      box-shadow: 0 0 0 2px rgb(255 255 255 / 0.65) inset;
    }}
    .motion-chip .dot {{
      background: rgb(255 255 255 / 0.9);
      border-radius: 999px;
      height: 8px;
      width: 8px;
    }}
    .motion-chip.stationary {{
      background: {MOTION_COLORS["stationary"]};
    }}
    .motion-chip.walking {{
      background: {MOTION_COLORS["walking"]};
    }}
    .motion-chip.cycling {{
      background: {MOTION_COLORS["cycling"]};
    }}
    .motion-chip.automotive {{
      background: {MOTION_COLORS["automotive"]};
    }}
    .motion-chip.moving {{
      background: {MOTION_COLORS["moving"]};
    }}
    .motion-chip.dominant {{
      background: #0f172a;
      cursor: default;
    }}
    .stop {{
      cursor: pointer;
    }}
    .hit-target {{
      fill: white;
      opacity: 0.001;
      pointer-events: all;
    }}
    .leader {{
      stroke: #111827;
      stroke-dasharray: 3 3;
      stroke-width: 1.5;
    }}
    .stop circle {{
      fill: #dc2626;
      stroke: white;
      stroke-width: 4;
    }}
    .stop.selected circle {{
      fill: #f59e0b;
      stroke: #111827;
    }}
    .label-bg {{
      fill: #111827;
      opacity: 0.9;
      rx: 4;
    }}
    .label-text {{
      fill: white;
      font-size: 14px;
      font-weight: 800;
      pointer-events: none;
    }}
    .place-label {{
      fill: #1e3a8a;
      font-size: 12px;
      font-weight: 750;
      paint-order: stroke;
      stroke: white;
      stroke-width: 4px;
    }}
    .empty-title {{
      fill: #111827;
      font-size: 22px;
      font-weight: 800;
    }}
    .empty-copy {{
      fill: #4b5563;
      font-size: 14px;
      font-weight: 600;
    }}
    .empty-panel {{
      background: #fff7ed;
      border-bottom-color: #fed7aa;
    }}
    @media (max-width: 820px) {{
      .app {{
        grid-template-columns: 1fr;
      }}
      .side {{
        border-left: 0;
        max-height: none;
      }}
      .map-wrap, svg {{
        min-height: 54vh;
      }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <main class="map-wrap">
      <div class="map-controls">
        <button id="zoomIn" type="button">+</button>
        <button id="zoomOut" type="button">-</button>
        <button id="resetView" class="secondary" type="button">Reset</button>
      </div>
      <div id="mapStatus" class="map-status">tiles: loading</div>
      <svg id="map" aria-label="{escaped_title}" viewBox="0 0 1000 700" role="img">
        <defs>
          <pattern id="grid" width="50" height="50" patternUnits="userSpaceOnUse">
            <path d="M 50 0 L 0 0 0 50" fill="none" stroke="#cbd5e1" stroke-width="1"/>
          </pattern>
        </defs>
        <rect x="-100000" y="-100000" width="200000" height="200000" fill="url(#grid)"/>
        <g id="viewport">
          <g id="tiles">{"".join(tile_images)}</g>
          <g id="route">{''.join(fallback_segments)}{''.join(fallback_track_markers)}</g>
          <g id="places">{"".join(fallback_places)}</g>
          <g id="stops">{"".join(fallback_stops)}</g>
          {empty_svg}
        </g>
      </svg>
    </main>
    <aside class="side">
      <section class="panel">
        <h1>{escaped_title}</h1>
        <div class="row">
          <button id="selectAll" class="secondary" type="button">Select all</button>
          <button id="clearSelection" class="secondary" type="button">Clear</button>
        </div>
        <button id="centerSelected" class="secondary" type="button" style="margin-top: 8px; width: 100%">Center selected</button>
        <p class="hint">Tap map labels or check rows, rename selected stops, then paste the generated command back into Telegram.</p>
        <div class="motion-summary">{''.join(motion_chips) if motion_chips else '<span class="hint">Motion summary unavailable</span>'}</div>
      </section>
      {empty_panel}
      <section class="panel">
        <label for="groupDistance">Nearby grouping distance, meters</label>
        <input id="groupDistance" type="number" min="20" step="10" value="150">
        <div class="row" style="margin-top: 8px">
          <button id="selectNearby" type="button">Select nearby</button>
          <button id="groupSelected" type="button">Group selected</button>
        </div>
      </section>
      <section class="panel">
        <label for="bulkName">Name for selected stops</label>
        <input id="bulkName" placeholder="Place name">
        <div class="row" style="margin-top: 8px">
          <button id="applyName" type="button">Apply name</button>
          <button id="resetNames" class="warning" type="button">Reset names</button>
        </div>
      </section>
      <section class="stops" id="stopList"></section>
      <section class="panel">
        <label for="commands">Paste this in the OwnTracks Telegram topic</label>
        <textarea id="commands" readonly></textarea>
        <div class="row" style="margin-top: 8px">
          <button id="copyCommands" type="button">Copy</button>
          <button id="refreshCommands" class="secondary" type="button">Refresh</button>
        </div>
      </section>
    </aside>
  </div>
  <script>
    const data = {payload};
    const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({{
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }}[char]));
    const svg = document.getElementById("map");
    const viewport = document.getElementById("viewport");
    const tilesLayer = document.getElementById("tiles");
    const route = document.getElementById("route");
    const placesLayer = document.getElementById("places");
    const stopsLayer = document.getElementById("stops");
    const stopList = document.getElementById("stopList");
    const commands = document.getElementById("commands");
    const mapStatus = document.getElementById("mapStatus");
    const width = 1000;
    const height = 700;
    const padding = 70;
    const selected = new Set();
    const originalNames = new Map(data.stops.map((stop) => [stop.alias, stop.name]));
    const originalTags = new Map(data.stops.map((stop) => [stop.alias, (stop.tags || []).join(" ")]));
    const originalNotes = new Map(data.stops.map((stop) => [stop.alias, stop.note || ""]));
    const embeddedTiles = {str(embedded_tile_images).lower()};
    let viewBox = [0, 0, width, height];
    let dragStart = null;
    let stopTapStart = null;
    let suppressStopClick = false;
    const activePointers = new Map();
    let pinchStart = null;
    let currentTileZoom = null;
    let currentTileCount = 0;

    const allPoints = [
      ...data.track,
      ...data.stops.map((stop) => [stop.lat, stop.lon]),
      ...data.namedPlaces.map((place) => [place.lat, place.lon])
    ].map((point) => [Number(point[0]), Number(point[1])]).filter((point) => Number.isFinite(point[0]) && Number.isFinite(point[1]));
    const centerLat = allPoints.length ? allPoints.reduce((sum, point) => sum + point[0], 0) / allPoints.length : 0;
    const cosLat = Math.max(0.2, Math.cos(centerLat * Math.PI / 180));
    const projected = allPoints.map(([lat, lon]) => [lon * cosLat, lat]);
    const minX = projected.length ? Math.min(...projected.map((point) => point[0])) : -1;
    const maxX = projected.length ? Math.max(...projected.map((point) => point[0])) : 1;
    const minY = projected.length ? Math.min(...projected.map((point) => point[1])) : -1;
    const maxY = projected.length ? Math.max(...projected.map((point) => point[1])) : 1;
    const spanX = Math.max(maxX - minX, 0.001);
    const spanY = Math.max(maxY - minY, 0.001);
    const scale = Math.min((width - padding * 2) / spanX, (height - padding * 2) / spanY);

    const project = (lat, lon) => {{
      const latNum = Number(lat);
      const lonNum = Number(lon);
      return [
        padding + ((lonNum * cosLat - minX) * scale) + ((width - padding * 2 - spanX * scale) / 2),
        height - padding - ((latNum - minY) * scale) - ((height - padding * 2 - spanY * scale) / 2)
      ];
    }};
    const offsetX = (width - padding * 2 - spanX * scale) / 2;
    const offsetY = (height - padding * 2 - spanY * scale) / 2;
    const unproject = (x, y) => {{
      const lon = (((x - padding - offsetX) / scale) + minX) / cosLat;
      const lat = minY + ((height - padding - offsetY - y) / scale);
      return [lat, lon];
    }};
    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const latToTileY = (lat, zoomLevel) => {{
      const latRad = clamp(lat, -85.05112878, 85.05112878) * Math.PI / 180;
      return Math.floor((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * (2 ** zoomLevel));
    }};
    const lonToTileX = (lon, zoomLevel) => Math.floor((lon + 180) / 360 * (2 ** zoomLevel));
    const tileLon = (x, zoomLevel) => x / (2 ** zoomLevel) * 360 - 180;
    const tileLat = (y, zoomLevel) => {{
      const n = Math.PI - 2 * Math.PI * y / (2 ** zoomLevel);
      return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
    }};
    const currentGeoBounds = () => {{
      const corners = [
        unproject(viewBox[0], viewBox[1]),
        unproject(viewBox[0] + viewBox[2], viewBox[1]),
        unproject(viewBox[0], viewBox[1] + viewBox[3]),
        unproject(viewBox[0] + viewBox[2], viewBox[1] + viewBox[3])
      ];
      return {{
        minLat: Math.min(...corners.map((point) => point[0])),
        maxLat: Math.max(...corners.map((point) => point[0])),
        minLon: Math.min(...corners.map((point) => point[1])),
        maxLon: Math.max(...corners.map((point) => point[1]))
      }};
    }};
    const drawTiles = () => {{
      if (!allPoints.length) {{
        currentTileZoom = null;
        currentTileCount = 0;
        mapStatus.textContent = "no location data";
        return;
      }}
      if (embeddedTiles) {{
        currentTileZoom = "embedded";
        currentTileCount = tilesLayer.querySelectorAll("image").length;
        mapStatus.textContent = `tiles: embedded · count ${{currentTileCount}}`;
        return;
      }}
      const bounds = currentGeoBounds();
      const lonSpan = Math.max(0.00001, Math.abs(bounds.maxLon - bounds.minLon));
      const deviceBoost = Math.ceil(Math.log2(window.devicePixelRatio || 1));
      const zoomLevel = clamp(Math.round(Math.log2(5 * 360 / lonSpan)) + deviceBoost + 2, 1, 19);
      const maxTile = (2 ** zoomLevel) - 1;
      const minTileX = clamp(lonToTileX(bounds.minLon, zoomLevel) - 1, 0, maxTile);
      const maxTileX = clamp(lonToTileX(bounds.maxLon, zoomLevel) + 1, 0, maxTile);
      const minTileY = clamp(latToTileY(bounds.maxLat, zoomLevel) - 1, 0, maxTile);
      const maxTileY = clamp(latToTileY(bounds.minLat, zoomLevel) + 1, 0, maxTile);
      const pieces = [];
      let sampleTile = "";
      for (let x = minTileX; x <= maxTileX; x += 1) {{
        for (let y = minTileY; y <= maxTileY; y += 1) {{
          if (!sampleTile) sampleTile = `${{zoomLevel}}/${{x}}/${{y}}`;
          const [x1, y1] = project(tileLat(y, zoomLevel), tileLon(x, zoomLevel));
          const [x2, y2] = project(tileLat(y + 1, zoomLevel), tileLon(x + 1, zoomLevel));
          pieces.push(`<image class="tile" href="https://tile.openstreetmap.org/${{zoomLevel}}/${{x}}/${{y}}.png" x="${{Math.min(x1, x2)}}" y="${{Math.min(y1, y2)}}" width="${{Math.abs(x2 - x1)}}" height="${{Math.abs(y2 - y1)}}" preserveAspectRatio="none"></image>`);
        }}
      }}
      tilesLayer.innerHTML = pieces.join("");
      currentTileZoom = zoomLevel;
      currentTileCount = pieces.length;
      mapStatus.textContent = `tile z${{zoomLevel}} · count ${{pieces.length}} · dpr ${{(window.devicePixelRatio || 1).toFixed(1)}} · ${{sampleTile}}`;
    }};
    const distanceMeters = (a, b) => {{
      const radius = 6371008.8;
      const toRad = (value) => value * Math.PI / 180;
      const dLat = toRad(b.lat - a.lat);
      const dLon = toRad(b.lon - a.lon);
      const lat1 = toRad(a.lat);
      const lat2 = toRad(b.lat);
      const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
      return 2 * radius * Math.asin(Math.sqrt(h));
    }};
    const zoomAtLeast = (minimum) => {{
      const zoom = map.getZoom();
      return Number.isFinite(Number(zoom)) ? Math.max(Number(zoom), minimum) : minimum;
    }};
    const setViewBox = () => {{
      svg.setAttribute("viewBox", viewBox.join(" "));
      drawTiles();
    }};
    const zoom = (factor, anchorX = viewBox[0] + viewBox[2] / 2, anchorY = viewBox[1] + viewBox[3] / 2) => {{
      const relX = (anchorX - viewBox[0]) / viewBox[2];
      const relY = (anchorY - viewBox[1]) / viewBox[3];
      viewBox[2] *= factor;
      viewBox[3] *= factor;
      viewBox[0] = anchorX - viewBox[2] * relX;
      viewBox[1] = anchorY - viewBox[3] * relY;
      setViewBox();
    }};
    const clientToSvg = (clientX, clientY) => {{
      const rect = svg.getBoundingClientRect();
      return [
        viewBox[0] + ((clientX - rect.left) / rect.width) * viewBox[2],
        viewBox[1] + ((clientY - rect.top) / rect.height) * viewBox[3]
      ];
    }};
    const pointerDistance = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    const pointerMidpoint = (a, b) => [(a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2];
    const selectedStops = () => data.stops.filter((stop) => selected.has(stop.alias));
    const parseTags = (value) => value.split(/[\\s,]+/).map((item) => item.trim()).filter(Boolean);
    const centerStop = (alias) => {{
      const stop = data.stops.find((item) => item.alias === alias);
      if (!stop) return;
      const [x, y] = project(stop.lat, stop.lon);
      viewBox[0] = x - viewBox[2] / 2;
      viewBox[1] = y - viewBox[3] / 2;
      setViewBox();
    }};
    const updateCommands = () => {{
      const changed = data.stops.filter((stop) =>
        stop.name !== originalNames.get(stop.alias)
        || (stop.tags || []).join(" ") !== originalTags.get(stop.alias)
        || (stop.note || "") !== originalNotes.get(stop.alias)
      );
      commands.value = changed.length
        ? [
            "/otb " + data.date,
            ...changed.map((stop) => {{
              const parts = [`${{stop.alias}} ${{stop.name}}`];
              if ((stop.tags || []).length) parts.push(`tags: ${{stop.tags.join(" ")}}`);
              else if (originalTags.get(stop.alias)) parts.push("tags:");
              if (stop.note) parts.push(`note: ${{stop.note}}`);
              else if (originalNotes.get(stop.alias)) parts.push("note:");
              return parts.join(" | ");
            }})
          ].join("\\n")
        : "";
    }};
    const draw = () => {{
      route.setAttribute("d", data.track.map(([lat, lon], index) => {{
        const [x, y] = project(lat, lon);
        return `${{index ? "L" : "M"}} ${{x.toFixed(1)}} ${{y.toFixed(1)}}`;
      }}).join(" "));
      placesLayer.innerHTML = "";
      for (const place of data.namedPlaces) {{
        const [x, y] = project(place.lat, place.lon);
        const label = `${{place.action || ""}} ${{place.name}}`.trim();
        placesLayer.insertAdjacentHTML("beforeend", `
          <g>
            <circle class="place" cx="${{x}}" cy="${{y}}" r="6"></circle>
            <text class="place-label" x="${{x + 9}}" y="${{y - 9}}">${{escapeHtml(label)}}</text>
          </g>
        `);
      }}
      stopsLayer.innerHTML = "";
      const stopPositions = [];
      for (const stop of data.stops) {{
        const [x, y] = project(stop.lat, stop.lon);
        const closeCount = stopPositions.filter(([existingX, existingY]) => Math.hypot(x - existingX, y - existingY) < 42).length;
        stopPositions.push([x, y]);
        const angle = closeCount * 1.7;
        const labelDx = 16 + (22 * closeCount);
        const labelDy = -31 - (14 * closeCount);
        const spread = Math.min(18 * closeCount, 56);
        const drawX = x + Math.cos(angle) * spread;
        const drawY = y + Math.sin(angle) * spread;
        const label = `${{stop.alias}}: ${{stop.name}}`;
        const labelWidth = Math.max(58, Math.min(260, label.length * 8 + 16));
        const hitX = Math.min(-16, labelDx - 4);
        const hitY = Math.min(-16, labelDy - 4);
        const hitWidth = Math.max(16, labelDx + labelWidth + 4) - hitX;
        const hitHeight = Math.max(16, labelDy + 28) - hitY;
        stopsLayer.insertAdjacentHTML("beforeend", `
          <g class="stop ${{selected.has(stop.alias) ? "selected" : ""}}" data-alias="${{escapeHtml(stop.alias)}}" transform="translate(${{drawX}}, ${{drawY}})">
            <rect class="hit-target" x="${{hitX}}" y="${{hitY}}" width="${{hitWidth}}" height="${{hitHeight}}"></rect>
            <line class="leader" x1="0" y1="0" x2="${{x - drawX}}" y2="${{y - drawY}}"></line>
            <circle r="10"></circle>
            <rect class="label-bg" x="${{labelDx}}" y="${{labelDy}}" width="${{labelWidth}}" height="24"></rect>
            <text class="label-text" x="${{labelDx + 8}}" y="${{labelDy + 17}}">${{escapeHtml(label)}}</text>
          </g>
        `);
      }}
    }};
    const renderList = () => {{
      stopList.innerHTML = data.stops.map((stop) => `
        <div class="stop-row ${{selected.has(stop.alias) ? "selected" : ""}}" data-row="${{escapeHtml(stop.alias)}}">
          <div class="stop-head">
            <input type="checkbox" data-check="${{escapeHtml(stop.alias)}}" ${{selected.has(stop.alias) ? "checked" : ""}}>
            <span class="alias">${{escapeHtml(stop.alias)}}</span>
            <a href="${{escapeHtml(stop.maps)}}" target="_blank" rel="noreferrer">Google Maps</a>
          </div>
          <input data-name="${{escapeHtml(stop.alias)}}" value="${{escapeHtml(stop.name)}}">
          <input data-tags="${{escapeHtml(stop.alias)}}" value="${{escapeHtml((stop.tags || []).join(" "))}}" placeholder="tags">
          <textarea data-note="${{escapeHtml(stop.alias)}}" placeholder="note">${{escapeHtml(stop.note || "")}}</textarea>
          <div class="meta">${{escapeHtml(stop.start)}} to ${{escapeHtml(stop.end)}} · ${{escapeHtml(stop.duration)}} · ${{escapeHtml(stop.points)}} points</div>
        </div>
      `).join("");
      stopList.querySelectorAll("[data-check]").forEach((input) => {{
        input.addEventListener("change", () => setSelected(input.dataset.check, input.checked, true));
      }});
      stopList.querySelectorAll("[data-name]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.name);
          if (stop) {{
            stop.name = input.value.trim() || originalNames.get(stop.alias);
            draw();
            updateCommands();
          }}
        }});
      }});
      stopList.querySelectorAll("[data-tags]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.tags);
          if (stop) {{
            stop.tags = parseTags(input.value);
            updateCommands();
          }}
        }});
      }});
      stopList.querySelectorAll("[data-note]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.note);
          if (stop) {{
            stop.note = input.value.trim();
            updateCommands();
          }}
        }});
      }});
    }};
    const refresh = () => {{
      draw();
      renderList();
      updateCommands();
    }};
    const setSelected = (alias, isSelected, center = false) => {{
      if (isSelected) selected.add(alias);
      else selected.delete(alias);
      refresh();
      if (isSelected && center) centerStop(alias);
    }};
    const toggleStop = (alias) => setSelected(alias, !selected.has(alias), true);
    stopsLayer.addEventListener("pointerdown", (event) => {{
      const node = event.target.closest(".stop");
      if (!node) return;
      stopTapStart = {{ alias: node.dataset.alias, x: event.clientX, y: event.clientY }};
      event.preventDefault();
      event.stopPropagation();
    }});
    stopsLayer.addEventListener("pointerup", (event) => {{
      const node = event.target.closest(".stop");
      if (!node) return;
      event.preventDefault();
      event.stopPropagation();
      const distance = stopTapStart ? Math.hypot(event.clientX - stopTapStart.x, event.clientY - stopTapStart.y) : 999;
      if (stopTapStart && stopTapStart.alias === node.dataset.alias && distance < 10) {{
        toggleStop(node.dataset.alias);
        suppressStopClick = true;
        setTimeout(() => suppressStopClick = false, 350);
      }}
      stopTapStart = null;
    }});
    stopsLayer.addEventListener("pointercancel", () => {{
      stopTapStart = null;
    }});
    stopsLayer.addEventListener("click", (event) => {{
      const node = event.target.closest(".stop");
      if (!node) return;
      event.preventDefault();
      event.stopPropagation();
      if (suppressStopClick) return;
      toggleStop(node.dataset.alias);
    }});
    document.getElementById("selectAll").addEventListener("click", () => {{
      data.stops.forEach((stop) => selected.add(stop.alias));
      refresh();
    }});
    document.getElementById("clearSelection").addEventListener("click", () => {{
      selected.clear();
      refresh();
    }});
    document.getElementById("centerSelected").addEventListener("click", () => {{
      const stop = selectedStops()[0];
      if (stop) centerStop(stop.alias);
    }});
    document.getElementById("applyName").addEventListener("click", () => {{
      const value = document.getElementById("bulkName").value.trim();
      if (!value) return;
      selectedStops().forEach((stop) => stop.name = value);
      refresh();
    }});
    document.getElementById("resetNames").addEventListener("click", () => {{
      data.stops.forEach((stop) => stop.name = originalNames.get(stop.alias));
      selected.clear();
      refresh();
    }});
    document.getElementById("selectNearby").addEventListener("click", () => {{
      const picked = selectedStops()[0] || data.stops[0];
      const threshold = Number(document.getElementById("groupDistance").value) || 150;
      if (!picked) return;
      selected.clear();
      data.stops.filter((stop) => distanceMeters(picked, stop) <= threshold).forEach((stop) => selected.add(stop.alias));
      refresh();
    }});
    document.getElementById("groupSelected").addEventListener("click", () => {{
      const stops = selectedStops();
      if (!stops.length) return;
      const existing = stops.find((stop) => !/^unnamed-stop-\\d+$/.test(stop.name));
      const name = document.getElementById("bulkName").value.trim() || (existing ? existing.name : stops[0].name);
      stops.forEach((stop) => stop.name = name);
      document.getElementById("bulkName").value = name;
      refresh();
    }});
    document.getElementById("refreshCommands").addEventListener("click", updateCommands);
    document.getElementById("copyCommands").addEventListener("click", async () => {{
      updateCommands();
      commands.select();
      try {{
        await navigator.clipboard.writeText(commands.value);
      }} catch (error) {{
        document.execCommand("copy");
      }}
    }});
    document.getElementById("zoomIn").addEventListener("click", () => zoom(0.75));
    document.getElementById("zoomOut").addEventListener("click", () => zoom(1.25));
    document.getElementById("resetView").addEventListener("click", () => {{
      viewBox = [0, 0, width, height];
      setViewBox();
    }});
    svg.addEventListener("pointerdown", (event) => {{
      activePointers.set(event.pointerId, event);
      if (activePointers.size === 2) {{
        const [a, b] = [...activePointers.values()];
        const [midX, midY] = pointerMidpoint(a, b);
        const [anchorX, anchorY] = clientToSvg(midX, midY);
        pinchStart = {{
          distance: pointerDistance(a, b),
          anchorX,
          anchorY,
          viewBox: [...viewBox]
        }};
        dragStart = null;
      }} else {{
        dragStart = {{ x: event.clientX, y: event.clientY, viewBox: [...viewBox] }};
      }}
      svg.setPointerCapture(event.pointerId);
    }});
    svg.addEventListener("pointermove", (event) => {{
      if (!activePointers.has(event.pointerId)) return;
      activePointers.set(event.pointerId, event);
      if (activePointers.size === 2 && pinchStart) {{
        const [a, b] = [...activePointers.values()];
        const factor = clamp(pinchStart.distance / Math.max(1, pointerDistance(a, b)), 0.2, 5);
        viewBox = [...pinchStart.viewBox];
        zoom(factor, pinchStart.anchorX, pinchStart.anchorY);
        return;
      }}
      if (!dragStart) return;
      const rect = svg.getBoundingClientRect();
      viewBox[0] = dragStart.viewBox[0] - ((event.clientX - dragStart.x) / rect.width) * viewBox[2];
      viewBox[1] = dragStart.viewBox[1] - ((event.clientY - dragStart.y) / rect.height) * viewBox[3];
      setViewBox();
    }});
    const endPointer = (event) => {{
      activePointers.delete(event.pointerId);
      pinchStart = null;
      dragStart = null;
    }};
    svg.addEventListener("pointerup", endPointer);
    svg.addEventListener("pointercancel", endPointer);
    svg.addEventListener("wheel", (event) => {{
      event.preventDefault();
      const [x, y] = clientToSvg(event.clientX, event.clientY);
      zoom(event.deltaY < 0 ? 0.85 : 1.15, x, y);
    }}, {{ passive: false }});
    setViewBox();
    refresh();
  </script>
</body>
</html>"""


def build_heatmap_summary(
    events: list[Event],
    scope: OwnTracksScope,
    user_tags: dict | None = None,
    home_filter: HomeFilterConfig | None = None,
) -> dict:
    scope_events = [event for event in events if (event_date(event) is not None and scope.start_date <= event_date(event) <= scope.end_date)]
    raw_location_points = [event for event in scope_events if event.is_location]
    anchors = home_anchors(events, home_filter)
    location_points, filtered_home_points = filter_home_area_points(raw_location_points, home_filter, anchors)
    day_points: dict[date, list[Event]] = {}
    buckets: Counter[tuple[float, float]] = Counter()
    bucket_minutes: Counter[tuple[float, float]] = Counter()
    bucket_visits: Counter[tuple[float, float]] = Counter()
    bucket_visit_minutes: Counter[tuple[float, float]] = Counter()
    bucket_modes: dict[tuple[float, float], Counter[str]] = {}
    label_sources = heatmap_label_sources(events, scope, user_tags or {})
    mode_points: Counter[str] = Counter()
    mode_distance: Counter[str] = Counter()
    previous: Event | None = None
    for event in location_points:
        if event.lat is None or event.lon is None:
            continue
        day = event_date(event)
        if day is not None:
            day_points.setdefault(day, []).append(event)
        mode = motion_mode(event)
        mode_points[mode] += 1
        if previous and previous.lat is not None and previous.lon is not None:
            segment = haversine_km(previous.lat, previous.lon, event.lat, event.lon)
            if segment <= 5:
                mode_distance[mode] += segment
            previous_day = event_date(previous)
            elapsed_minutes = max(0, (event_time(event) - event_time(previous)).total_seconds() / 60)
            if previous_day == day and elapsed_minutes <= 12 * 60:
                previous_mode = motion_mode(previous)
                previous_bucket = (round(previous.lat, 4), round(previous.lon, 4))
                if previous_mode == "stationary" or segment <= 0.2:
                    bucket_minutes[previous_bucket] += min(elapsed_minutes, 60)
        previous = event
        bucket = (round(event.lat, 4), round(event.lon, 4))
        buckets[bucket] += 1
        bucket_modes.setdefault(bucket, Counter())[mode] += 1

    for points in day_points.values():
        for visit in heatmap_visit_clusters(points):
            bucket = (round(visit["lat"], 4), round(visit["lon"], 4))
            bucket_visits[bucket] += 1
            bucket_visit_minutes[bucket] += int(visit["duration_minutes"])
            bucket_modes.setdefault(bucket, Counter())[str(visit.get("mode") or "stationary")] += 1

    heat_points: list[dict] = []
    hotspots: list[dict] = []
    all_buckets = set(buckets) | set(bucket_minutes) | set(bucket_visits)
    for lat, lon in sorted(all_buckets, key=lambda item: (-max(bucket_minutes[item], buckets[item], bucket_visits[item]), item)):
        count = buckets[(lat, lon)]
        duration_minutes = int(round(bucket_minutes[(lat, lon)] or bucket_visit_minutes[(lat, lon)]))
        visit_count = bucket_visits[(lat, lon)]
        match = best_heatmap_match(lat, lon, label_sources)
        label = match.get("label") if match else None
        display_label = label or f"{lat:.4f}, {lon:.4f}"
        tags = match.get("tags", []) if match else []
        dominant_mode = bucket_modes[(lat, lon)].most_common(1)[0][0] if bucket_modes.get((lat, lon)) else "moving"
        heat_point = {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "weight": count,
            "duration_minutes": duration_minutes,
            "visit_count": visit_count,
            "label": display_label,
            "tags": tags,
            "mode": dominant_mode,
        }
        heat_points.append(heat_point)
        hotspots.append(
            {
                "lat": heat_point["lat"],
                "lon": heat_point["lon"],
                "count": count,
                "duration_minutes": duration_minutes,
                "visit_count": visit_count,
                "label": display_label,
                "tags": tags,
            }
        )

    least_visited = sorted(hotspots, key=lambda item: (item["duration_minutes"], item["label"]))[:10]
    most_visited = sorted(hotspots, key=lambda item: (-item["duration_minutes"], item["label"]))[:10]
    total_distance_km = round(sum(summarize_distance(points) for points in day_points.values()), 2)
    return {
        "title": f"OwnTracks heatmap - {scope.value}",
        "scope": {
            "kind": scope.kind,
            "value": scope.value,
            "start": scope.start_date.isoformat(),
            "end": scope.end_date.isoformat(),
        },
        "stats": {
            "days_with_points": len(day_points),
            "location_points": len(location_points),
            "raw_location_points": len(raw_location_points),
            "filtered_home_points": filtered_home_points,
            "unique_locations": len(all_buckets),
            "max_visits": max(bucket_visits.values()) if bucket_visits else 0,
            "min_visits": min(bucket_visits.values()) if bucket_visits else 0,
            "max_time_minutes": int(max(bucket_minutes.values())) if bucket_minutes else 0,
            "sampled_distance_km": total_distance_km,
        },
        "motion_summary": {
            "counts": dict(mode_points),
            "distance_km": {mode: round(distance, 2) for mode, distance in mode_distance.items()},
            "dominant": mode_points.most_common(1)[0][0] if mode_points else "unknown",
        },
        "heat_points": heat_points,
        "most_visited": most_visited,
        "least_visited": least_visited,
        "home_filter": {
            "enabled": bool(home_filter and home_filter.enabled),
            "radius_m": home_filter.radius_m if home_filter and home_filter.enabled else None,
            "anchor_count": len(anchors),
            "filtered_points": filtered_home_points,
        },
    }


def heatmap_label_sources(events: list[Event], scope: OwnTracksScope, user_tags: dict) -> list[dict]:
    sources: list[dict] = []
    day_events: dict[date, list[Event]] = {}
    for event in events:
        day = event_date(event)
        if day is None or day < scope.start_date or day > scope.end_date or not event.is_location:
            continue
        day_events.setdefault(day, []).append(event)
    for day, scoped_events in day_events.items():
        plan, _track_points = build_plan(scoped_events, day, user_tags)
        for stop in plan.get("candidate_stops", []):
            label = str(stop.get("reviewed_name") or stop.get("name") or "").strip()
            if not label:
                continue
            sources.append(
                {
                    "lat": stop.get("lat"),
                    "lon": stop.get("lon"),
                    "label": label,
                    "tags": list(stop.get("user_tags") or stop.get("tags") or []),
                    "mode": str(stop.get("motion_mode") or "stationary"),
                    "priority": 4,
                    "date": day.isoformat(),
                }
            )
        for place in plan.get("named_places", []):
            label = str(place.get("name") or "").strip()
            if not label:
                continue
            sources.append(
                {
                    "lat": place.get("lat"),
                    "lon": place.get("lon"),
                    "label": label,
                    "tags": list(place.get("tags") or []),
                    "mode": "moving",
                    "priority": 3,
                    "date": day.isoformat(),
                }
            )
    for event in events:
        if event.kind != "transition":
            continue
        label = str(event.payload.get("desc") or "").strip()
        if not label or event.lat is None or event.lon is None:
            continue
        day = event_date(event)
        if day is None or day > scope.end_date:
            continue
        sources.append(
            {
                "lat": event.lat,
                "lon": event.lon,
                "label": label,
                "tags": [f"place:{slug(label)}", f"geofence:{event.payload.get('event')}"],
                "mode": "moving",
                "priority": 2,
                "date": day.isoformat(),
            }
        )
    return sources


def best_heatmap_match(lat: float, lon: float, sources: list[dict], radius_m: int = 400) -> dict | None:
    best: tuple[int, str, float, str] | None = None
    best_source: dict | None = None
    for source in sources:
        source_lat = as_float(source.get("lat"))
        source_lon = as_float(source.get("lon"))
        label = str(source.get("label") or "").strip()
        if source_lat is None or source_lon is None or not label:
            continue
        distance_m = haversine_km(lat, lon, source_lat, source_lon) * 1000
        if distance_m > radius_m:
            continue
        priority = int(source.get("priority") or 0)
        date_key = str(source.get("date") or "")
        candidate = (-priority, date_key, distance_m, label.lower())
        if best is None or candidate < best:
            best = candidate
            tags = source.get("tags") or []
            best_source = {
                "label": label,
                "tags": [str(tag) for tag in tags if str(tag).strip()],
            }
    return best_source


def build_sample_heatmap_summary() -> dict:
    points: list[dict] = []

    def add_cluster(
        label: str,
        lat: float,
        lon: float,
        visits: list[int],
        tags: list[str],
        spread: float,
    ) -> None:
        for index, count in enumerate(visits):
            row = (index % 5) - 2
            col = (index // 5) - 2
            points.append(
                {
                    "lat": round(lat + row * spread, 6),
                    "lon": round(lon + col * spread, 6),
                    "weight": count,
                    "duration_minutes": count * 12,
                    "visit_count": max(1, round(count / 8)),
                    "label": label if index == 0 else f"{label} area {index + 1}",
                    "tags": tags,
                    "mode": "stationary",
                }
            )

    add_cluster("Bengaluru errands", 12.9716, 77.5946, [95, 72, 48, 27, 18, 12], ["city", "india", "errands"], 0.018)
    add_cluster("Mumbai work travel", 19.0760, 72.8777, [64, 41, 22, 13], ["city", "india", "work"], 0.025)
    add_cluster("Delhi airport loop", 28.5562, 77.1000, [52, 36, 19], ["city", "india", "airport"], 0.02)
    add_cluster("London commute", 51.5072, -0.1276, [44, 31, 20, 8], ["city", "uk", "commute"], 0.03)
    add_cluster("New York trip", 40.7128, -74.0060, [58, 33, 16, 9], ["city", "usa", "travel"], 0.035)
    add_cluster("San Francisco visit", 37.7749, -122.4194, [38, 21, 11], ["city", "usa", "travel"], 0.028)
    add_cluster("Tokyo vacation", 35.6762, 139.6503, [46, 29, 14], ["city", "japan", "vacation"], 0.025)
    add_cluster("Singapore stopover", 1.3521, 103.8198, [34, 18, 8], ["city", "singapore", "airport"], 0.018)
    add_cluster("Sydney holiday", -33.8688, 151.2093, [26, 15, 6], ["city", "australia", "vacation"], 0.03)
    add_cluster("Sao Paulo conference", -23.5558, -46.6396, [23, 12, 5], ["city", "brazil", "work"], 0.03)
    add_cluster("Cape Town visit", -33.9249, 18.4241, [19, 10, 4], ["city", "south-africa", "travel"], 0.025)

    most_visited = sorted(
        [
            {
                "lat": p["lat"],
                "lon": p["lon"],
                "count": p["weight"],
                "duration_minutes": p["duration_minutes"],
                "visit_count": p["visit_count"],
                "label": p["label"],
                "tags": p["tags"],
            }
            for p in points
        ],
        key=lambda item: (-item["duration_minutes"], item["label"]),
    )[:10]
    least_visited = sorted(
        [
            {
                "lat": p["lat"],
                "lon": p["lon"],
                "count": p["weight"],
                "duration_minutes": p["duration_minutes"],
                "visit_count": p["visit_count"],
                "label": p["label"],
                "tags": p["tags"],
            }
            for p in points
        ],
        key=lambda item: (item["duration_minutes"], item["label"]),
    )[:10]
    return {
        "title": "OwnTracks sample heatmap",
        "scope": {
            "kind": "sample",
            "value": "sample",
            "start": "sample",
            "end": "sample",
        },
        "stats": {
            "days_with_points": 90,
            "location_points": sum(int(point["weight"]) for point in points),
            "unique_locations": len(points),
            "max_visits": max(int(point["visit_count"]) for point in points),
            "min_visits": min(int(point["visit_count"]) for point in points),
            "max_time_minutes": max(int(point["duration_minutes"]) for point in points),
            "sampled_distance_km": 0,
        },
        "heat_points": points,
        "most_visited": most_visited,
        "least_visited": least_visited,
    }


def render_heatmap_html(summary: dict) -> str:
    payload = json.dumps(summary, ensure_ascii=False).replace("</", "<\\/")
    title = escape(summary["title"])
    scope = summary["scope"]
    stats = summary["stats"]
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{
      height: 100%;
      margin: 0;
    }}
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f8fafc;
    }}
    #map {{
      background: #e2e8f0;
    }}
    .panel {{
      background: rgb(255 255 255 / 0.96);
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      box-shadow: 0 2px 12px rgb(15 23 42 / 0.16);
      left: 10px;
      max-height: calc(100vh - 20px);
      max-width: min(380px, calc(100vw - 20px));
      overflow: auto;
      padding: 10px;
      position: absolute;
      top: 10px;
      z-index: 1000;
    }}
    .panel-header {{
      align-items: center;
      display: flex;
      gap: 8px;
      justify-content: space-between;
      margin-bottom: 6px;
    }}
    .panel h1 {{
      font-size: 16px;
      margin: 0;
    }}
    .panel-toggle {{
      appearance: none;
      background: #0f172a;
      border: 0;
      border-radius: 6px;
      color: white;
      cursor: pointer;
      flex: 0 0 auto;
      font-size: 12px;
      font-weight: 800;
      padding: 6px 10px;
    }}
    .panel-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }}
    .panel-action {{
      appearance: none;
      background: #e2e8f0;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      color: #0f172a;
      cursor: pointer;
      font-size: 12px;
      font-weight: 700;
      padding: 6px 10px;
    }}
    .panel-action.active {{
      background: #0f172a;
      border-color: #0f172a;
      color: white;
    }}
    .mode-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0 0 6px;
    }}
    .mode-chip {{
      appearance: none;
      background: #e2e8f0;
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      color: #0f172a;
      cursor: pointer;
      font-size: 12px;
      font-weight: 700;
      padding: 5px 9px;
    }}
    .mode-chip.active {{
      background: #0f172a;
      border-color: #0f172a;
      color: white;
    }}
    .mode-summary-text {{
      color: #475569;
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .panel-body {{
      display: block;
    }}
    .panel.collapsed {{
      max-height: none;
      overflow: visible;
      padding: 10px;
      width: auto;
    }}
    .panel.collapsed .panel-body {{
      display: none;
    }}
    .panel .subtle {{
      color: #475569;
      font-size: 12px;
      line-height: 1.4;
      margin-bottom: 8px;
    }}
    .stat-grid {{
      display: grid;
      gap: 6px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-bottom: 10px;
    }}
    .stat {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      padding: 6px 8px;
    }}
    .stat .label {{
      color: #64748b;
      display: block;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .stat .value {{
      color: #0f172a;
      font-size: 15px;
      font-weight: 800;
      margin-top: 2px;
    }}
    .list {{
      margin-top: 10px;
    }}
    .list h2 {{
      font-size: 13px;
      margin: 0 0 6px;
    }}
    .spot {{
      align-items: center;
      background: #fff;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      cursor: pointer;
      display: flex;
      gap: 8px;
      justify-content: space-between;
      margin-bottom: 6px;
      padding: 6px 8px;
    }}
    .spot:hover {{
      border-color: #94a3b8;
    }}
    .spot .name {{
      font-size: 12px;
      font-weight: 700;
    }}
    .spot .count {{
      background: #0f172a;
      border-radius: 999px;
      color: white;
      font-size: 11px;
      font-weight: 800;
      min-width: 28px;
      padding: 2px 6px;
      text-align: center;
    }}
    .empty {{
      background: white;
      border-radius: 8px;
      left: 50%;
      padding: 16px 18px;
      position: absolute;
      text-align: center;
      top: 50%;
      transform: translate(-50%, -50%);
      z-index: 999;
    }}
    .leaflet-control-scale {{
      margin-bottom: 14px !important;
      margin-right: 14px !important;
    }}
    .leaflet-control-scale-line {{
      background: rgb(255 255 255 / 0.92);
      border-color: #111827;
      border-width: 0 2px 2px;
      box-shadow: 0 1px 5px rgb(15 23 42 / 0.22);
      color: #111827;
      font-size: 12px;
      font-weight: 800;
    }}
    .heatmap-legend {{
      background: rgb(255 255 255 / 0.94);
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      box-shadow: 0 2px 10px rgb(15 23 42 / 0.12);
      color: #0f172a;
      font-size: 12px;
      line-height: 1.35;
      padding: 8px 10px;
    }}
    .heatmap-legend .title {{
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0;
      margin-bottom: 6px;
      text-transform: uppercase;
    }}
    .heatmap-legend .row {{
      align-items: center;
      display: flex;
      gap: 8px;
      margin-top: 4px;
      white-space: nowrap;
    }}
    .heatmap-legend .swatch {{
      border-radius: 4px;
      display: inline-block;
      flex: 0 0 auto;
      height: 10px;
      width: 36px;
    }}
    .place-label {{
      background: rgb(15 23 42 / 0.92);
      border: 0;
      border-radius: 6px;
      color: white;
      font-size: 11px;
      font-weight: 700;
      padding: 3px 6px;
      box-shadow: 0 1px 4px rgb(15 23 42 / 0.22);
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div class="panel" id="heatmapPanel">
    <div class="panel-header">
      <h1>{title}</h1>
      <button type="button" id="toggleHeatmapPanel" class="panel-toggle">Hide</button>
    </div>
    <div class="panel-body">
      <div class="subtle">{scope["start"]} to {scope["end"]}</div>
      <div class="panel-actions">
        <button type="button" class="panel-action active" data-heat-metric="time">Time spent</button>
        <button type="button" class="panel-action" data-heat-metric="visits">Visits</button>
        <button type="button" class="panel-action" data-heat-metric="raw">Raw points</button>
        <button type="button" id="toggleHeatmapPoints" class="panel-action">Show points</button>
      </div>
      <div class="mode-summary" id="modeSummary"></div>
      <div class="mode-summary-text" id="modeSummaryText"></div>
      <div class="stat-grid">
        <div class="stat"><span class="label">Days</span><span class="value">{stats["days_with_points"]}</span></div>
        <div class="stat"><span class="label">Points</span><span class="value">{stats["location_points"]}</span></div>
        <div class="stat"><span class="label">Locations</span><span class="value">{stats["unique_locations"]}</span></div>
        <div class="stat"><span class="label">Max visits</span><span class="value">{stats["max_visits"]}</span></div>
        <div class="stat"><span class="label">Min visits</span><span class="value">{stats["min_visits"]}</span></div>
        <div class="stat"><span class="label">Distance</span><span class="value">{stats["sampled_distance_km"]} km</span></div>
      </div>
      <div class="list">
        <h2 id="mostVisitedTitle">Most time spent</h2>
        <div id="mostVisited"></div>
      </div>
      <div class="list">
        <h2 id="leastVisitedTitle">Least time spent</h2>
        <div id="leastVisited"></div>
      </div>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
  <script>
    const data = {payload};
    const allSpots = data.heat_points.map((item) => ({{
      lat: item.lat,
      lon: item.lon,
      rawCount: item.weight || 0,
      durationMinutes: item.duration_minutes || 0,
      visitCount: item.visit_count || 0,
      label: item.label || `${{item.lat}}, ${{item.lon}}`,
      tags: item.tags || [],
      mode: item.mode || "moving",
    }}));
    const map = L.map("map", {{ preferCanvas: true, zoomControl: false }});
    const panel = document.getElementById("heatmapPanel");
    const togglePanelButton = document.getElementById("toggleHeatmapPanel");
    const togglePointsButton = document.getElementById("toggleHeatmapPoints");
    const modeSummary = document.getElementById("modeSummary");
    const modeSummaryText = document.getElementById("modeSummaryText");
    const pointLayer = L.layerGroup();
    let pointsVisible = false;
    let filteredSpots = allSpots;
    let activeMode = "all";
    let activeMetric = "time";
    const heatMetrics = {{
      time: {{
        title: "Time spent",
        highLabel: "Most time",
        mostTitle: "Most time spent",
        leastTitle: "Least time spent",
        value: (spot) => spot.durationMinutes || Math.min(spot.rawCount * 5, 60),
        format: (value) => `${{Math.round(value)}} min`,
      }},
      visits: {{
        title: "Visits",
        highLabel: "Most visits",
        mostTitle: "Most visits",
        leastTitle: "Fewest visits",
        value: (spot) => spot.visitCount || 0,
        format: (value) => `${{Math.round(value)}} visits`,
      }},
      raw: {{
        title: "Raw points",
        highLabel: "Most points",
        mostTitle: "Most raw points",
        leastTitle: "Fewest raw points",
        value: (spot) => spot.rawCount || 0,
        format: (value) => `${{Math.round(value)}} points`,
      }},
    }};
    const metricConfig = () => heatMetrics[activeMetric] || heatMetrics.time;
    const metricValue = (spot) => metricConfig().value(spot);
    const metricLabel = (spot) => metricConfig().format(metricValue(spot));
    L.control.zoom({{ position: "bottomright" }}).addTo(map);
    L.control.scale({{ position: "bottomright", metric: true, imperial: false, maxWidth: 160 }}).addTo(map);
    const legend = L.control({{ position: "bottomleft" }});
    legend.onAdd = () => {{
      const el = L.DomUtil.create("div", "heatmap-legend");
      el.innerHTML = `
        <div class="title">Heat intensity</div>
        <div class="row"><span class="swatch" style="background: #0b3d91;"></span><span>Low</span></div>
        <div class="row"><span class="swatch" style="background: #00bcd4;"></span><span>Medium</span></div>
        <div class="row"><span class="swatch" style="background: #ff9800;"></span><span>High</span></div>
        <div class="row"><span class="swatch" style="background: #d32f2f;"></span><span id="heatLegendHigh">Most time</span></div>
      `;
      return el;
    }};
    legend.addTo(map);
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
      opacity: 0.95,
    }}).addTo(map);
    const heat = L.heatLayer([], {{
      radius: 28,
      blur: 20,
      maxZoom: 17,
      minOpacity: 0.25,
      gradient: {{
        0.15: "#0b3d91",
        0.45: "#00bcd4",
        0.75: "#ff9800",
        1.0: "#d32f2f",
      }},
      }}).addTo(map);
    const centerAndZoom = (spot) => {{
      map.setView([spot.lat, spot.lon], Math.max(map.getZoom(), 14), {{ animate: true }});
    }};
    const makeSpot = (spot) => {{
      const value = metricValue(spot);
      const marker = L.circleMarker([spot.lat, spot.lon], {{
        radius: Math.min(16, 5 + Math.log2(value + 1) * 2.5),
        color: "#1e3a8a",
        weight: 2,
        fillColor: "#3b82f6",
        fillOpacity: 0.78,
      }});
      marker.bindTooltip(`${{spot.label}} · ${{metricLabel(spot)}}`, {{ permanent: false, direction: "right", className: "place-label" }});
      marker.bindPopup(`<strong>${{spot.label}}</strong><br>${{metricConfig().title}}: ${{metricLabel(spot)}}<br>Visits: ${{spot.visitCount || 0}}<br>Raw points: ${{spot.rawCount || 0}}`);
      marker.on("click", () => {{
        centerAndZoom(spot);
        marker.openPopup();
      }});
      pointLayer.addLayer(marker);
    }};
    const topSpots = (items) => [...items].sort((a, b) => metricValue(b) - metricValue(a) || a.label.localeCompare(b.label)).slice(0, 10);
    const leastSpots = (items) => [...items].filter((spot) => metricValue(spot) > 0).sort((a, b) => metricValue(a) - metricValue(b) || a.label.localeCompare(b.label)).slice(0, 10);
    const refreshPointLayer = (items) => {{
      pointLayer.clearLayers();
      items.forEach((spot) => makeSpot(spot));
    }};
    const fitToSpots = (items) => {{
      if (!items.length) return;
      map.fitBounds(L.latLngBounds(items.map((item) => [item.lat, item.lon])).pad(0.2));
    }};
    const syncPanelButton = () => {{
      togglePanelButton.textContent = panel.classList.contains("collapsed") ? "Show" : "Hide";
    }};
    const syncPointsButton = () => {{
      togglePointsButton.textContent = pointsVisible ? "Hide points" : "Show points";
      togglePointsButton.classList.toggle("active", pointsVisible);
    }};
    const syncMetricButtons = () => {{
      document.querySelectorAll("[data-heat-metric]").forEach((button) => {{
        button.classList.toggle("active", button.dataset.heatMetric === activeMetric);
      }});
      document.getElementById("mostVisitedTitle").textContent = metricConfig().mostTitle;
      document.getElementById("leastVisitedTitle").textContent = metricConfig().leastTitle;
      const high = document.getElementById("heatLegendHigh");
      if (high) high.textContent = metricConfig().highLabel;
    }};
    const motionModes = ["all", "stationary", "walking", "cycling", "automotive", "moving"];
    const motionSummary = data.motion_summary || {{}};
    const modeCounts = motionSummary.counts || {{}};
    const modeDistances = motionSummary.distance_km || {{}};
    const renderModeSummary = () => {{
      modeSummary.innerHTML = motionModes.map((mode) => {{
        const count = mode === "all" ? allSpots.length : (modeCounts[mode] || 0);
        const label = mode === "all" ? `All (${{allSpots.length}})` : `${{mode}} (${{count}})`;
        return `<button type="button" class="mode-chip ${{activeMode === mode ? "active" : ""}}" data-mode="${{mode}}">${{label}}</button>`;
      }}).join("");
      modeSummary.querySelectorAll("[data-mode]").forEach((button) => {{
        button.addEventListener("click", () => {{
          activeMode = button.dataset.mode;
          applyFilter(true);
        }});
      }});
      const dominant = motionSummary.dominant || "unknown";
      const distanceBits = motionModes
        .filter((mode) => mode !== "all" && modeDistances[mode])
        .map((mode) => `${{mode}}: ${{modeDistances[mode]}} km`);
      modeSummaryText.textContent = distanceBits.length
        ? `Dominant motion: ${{dominant}} · ${{distanceBits.join(" · ")}}`
        : `Dominant motion: ${{dominant}}`;
    }};
    const modeMatches = (spot) => activeMode === "all" || (spot.mode || "moving") === activeMode;
    const setPointsVisible = (visible) => {{
      pointsVisible = visible;
      if (pointsVisible) {{
        pointLayer.addTo(map);
      }} else {{
        pointLayer.removeFrom(map);
      }}
      syncPointsButton();
    }};
    const applyFilter = (fit = false) => {{
      filteredSpots = allSpots.filter((spot) => modeMatches(spot));
      const weightedSpots = filteredSpots
        .map((spot) => [spot.lat, spot.lon, metricValue(spot)])
        .filter((spot) => spot[2] > 0);
      heat.setLatLngs(weightedSpots);
      if (heat.redraw) heat.redraw();
      refreshPointLayer(filteredSpots);
      listFor(topSpots(filteredSpots), "mostVisited");
      listFor(leastSpots(filteredSpots), "leastVisited");
      syncMetricButtons();
      renderModeSummary();
      if (fit) fitToSpots(filteredSpots);
    }};
    togglePointsButton.addEventListener("click", () => setPointsVisible(!pointsVisible));
    document.querySelectorAll("[data-heat-metric]").forEach((button) => {{
      button.addEventListener("click", () => {{
        activeMetric = button.dataset.heatMetric || "time";
        applyFilter(false);
      }});
    }});
    togglePanelButton.addEventListener("click", () => {{
      panel.classList.toggle("collapsed");
      syncPanelButton();
    }});
    if (window.matchMedia("(max-width: 800px)").matches) {{
      panel.classList.add("collapsed");
    }}
    setPointsVisible(false);
    syncPanelButton();
    renderModeSummary();
    const listFor = (items, target) => {{
      const root = document.getElementById(target);
      if (!items.length) {{
        root.innerHTML = `<div class="spot"><div class="name">No matches</div><div class="count">0</div></div>`;
        return;
      }}
      root.innerHTML = items.map((spot) => `
        <div class="spot" data-lat="${{spot.lat}}" data-lon="${{spot.lon}}">
          <div class="name">${{spot.label}}</div>
          <div class="count">${{metricLabel(spot)}}</div>
        </div>
      `).join("");
      root.querySelectorAll(".spot").forEach((row) => {{
        row.addEventListener("click", () => {{
          const lat = Number(row.dataset.lat);
          const lon = Number(row.dataset.lon);
          map.setView([lat, lon], Math.max(map.getZoom(), 14), {{ animate: true }});
        }});
      }});
    }};
    applyFilter(false);
    if (filteredSpots.length) {{
      fitToSpots(filteredSpots);
    }} else {{
      map.setView([0, 0], 2);
      document.body.insertAdjacentHTML("beforeend", `<div class="empty"><strong>No heatmap points for ${{data.scope.value}}</strong><br>Try a different filter, month, or year.</div>`);
    }}
  </script>
</body>
</html>"""


def render_leaflet_map_html(plan: dict) -> str:
    title = f"OwnTracks map - {plan['date']}"
    track = [
        [point["lat"], point["lon"]]
        for point in plan.get("sampled_track", [])
        if point.get("lat") is not None and point.get("lon") is not None
    ]
    stops = [
        {
            "alias": stop.get("alias", ""),
            "name": stop.get("reviewed_name") or stop.get("name") or "unnamed stop",
            "id": stop.get("id", ""),
            "lat": stop.get("lat"),
            "lon": stop.get("lon"),
            "start": stop.get("start", ""),
            "end": stop.get("end", ""),
            "duration": stop.get("duration", ""),
            "points": stop.get("points", ""),
            "maps": stop.get("maps", ""),
            "tags": stop.get("user_tags", []),
            "note": stop.get("user_note", ""),
        }
        for stop in plan.get("candidate_stops", [])
        if stop.get("lat") is not None and stop.get("lon") is not None
    ]
    named_places = [
        {
            "name": place.get("name", ""),
            "action": place.get("action", ""),
            "lat": place.get("lat"),
            "lon": place.get("lon"),
            "time": place.get("time", ""),
        }
        for place in plan.get("named_places", [])
        if place.get("lat") is not None and place.get("lon") is not None
    ]
    motion_summary = plan.get("motion_summary") or {}
    motion_counts = motion_summary.get("counts") or {}
    motion_dom = motion_summary.get("dominant") or "unknown"
    motion_chips = ['<button type="button" class="motion-chip all active" data-motion-mode="all"><span class="dot"></span>all</button>']
    for mode in ("stationary", "walking", "cycling", "automotive", "moving"):
        count = motion_counts.get(mode)
        if count:
            motion_chips.append(f'<button type="button" class="motion-chip {escape(mode)}" data-motion-mode="{escape(mode)}"><span class="dot"></span>{escape(mode)}: {count}</button>')
    if motion_chips:
        motion_chips.insert(1, f'<span class="motion-chip dominant"><span class="dot"></span>dominant: {escape(motion_dom)}</span>')
    payload = json.dumps(
        {
            "date": plan["date"],
            "track": track,
            "rawSampledTrack": plan.get("raw_sampled_track", plan.get("sampled_track", [])),
            "sampledTrack": plan.get("sampled_track", []),
            "stops": stops,
            "namedPlaces": named_places,
            "rideSegments": plan.get("ride_segments", []),
            "motionSummary": motion_summary,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    escaped_title = escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{
      height: 100%;
      margin: 0;
    }}
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    #map {{
      background: #dbeafe;
    }}
    .tools {{
      background: rgb(255 255 255 / 0.94);
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      box-shadow: 0 2px 12px rgb(15 23 42 / 0.18);
      left: 10px;
      max-height: calc(100vh - 20px);
      max-width: min(420px, calc(100vw - 20px));
      overflow: auto;
      padding: 10px;
      position: absolute;
      top: 10px;
      z-index: 1000;
    }}
    .tools.collapsed {{
      overflow: hidden;
      padding: 7px;
      width: auto;
    }}
    .tools.collapsed .tools-body {{
      display: none;
    }}
    .tools.collapsed h1 {{
      display: none;
    }}
    .tools-title {{
      align-items: center;
      display: flex;
      gap: 8px;
      justify-content: space-between;
    }}
    .tools h1 {{
      font-size: 15px;
      margin: 0;
    }}
    .tools-body {{
      margin-top: 8px;
    }}
    .tools textarea, .tools input {{
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font: inherit;
      padding: 7px;
      width: 100%;
    }}
    .tools textarea {{
      height: 72px;
      margin-top: 8px;
    }}
    .tools label {{
      color: #374151;
      display: block;
      font-size: 12px;
      font-weight: 750;
      margin: 8px 0 4px;
    }}
    .row {{
      display: flex;
      gap: 6px;
      margin-top: 8px;
    }}
    .row > * {{
      flex: 1;
    }}
    button {{
      background: #111827;
      border: 0;
      border-radius: 6px;
      color: white;
      font: inherit;
      font-size: 12px;
      font-weight: 700;
      padding: 7px 9px;
    }}
    button.secondary {{
      background: #e5e7eb;
      color: #111827;
    }}
    button.active {{
      box-shadow: 0 0 0 2px #2563eb inset;
    }}
    .status {{
      color: #374151;
      font-size: 12px;
      font-weight: 700;
      margin-top: 7px;
    }}
    .stop-list {{
      margin-top: 8px;
      max-height: 34vh;
      overflow: auto;
    }}
    .stop-row {{
      border: 1px solid #e5e7eb;
      border-radius: 7px;
      margin-bottom: 6px;
      padding: 7px;
    }}
    .stop-row.selected {{
      border-color: #dc2626;
      box-shadow: 0 0 0 1px #dc2626 inset;
    }}
    .stop-head {{
      align-items: center;
      display: flex;
      gap: 7px;
      margin-bottom: 6px;
    }}
    .stop-head input {{
      width: auto;
    }}
    .alias {{
      background: #111827;
      border-radius: 4px;
      color: white;
      font-size: 12px;
      font-weight: 800;
      min-width: 34px;
      padding: 3px 5px;
      text-align: center;
    }}
    .meta {{
      color: #6b7280;
      font-size: 11px;
      line-height: 1.35;
      margin-top: 5px;
    }}
    .route-segment {{
      fill: none;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 5;
    }}
    .route-segment.motion-stationary {{
      stroke: {MOTION_COLORS["stationary"]};
    }}
    .route-segment.motion-walking {{
      stroke: {MOTION_COLORS["walking"]};
    }}
    .route-segment.motion-cycling {{
      stroke: {MOTION_COLORS["cycling"]};
    }}
    .route-segment.motion-automotive {{
      stroke: {MOTION_COLORS["automotive"]};
    }}
    .route-segment.motion-moving {{
      stroke: {MOTION_COLORS["moving"]};
    }}
    .route-segment.motion-unknown {{
      stroke: {MOTION_COLORS["unknown"]};
    }}
    .route-arrow-marker {{
      background: transparent;
      border: 0;
    }}
    .route-arrow {{
      -webkit-text-stroke: 2px white;
      font-size: 20px;
      font-weight: 900;
      line-height: 1;
      text-shadow: 0 1px 4px rgb(15 23 42 / 0.35);
      transform-origin: center;
    }}
    .motion-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }}
    .motion-chip {{
      align-items: center;
      appearance: none;
      border: 0;
      border-radius: 999px;
      cursor: pointer;
      color: white;
      display: inline-flex;
      font-size: 12px;
      font-weight: 800;
      gap: 6px;
      padding: 5px 9px;
    }}
    .motion-chip:hover {{
      filter: brightness(1.08);
    }}
    .motion-chip.active {{
      box-shadow: 0 0 0 2px rgb(255 255 255 / 0.65) inset;
    }}
    .motion-chip .dot {{
      background: rgb(255 255 255 / 0.9);
      border-radius: 999px;
      height: 8px;
      width: 8px;
    }}
    .motion-chip.stationary {{
      background: {MOTION_COLORS["stationary"]};
    }}
    .motion-chip.walking {{
      background: {MOTION_COLORS["walking"]};
    }}
    .motion-chip.cycling {{
      background: {MOTION_COLORS["cycling"]};
    }}
    .motion-chip.automotive {{
      background: {MOTION_COLORS["automotive"]};
    }}
    .motion-chip.moving {{
      background: {MOTION_COLORS["moving"]};
    }}
    .motion-chip.dominant {{
      background: #0f172a;
      cursor: default;
    }}
    .profile {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      margin-top: 10px;
      padding: 8px;
    }}
    .profile-title {{
      color: #334155;
      font-size: 11px;
      font-weight: 800;
      margin-bottom: 6px;
      text-transform: uppercase;
    }}
    .elevation-chart {{
      display: block;
      height: 160px;
      width: 100%;
    }}
    .profile-summary {{
      color: #475569;
      font-size: 12px;
      line-height: 1.35;
      margin-top: 6px;
    }}
    .ride-segments {{
      display: grid;
      gap: 6px;
      margin-top: 8px;
      max-height: 24vh;
      overflow: auto;
    }}
    .ride-segment {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 7px;
      color: #111827;
      cursor: pointer;
      display: block;
      padding: 7px;
      text-align: left;
      width: 100%;
    }}
    .ride-segment:hover {{
      border-color: #2563eb;
    }}
    .ride-segment strong {{
      display: block;
      font-size: 12px;
      line-height: 1.25;
      margin-bottom: 4px;
    }}
    .ride-segment span {{
      color: #475569;
      display: block;
      font-size: 11px;
      line-height: 1.35;
    }}
    .route-legend {{
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 10px;
      margin: 8px 0 10px;
      min-height: 22px;
    }}
    .route-legend .legend-title {{
      color: #334155;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .route-legend .legend-item {{
      align-items: center;
      display: inline-flex;
      gap: 5px;
      font-size: 11px;
      color: #334155;
      white-space: nowrap;
    }}
    .route-legend .legend-swatch {{
      border-radius: 999px;
      display: inline-block;
      height: 10px;
      width: 10px;
    }}
    .route-legend .legend-gradient {{
      border: 1px solid rgb(15 23 42 / 0.16);
      border-radius: 999px;
      display: block;
      flex-basis: 100%;
      height: 12px;
      min-width: 180px;
    }}
    .route-legend .legend-scale {{
      color: #334155;
      display: flex;
      flex-basis: 100%;
      font-size: 11px;
      font-weight: 750;
      justify-content: space-between;
      min-width: 180px;
    }}
    .route-legend-map {{
      background: rgb(255 255 255 / 0.94);
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      box-shadow: 0 1px 6px rgb(15 23 42 / 0.18);
      max-width: 230px;
      padding: 8px 10px;
    }}
    .stop-label {{
      background: #111827;
      border: 0;
      border-radius: 4px;
      box-shadow: 0 2px 8px rgb(0 0 0 / 0.22);
      color: white;
      font-size: 13px;
      font-weight: 800;
      padding: 4px 6px;
    }}
    .place-label {{
      background: white;
      border: 1px solid #2563eb;
      border-radius: 4px;
      color: #1e3a8a;
      font-size: 12px;
      font-weight: 750;
      padding: 3px 5px;
    }}
    .empty {{
      background: white;
      border-radius: 8px;
      left: 50%;
      padding: 16px 18px;
      position: absolute;
      text-align: center;
      top: 50%;
      transform: translate(-50%, -50%);
      z-index: 999;
    }}
    .leaflet-control-scale {{
      margin-bottom: 14px !important;
      margin-right: 14px !important;
    }}
    .leaflet-control-scale-line {{
      background: rgb(255 255 255 / 0.92);
      border-color: #111827;
      border-width: 0 2px 2px;
      box-shadow: 0 1px 5px rgb(15 23 42 / 0.22);
      color: #111827;
      font-size: 12px;
      font-weight: 800;
    }}
    .stop-popup {{
      min-width: 240px;
    }}
    .stop-popup strong {{
      display: block;
      font-size: 14px;
      margin-bottom: 6px;
    }}
    .stop-popup label {{
      color: #374151;
      display: block;
      font-size: 11px;
      font-weight: 800;
      margin: 8px 0 3px;
    }}
    .stop-popup input,
    .stop-popup textarea {{
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      box-sizing: border-box;
      font: inherit;
      padding: 6px;
      width: 100%;
    }}
    .stop-popup textarea {{
      min-height: 62px;
      resize: vertical;
    }}
    .popup-meta {{
      color: #4b5563;
      font-size: 12px;
      line-height: 1.4;
    }}
    .segment-popup {{
      min-width: 210px;
    }}
    .segment-popup strong {{
      display: block;
      font-size: 14px;
      margin-bottom: 6px;
    }}
    .segment-popup .popup-meta div {{
      margin-top: 2px;
    }}
    @media (max-width: 700px) {{
      .tools {{
        max-height: calc(100vh - 20px);
        max-width: min(360px, calc(100vw - 20px));
      }}
      .tools:not(.collapsed) {{
        bottom: 10px;
      }}
      .stop-list {{
        display: none;
      }}
      .tools textarea {{
        height: 54px;
      }}
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <div id="tools" class="tools">
    <div class="tools-title">
      <h1>{escaped_title}</h1>
      <button id="toggleTools" type="button" class="secondary">Hide</button>
    </div>
    <div id="toolsBody" class="tools-body">
      <div class="row">
        <button id="selectAll" type="button" class="secondary">Select all</button>
        <button id="clearSelection" type="button" class="secondary">Clear</button>
        <button id="fitAll" type="button" class="secondary">Fit</button>
      </div>
      <div class="row" style="margin-top: 8px">
        <button id="prevDay" type="button" class="secondary">Previous day</button>
        <button id="nextDay" type="button" class="secondary">Next day</button>
      </div>
      <button id="centerSelected" type="button" class="secondary" style="margin-top: 8px; width: 100%">Center selected</button>
      <div class="row" style="margin-top: 8px">
        <button id="toggleEdges" type="button" class="secondary">Hide edges</button>
        <button id="toggleArrows" type="button" class="secondary">Hide arrows</button>
      </div>
      <div class="row" style="margin-top: 8px">
        <button id="toggleStopLabels" type="button" class="secondary">Hide stop labels</button>
        <button id="togglePlaceLabels" type="button" class="secondary">Hide point labels</button>
      </div>
      <button id="toggleFilteredPoints" type="button" class="secondary" style="margin-top: 8px; width: 100%">Show filtered points</button>
      <div class="profile">
        <div class="profile-title">Route animation</div>
        <div class="row">
          <button id="routeAnimPlay" type="button" class="secondary">Play</button>
          <button id="routeAnimReset" type="button" class="secondary">Reset</button>
        </div>
        <label for="routeAnimDuration">Playback duration</label>
        <select id="routeAnimDuration">
          <option value="5">5 sec</option>
          <option value="10">10 sec</option>
          <option value="15" selected>15 sec</option>
          <option value="20">20 sec</option>
          <option value="30">30 sec</option>
        </select>
        <div id="routeAnimStatus" class="profile-summary">ready</div>
      </div>
      <div class="motion-summary">{''.join(motion_chips) if motion_chips else '<span class="hint">Motion summary unavailable</span>'}</div>
      <div class="profile">
        <div class="profile-title">Ride segments</div>
        <div id="rideSegments" class="ride-segments"></div>
      </div>
      <div class="profile">
        <div class="profile-title">Elevation profile</div>
        <div class="row">
          <button id="profileDistance" type="button" class="secondary">Distance</button>
          <button id="profileTime" type="button" class="secondary">Time</button>
        </div>
        <div class="row" style="margin-top: 8px">
          <button id="routeMotion" type="button" class="secondary">Motion colors</button>
          <button id="routeSpeed" type="button" class="secondary">Speed colors</button>
        </div>
        <div class="row" style="margin-top: 8px">
          <button id="routeElevationBands" type="button" class="secondary">Elevation bands</button>
          <button id="routeElevationSlope" type="button" class="secondary">Ascent / descent</button>
        </div>
        <div id="routeLegend" class="route-legend"></div>
        <svg id="elevationChart" class="elevation-chart" viewBox="0 0 600 160" preserveAspectRatio="none" aria-label="Elevation profile"></svg>
        <div id="elevationSummary" class="profile-summary"></div>
      </div>
      <label for="groupDistance">Nearby grouping distance, meters</label>
      <input id="groupDistance" type="number" min="20" step="10" value="150">
      <div class="row">
        <button id="selectNearby" type="button">Select nearby</button>
        <button id="groupSelected" type="button">Group selected</button>
      </div>
      <label for="bulkName">Name for selected stops</label>
      <input id="bulkName" placeholder="Name selected stops">
      <div class="row">
        <button id="applyName" type="button">Apply</button>
      </div>
      <div id="stopList" class="stop-list"></div>
      <label for="commands">Paste this in Telegram</label>
      <textarea id="commands" readonly></textarea>
      <button id="copyCommands" type="button" class="secondary" style="margin-top: 8px; width: 100%">Copy</button>
      <div id="status" class="status">loading</div>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const data = {payload};
    const selected = new Set();
    const originalNames = new Map(data.stops.map((stop) => [stop.alias, stop.name]));
    const originalTags = new Map(data.stops.map((stop) => [stop.alias, (stop.tags || []).join(" ")]));
    const originalNotes = new Map(data.stops.map((stop) => [stop.alias, stop.note || ""]));
    const tools = document.getElementById("tools");
    const toggleToolsButton = document.getElementById("toggleTools");
    const commands = document.getElementById("commands");
    const status = document.getElementById("status");
    let clipboardStatus = "";
    const map = L.map("map", {{ preferCanvas: true, zoomControl: false }});
    const tiles = L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }}).addTo(map);
    L.control.zoom({{ position: "bottomright" }}).addTo(map);
    L.control.scale({{ position: "bottomright", metric: true, imperial: false, maxWidth: 160 }}).addTo(map);
    const fitPoints = [];
    let fitMinLat = Infinity;
    let fitMaxLat = -Infinity;
    let fitMinLon = Infinity;
    let fitMaxLon = -Infinity;
    let fitCount = 0;
    const markers = new Map();
    const placeMarkers = [];
    const routeRenderer = L.svg();
    routeRenderer.addTo(map);
    const edgeLayer = L.layerGroup().addTo(map);
    const arrowLayer = L.layerGroup().addTo(map);
    const routeLayer = L.layerGroup().addTo(map);
    const filteredPointLayer = L.layerGroup();
    const animationLayer = L.layerGroup().addTo(map);
    let activeMotionMode = "all";
    let edgesVisible = true;
    let arrowsVisible = true;
    let stopLabelsVisible = true;
    let placeLabelsVisible = true;
    let filteredPointsVisible = false;
    let routeColorMode = "speed";
    let profileAxis = "distance";
    let routeAnimationFrame = null;
    let routeAnimationRunning = false;
    let routeAnimationStartMs = null;
    let routeAnimationElapsedMs = 0;
    let routeAnimationStaticVisibility = null;
    const motionModes = ["all", "stationary", "walking", "cycling", "automotive", "moving"];
    const routeColorModes = ["motion", "speed", "bands", "slope"];
    const profileAxes = ["distance", "time"];
    const motionColors = {{
      stationary: "{MOTION_COLORS["stationary"]}",
      walking: "{MOTION_COLORS["walking"]}",
      cycling: "{MOTION_COLORS["cycling"]}",
      automotive: "{MOTION_COLORS["automotive"]}",
      moving: "{MOTION_COLORS["moving"]}",
      unknown: "{MOTION_COLORS["unknown"]}"
    }};
    const elevationSummaryData = data.elevation_summary || {{}};
    const elevationPalette = ["#2563eb", "#06b6d4", "#10b981", "#f59e0b", "#f97316", "#ef4444"];
    const elevationMin = Number.isFinite(Number(elevationSummaryData.min_alt_m)) ? Number(elevationSummaryData.min_alt_m) : 0;
    const elevationMax = Number.isFinite(Number(elevationSummaryData.max_alt_m)) ? Number(elevationSummaryData.max_alt_m) : 0;
    const elevationSpan = Math.max(1, elevationMax - elevationMin);
    const slopeColors = {{
      descentStrong: "#1d4ed8",
      descent: "#38bdf8",
      flat: "#6b7280",
      ascent: "#fb923c",
      ascentStrong: "#dc2626",
    }};
    const speedPalette = ["#6b7280", "#16a34a", "#2563eb", "#f59e0b", "#ef4444", "#7c2d12"];
    let elevationBandBounds = {{ min: elevationMin, max: elevationMax }};
    let speedBounds = {{ min: 0, max: 1 }};
    const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({{
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }}[char]));
    const dateForOffset = (dateText, offsetDays) => {{
      const match = String(dateText || "").match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})$/);
      if (!match) return null;
      const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
      if (!Number.isFinite(date.getTime())) return null;
      date.setUTCDate(date.getUTCDate() + offsetDays);
      return date.toISOString().slice(0, 10);
    }};
    const mapPathForDate = (dateText) => {{
      const path = window.location.pathname;
      if (/\\/owntracks\\/map\\/[^/]+$/.test(path)) {{
        return path.replace(/\\/owntracks\\/map\\/[^/]+$/, `/owntracks/map/${{dateText}}`);
      }}
      return `/owntracks/map/${{dateText}}`;
    }};
    const navigateDay = (offsetDays) => {{
      const targetDate = dateForOffset(data.date, offsetDays);
      if (!targetDate) return;
      window.location.href = `${{mapPathForDate(targetDate)}}${{window.location.search}}${{window.location.hash}}`;
    }};
    const syncDayNavigationButtons = () => {{
      const canNavigate = Boolean(dateForOffset(data.date, 0));
      document.getElementById("prevDay").disabled = !canNavigate;
      document.getElementById("nextDay").disabled = !canNavigate;
    }};
    const updateStatus = () => {{
      status.textContent = `leaflet z${{map.getZoom()}} · selected ${{selected.size}} · stops ${{data.stops.length}}${{clipboardStatus}}`;
    }};
    const distanceMeters = (a, b) => {{
      const radius = 6371008.8;
      const toRad = (value) => value * Math.PI / 180;
      const dLat = toRad(b.lat - a.lat);
      const dLon = toRad(b.lon - a.lon);
      const lat1 = toRad(a.lat);
      const lat2 = toRad(b.lat);
      const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
      return 2 * radius * Math.asin(Math.sqrt(h));
    }};
    const zoomAtLeast = (minimum) => {{
      const zoom = map.getZoom();
      return Number.isFinite(Number(zoom)) ? Math.max(Number(zoom), minimum) : minimum;
    }};
    const derivedSpeedKmh = (previous, current) => {{
      if (!previous || !current) return null;
      const previousTs = Number(previous.timestamp);
      const currentTs = Number(current.timestamp);
      if (!Number.isFinite(previousTs) || !Number.isFinite(currentTs) || currentTs <= previousTs) return null;
      const meters = distanceMeters(previous, current);
      const seconds = currentTs - previousTs;
      const kmh = (meters / 1000) / (seconds / 3600);
      return Number.isFinite(kmh) && kmh <= 160 ? kmh : null;
    }};
    const bestSpeedKmh = (reported, derived) => {{
      const reportedSpeed = Number.isFinite(Number(reported)) ? Number(reported) : null;
      const derivedSpeed = Number.isFinite(Number(derived)) ? Number(derived) : null;
      if (derivedSpeed == null) return reportedSpeed;
      if (reportedSpeed == null) return derivedSpeed;
      if (reportedSpeed <= 1 && derivedSpeed > 3) return derivedSpeed;
      if (reportedSpeed < derivedSpeed * 0.4 && derivedSpeed > 5) return derivedSpeed;
      return reportedSpeed;
    }};
    const hexToRgb = (hex) => {{
      const value = String(hex || "").replace("#", "");
      const parsed = Number.parseInt(value.length === 3 ? value.split("").map((char) => char + char).join("") : value, 16);
      if (!Number.isFinite(parsed)) return [100, 116, 139];
      return [(parsed >> 16) & 255, (parsed >> 8) & 255, parsed & 255];
    }};
    const mixColor = (from, to, ratio) => {{
      const a = hexToRgb(from);
      const b = hexToRgb(to);
      const r = Math.max(0, Math.min(1, ratio));
      const mixed = a.map((value, index) => Math.round(value + (b[index] - value) * r));
      return `rgb(${{mixed[0]}}, ${{mixed[1]}}, ${{mixed[2]}})`;
    }};
    const percentile = (values, ratio) => {{
      if (!values.length) return null;
      const sorted = [...values].sort((a, b) => a - b);
      const index = Math.max(0, Math.min(sorted.length - 1, (sorted.length - 1) * ratio));
      const lower = Math.floor(index);
      const upper = Math.ceil(index);
      if (lower === upper) return sorted[lower];
      return interpolateNumber(sorted[lower], sorted[upper], index - lower);
    }};
    const speedColor = (speed) => {{
      const value = Number(speed);
      if (!Number.isFinite(value)) return motionColors.unknown;
      const span = Math.max(0.1, speedBounds.max - speedBounds.min);
      const ratio = Math.max(0, Math.min(1, (value - speedBounds.min) / span));
      const scaled = ratio * (speedPalette.length - 1);
      const index = Math.min(speedPalette.length - 2, Math.floor(scaled));
      return mixColor(speedPalette[index], speedPalette[index + 1], scaled - index);
    }};
    const interpolateNumber = (from, to, ratio) => from + ((to - from) * ratio);
    const interpolateSpeed = (from, to, ratio) => {{
      const start = Number(from);
      const end = Number(to);
      if (Number.isFinite(start) && Number.isFinite(end)) return interpolateNumber(start, end, ratio);
      if (Number.isFinite(end)) return end;
      if (Number.isFinite(start)) return start;
      return null;
    }};
    const parseTags = (value) => value.split(/[\\s,]+/).map((item) => item.trim()).filter(Boolean);
    const copyCommandsToClipboard = async (automatic = false) => {{
      if (!commands.value) return false;
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          await navigator.clipboard.writeText(commands.value);
        }} else if (automatic) {{
          throw new Error("automatic clipboard unavailable");
        }} else {{
          commands.select();
          document.execCommand("copy");
        }}
        clipboardStatus = automatic ? " · copied" : " · copied manually";
        updateStatus();
        return true;
      }} catch (error) {{
        clipboardStatus = " · copy blocked";
        updateStatus();
        return false;
      }}
    }};
    const updateCommands = (autoCopy = true) => {{
      const changed = data.stops.filter((stop) =>
        stop.name !== originalNames.get(stop.alias)
        || (stop.tags || []).join(" ") !== originalTags.get(stop.alias)
        || (stop.note || "") !== originalNotes.get(stop.alias)
      );
      commands.value = changed.length
        ? [
            "/otb " + data.date,
            ...changed.map((stop) => {{
              const parts = [`${{stop.alias}} ${{stop.name}}`];
              if ((stop.tags || []).length) parts.push(`tags: ${{stop.tags.join(" ")}}`);
              else if (originalTags.get(stop.alias)) parts.push("tags:");
              if (stop.note) parts.push(`note: ${{stop.note}}`);
              else if (originalNotes.get(stop.alias)) parts.push("note:");
              return parts.join(" | ");
            }})
          ].join("\\n")
        : "";
      clipboardStatus = commands.value ? clipboardStatus : "";
      updateStatus();
      if (autoCopy && commands.value) copyCommandsToClipboard(true);
    }};
    const selectedStops = () => data.stops.filter((stop) => selected.has(stop.alias));
    const rawTrackPoints = data.sampledTrack || [];
    const rawUnfilteredTrackPoints = data.rawSampledTrack || rawTrackPoints;
    const rideSegments = data.rideSegments || [];
    const trackPoints = [];
    const visibleTrackLineNumbers = new Set(rawTrackPoints.map((point) => Number(point.line)).filter(Number.isFinite));
    const filteredTrackPoints = rawUnfilteredTrackPoints.filter((point) => {{
      const line = Number(point.line);
      return Number.isFinite(line) && !visibleTrackLineNumbers.has(line);
    }});
    const rideSegmentsEl = document.getElementById("rideSegments");
    const routeLegend = document.getElementById("routeLegend");
    const routeLegendControl = L.control({{ position: "topright" }});
    routeLegendControl.onAdd = () => {{
      const el = L.DomUtil.create("div", "route-legend route-legend-map");
      L.DomEvent.disableClickPropagation(el);
      return el;
    }};
    routeLegendControl.addTo(map);
    const toTimestamp = (value) => {{
      const num = Number(value);
      return Number.isFinite(num) ? num : null;
    }};
    const timeOrigin = rawTrackPoints.length ? toTimestamp(rawTrackPoints[0].timestamp) : null;
    let cumulativeTrackDistanceKm = 0;
    let previousTrackPoint = null;
    for (const point of rawTrackPoints) {{
      const lat = Number(point.lat);
      const lon = Number(point.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const alt = Number(point.alt_m);
      const timestamp = toTimestamp(point.timestamp);
      const speedKmh = Number(point.speed_kmh);
      const currentTrackPoint = {{ lat, lon, timestamp }};
      const segmentSpeedKmh = derivedSpeedKmh(previousTrackPoint, currentTrackPoint);
      if (previousTrackPoint) cumulativeTrackDistanceKm += distanceMeters(previousTrackPoint, currentTrackPoint) / 1000;
      trackPoints.push({{
        lat,
        lon,
        alt_m: Number.isFinite(alt) ? alt : null,
        motion_mode: point.motion_mode || "moving",
        speed_kmh: bestSpeedKmh(speedKmh, segmentSpeedKmh),
        reported_speed_kmh: Number.isFinite(speedKmh) ? speedKmh : null,
        derived_speed_kmh: segmentSpeedKmh,
        timestamp,
        cumulativeDistanceKm: cumulativeTrackDistanceKm,
      }});
      previousTrackPoint = currentTrackPoint;
    }}
    const refreshElevationBandBounds = () => {{
      const samples = visibleTrackPoints().filter((point) => Number.isFinite(point.alt_m));
      if (!samples.length) {{
        elevationBandBounds = {{ min: elevationMin, max: elevationMax }};
        return;
      }}
      const values = samples.map((point) => Number(point.alt_m));
      elevationBandBounds = {{
        min: Math.min(...values),
        max: Math.max(...values),
      }};
    }};
    const refreshSpeedBounds = () => {{
      const values = visibleTrackPoints().map((point) => Number(point.speed_kmh)).filter(Number.isFinite);
      if (!values.length) {{
        speedBounds = {{ min: 0, max: 1 }};
        return;
      }}
      const minSpeed = Math.max(0, Math.min(...values));
      const p75Speed = percentile(values, 0.75) ?? minSpeed;
      const p95Speed = percentile(values, 0.95) ?? p75Speed;
      const robustMaxSpeed = Math.max(p95Speed, p75Speed * 1.5, 30);
      speedBounds = {{ min: minSpeed, max: Math.max(minSpeed + 1, robustMaxSpeed) }};
    }};
    const routeColorFor = (point, prev = null) => {{
      if (routeColorMode === "bands") {{
        const alt = Number.isFinite(Number(point.alt_m)) ? Number(point.alt_m) : (prev && Number.isFinite(Number(prev.alt_m)) ? Number(prev.alt_m) : null);
        if (!Number.isFinite(Number(alt))) return motionColors.unknown;
        const minAlt = Number.isFinite(Number(elevationBandBounds.min)) ? Number(elevationBandBounds.min) : elevationMin;
        const maxAlt = Number.isFinite(Number(elevationBandBounds.max)) ? Number(elevationBandBounds.max) : elevationMax;
        const span = Math.max(1, maxAlt - minAlt);
        const ratio = Math.max(0, Math.min(1, (Number(alt) - minAlt) / span));
        const index = Math.min(elevationPalette.length - 1, Math.floor(ratio * elevationPalette.length));
        return elevationPalette[index];
      }}
      if (routeColorMode === "slope") {{
        const currentAlt = Number.isFinite(Number(point.alt_m)) ? Number(point.alt_m) : null;
        const previousAlt = prev && Number.isFinite(Number(prev.alt_m)) ? Number(prev.alt_m) : null;
        if (!Number.isFinite(currentAlt) || !Number.isFinite(previousAlt)) {{
          return motionColors.unknown;
        }}
        const delta = currentAlt - previousAlt;
        if (delta >= 12) return slopeColors.ascentStrong;
        if (delta >= 4) return slopeColors.ascent;
        if (delta <= -12) return slopeColors.descentStrong;
        if (delta <= -4) return slopeColors.descent;
        return slopeColors.flat;
      }}
      if (routeColorMode === "speed") {{
        const speed = Number.isFinite(Number(point.speed_kmh)) ? Number(point.speed_kmh) : (prev && Number.isFinite(Number(prev.speed_kmh)) ? Number(prev.speed_kmh) : null);
        return speedColor(speed);
      }}
      const mode = point.motion_mode || (prev && prev.motion_mode) || "moving";
      return motionColors[mode] || motionColors.moving;
    }};
    const formatSpeed = (value) => Number.isFinite(Number(value)) ? `${{Number(value).toFixed(1)}} km/h` : "unknown";
    const formatAltitude = (value) => Number.isFinite(Number(value)) ? `${{Number(value).toFixed(1)}} m` : "unknown";
    const formatDeltaAltitude = (value) => {{
      if (!Number.isFinite(Number(value))) return "unknown";
      const delta = Number(value);
      const sign = delta > 0 ? "+" : "";
      return `${{sign}}${{delta.toFixed(1)}} m`;
    }};
    const formatMeters = (value) => {{
      const meters = Number(value);
      if (!Number.isFinite(meters)) return "unknown";
      return meters >= 1000 ? `${{(meters / 1000).toFixed(2)}} km` : `${{Math.round(meters)}} m`;
    }};
    const formatDuration = (seconds) => {{
      const value = Number(seconds);
      if (!Number.isFinite(value) || value < 0) return "unknown";
      if (value < 60) return `${{Math.round(value)}} sec`;
      const minutes = Math.floor(value / 60);
      const secs = Math.round(value % 60);
      return secs ? `${{minutes}}m ${{secs}}s` : `${{minutes}} min`;
    }};
    const formatTime = (timestamp) => Number.isFinite(Number(timestamp))
      ? new Date(Number(timestamp) * 1000).toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit", second: "2-digit" }})
      : "unknown";
    const formatSpeedPlain = (value) => Number.isFinite(Number(value)) ? `${{Number(value).toFixed(1)}} km/h` : "unknown";
    const speedSourceLabel = (point) => {{
      const speed = Number(point.speed_kmh);
      const reported = Number(point.reported_speed_kmh);
      const derived = Number(point.derived_speed_kmh);
      if (Number.isFinite(reported) && Math.abs(speed - reported) < 0.05) return "OwnTracks vel";
      if (Number.isFinite(derived) && Math.abs(speed - derived) < 0.05) return "GPS distance/time";
      return "best available";
    }};
    const routeColorModeLabel = () => ({{
      motion: "Motion colors",
      speed: "Speed colors",
      bands: "Elevation bands",
      slope: "Ascent / descent",
    }}[routeColorMode] || "Route colors");
    const slopeLabel = (delta) => {{
      if (!Number.isFinite(Number(delta))) return "unknown";
      if (delta >= 12) return "strong ascent";
      if (delta >= 4) return "ascent";
      if (delta <= -12) return "strong descent";
      if (delta <= -4) return "descent";
      return "flat";
    }};
    const segmentPopupHtml = (prev, current, meters) => {{
      const seconds = Number(current.timestamp) - Number(prev.timestamp);
      const currentAlt = Number(current.alt_m);
      const previousAlt = Number(prev.alt_m);
      const elevationDelta = Number.isFinite(currentAlt) && Number.isFinite(previousAlt) ? currentAlt - previousAlt : null;
      const motionMode = current.motion_mode || prev.motion_mode || "moving";
      return `
        <div class="segment-popup">
          <strong>${{escapeHtml(routeColorModeLabel())}}</strong>
          <div class="popup-meta">
            <div>Speed: ${{formatSpeed(current.speed_kmh)}} · Source: ${{escapeHtml(speedSourceLabel(current))}}</div>
            <div>OwnTracks vel: ${{formatSpeed(current.reported_speed_kmh)}}</div>
            <div>GPS-derived: ${{formatSpeed(current.derived_speed_kmh)}}</div>
            <div>Altitude: ${{formatAltitude(current.alt_m)}} · Change: ${{formatDeltaAltitude(elevationDelta)}}</div>
            <div>Slope band: ${{escapeHtml(slopeLabel(elevationDelta))}}</div>
            <div>Distance: ${{formatMeters(meters)}} · Duration: ${{formatDuration(seconds)}}</div>
            <div>${{formatTime(prev.timestamp)}} to ${{formatTime(current.timestamp)}}</div>
            <div>Motion: ${{escapeHtml(motionMode)}}</div>
          </div>
        </div>
      `;
    }};
    const visibleTrackPoints = () => trackPoints.filter((point) => activeMotionMode === "all" || (point.motion_mode || "moving") === activeMotionMode);
    const filteredTrackPointsForMode = () => filteredTrackPoints.filter((point) => activeMotionMode === "all" || (point.motion_mode || "moving") === activeMotionMode);
    const filteredPointPopupHtml = (point) => `
      <div class="segment-popup">
        <strong>Filtered point</strong>
        <div class="popup-meta">
          <div>${{escapeHtml(point.time || "unknown")}}</div>
          <div>Line: ${{escapeHtml(point.line || "")}}</div>
          <div>Motion: ${{escapeHtml(point.motion_mode || "unknown")}}</div>
          <div>Speed: ${{formatSpeed(point.speed_kmh)}} · Accuracy: ${{Number.isFinite(Number(point.accuracy_m)) ? Number(point.accuracy_m).toFixed(0) + " m" : "unknown"}}</div>
          ${{point.maps ? `<div><a href="${{escapeHtml(point.maps)}}" target="_blank" rel="noreferrer">Google Maps</a></div>` : ""}}
        </div>
      </div>
    `;
    const segmentPointsFor = (segment) => trackPoints.filter((point) => {{
      const timestamp = Number(point.timestamp);
      return Number.isFinite(timestamp) && timestamp >= Number(segment.start_timestamp) && timestamp <= Number(segment.end_timestamp);
    }});
    const fitRideSegment = (segment) => {{
      const points = segmentPointsFor(segment);
      if (points.length >= 2) {{
        map.fitBounds(L.latLngBounds(points.map((point) => [point.lat, point.lon])).pad(0.18));
      }} else if (Number.isFinite(Number(segment.start_lat)) && Number.isFinite(Number(segment.start_lon))) {{
        map.setView([Number(segment.start_lat), Number(segment.start_lon)], zoomAtLeast(14), {{ animate: true }});
      }}
    }};
    const renderRideSegments = () => {{
      if (!rideSegmentsEl) return;
      if (!rideSegments.length) {{
        rideSegmentsEl.innerHTML = '<div class="profile-summary">No ride segments found for this day.</div>';
        return;
      }}
      rideSegmentsEl.innerHTML = rideSegments.map((segment, index) => `
        <button type="button" class="ride-segment" data-segment-index="${{index}}">
          <strong>${{escapeHtml(segment.label || segment.id || "Ride segment")}}</strong>
          <span>${{escapeHtml(segment.duration || "")}} elapsed · ${{escapeHtml(segment.moving_duration || "")}} moving · ${{Number(segment.distance_km || 0).toFixed(2)}} km</span>
          <span>moving avg ${{formatSpeedPlain(segment.moving_average_speed_kmh)}} · gross avg ${{formatSpeedPlain(segment.average_speed_kmh)}} · max ${{formatSpeedPlain(segment.max_speed_kmh)}}</span>
          <span>${{escapeHtml(segment.dominant_moving_motion || segment.dominant_motion || "unknown")}} · ${{escapeHtml(segment.start_time || "")}} to ${{escapeHtml(segment.end_time || "")}}</span>
        </button>
      `).join("");
      rideSegmentsEl.querySelectorAll("[data-segment-index]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const segment = rideSegments[Number(button.dataset.segmentIndex)];
          if (segment) fitRideSegment(segment);
        }});
      }});
    }};
    const routePointRadius = () => {{
      const zoom = map.getZoom();
      if (zoom <= 12) return 1.8;
      if (zoom <= 15) return 2.6;
      return 3.5;
    }};
    const routeArrowSize = () => {{
      const zoom = map.getZoom();
      if (zoom <= 12) return {{ size: 10, stroke: 1 }};
      if (zoom <= 15) return {{ size: 14, stroke: 1.4 }};
      return {{ size: 20, stroke: 2 }};
    }};
    const routeEdgeWeight = () => {{
      const zoom = map.getZoom();
      if (zoom <= 8) return 0.8;
      if (zoom <= 10) return 1.2;
      if (zoom <= 12) return 1.8;
      if (zoom <= 14) return 3.2;
      if (zoom <= 15) return 4.5;
      return 6.5;
    }};
    const drawRoute = () => {{
      routeLayer.clearLayers();
      refreshElevationBandBounds();
      const points = visibleTrackPoints();
      if (!points.length) {{
        renderRouteLegend();
        return;
      }}
      let previous = null;
      const pointRadius = routePointRadius();
      refreshSpeedBounds();
      for (const point of points) {{
        L.circleMarker([point.lat, point.lon], {{
          radius: pointRadius,
          color: routeColorFor(point, previous),
          fillColor: routeColorFor(point, previous),
          fillOpacity: 0.9,
          weight: 0,
        }}).addTo(routeLayer);
        previous = point;
      }}
      drawEdges();
      renderRouteLegend();
    }};
    const drawEdges = () => {{
      edgeLayer.clearLayers();
      arrowLayer.clearLayers();
      const points = visibleTrackPoints();
      if ((!edgesVisible && !arrowsVisible) || points.length < 2) return;
      const arrowSpacingMeters = () => {{
        const zoom = map.getZoom();
        if (zoom <= 12) return 1600;
        if (zoom <= 15) return 600;
        return 220;
      }};
      const bearingBetween = (fromLat, fromLon, toLat, toLon) => {{
        const startLat = fromLat * Math.PI / 180;
        const endLat = toLat * Math.PI / 180;
        const dLon = (toLon - fromLon) * Math.PI / 180;
        const y = Math.sin(dLon) * Math.cos(endLat);
        const x = Math.cos(startLat) * Math.sin(endLat) - Math.sin(startLat) * Math.cos(endLat) * Math.cos(dLon);
        return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
      }};
      const arrowSpacing = arrowSpacingMeters();
      const arrowStyle = routeArrowSize();
      const arrowSize = arrowStyle.size;
      const arrowAnchor = arrowSize / 2;
      const edgeWeight = routeEdgeWeight();
      const stationaryArrowMinMeters = 500;
      let metersSinceArrow = arrowSpacing / 2;
      let stationaryRunHasArrow = false;
      for (let index = 1; index < points.length; index += 1) {{
        const prev = points[index - 1];
        const current = points[index];
        const prevLat = Number(prev.lat);
        const prevLon = Number(prev.lon);
        const currentLat = Number(current.lat);
        const currentLon = Number(current.lon);
        if (!Number.isFinite(prevLat) || !Number.isFinite(prevLon) || !Number.isFinite(currentLat) || !Number.isFinite(currentLon)) continue;
        const mode = current.motion_mode || prev.motion_mode || "moving";
        if (activeMotionMode !== "all" && mode !== activeMotionMode) continue;
        const color = routeColorFor(current, prev);
        const segmentMeters = distanceMeters({{ lat: prevLat, lon: prevLon }}, {{ lat: currentLat, lon: currentLon }});
        if (edgesVisible) {{
          if (routeColorMode === "speed") {{
            const steps = Math.max(3, Math.min(18, Math.ceil((Number.isFinite(segmentMeters) ? segmentMeters : 0) / 120)));
            for (let step = 0; step < steps; step += 1) {{
              const startRatio = step / steps;
              const endRatio = (step + 1) / steps;
              const midRatio = (startRatio + endRatio) / 2;
              const startLat = interpolateNumber(prevLat, currentLat, startRatio);
              const startLon = interpolateNumber(prevLon, currentLon, startRatio);
              const endLat = interpolateNumber(prevLat, currentLat, endRatio);
              const endLon = interpolateNumber(prevLon, currentLon, endRatio);
              const speed = interpolateSpeed(prev.speed_kmh, current.speed_kmh, midRatio);
              L.polyline(
                [[startLat, startLon], [endLat, endLon]],
                {{
                  renderer: routeRenderer,
                  color: speedColor(speed),
                  weight: edgeWeight,
                  opacity: 1,
                  noClip: true,
                  interactive: true,
                }}
              ).bindPopup(segmentPopupHtml(prev, current, segmentMeters)).addTo(edgeLayer);
            }}
          }} else {{
            L.polyline(
              [[prevLat, prevLon], [currentLat, currentLon]],
              {{
                renderer: routeRenderer,
                color,
                weight: edgeWeight,
                opacity: 1,
                noClip: true,
                interactive: true,
              }}
            ).bindPopup(segmentPopupHtml(prev, current, segmentMeters)).addTo(edgeLayer);
          }}
        }}
        if (!arrowsVisible) continue;
        if (!Number.isFinite(segmentMeters) || segmentMeters < 25) continue;
        if (mode === "stationary") {{
          if (stationaryRunHasArrow || segmentMeters < stationaryArrowMinMeters) continue;
          stationaryRunHasArrow = true;
        }} else {{
          stationaryRunHasArrow = false;
          metersSinceArrow += segmentMeters;
          if (metersSinceArrow < arrowSpacing) continue;
          metersSinceArrow = 0;
        }}
        const arrowLat = (prevLat + currentLat) / 2;
        const arrowLon = (prevLon + currentLon) / 2;
        const angle = bearingBetween(prevLat, prevLon, currentLat, currentLon);
        L.marker([arrowLat, arrowLon], {{
          interactive: false,
          icon: L.divIcon({{
            className: "route-arrow-marker",
            html: `<div class="route-arrow" style="color: ${{color}}; -webkit-text-stroke: ${{arrowStyle.stroke}}px white; font-size: ${{arrowSize}}px; transform: rotate(${{angle - 90}}deg)">➤</div>`,
            iconSize: [arrowSize + 4, arrowSize + 4],
            iconAnchor: [arrowAnchor + 2, arrowAnchor + 2],
          }}),
        }}).addTo(arrowLayer);
      }}
    }};
    const routeAnimationSegments = () => {{
      const points = visibleTrackPoints();
      const segments = [];
      for (let index = 1; index < points.length; index += 1) {{
        const prev = points[index - 1];
        const current = points[index];
        const prevLat = Number(prev.lat);
        const prevLon = Number(prev.lon);
        const currentLat = Number(current.lat);
        const currentLon = Number(current.lon);
        if (!Number.isFinite(prevLat) || !Number.isFinite(prevLon) || !Number.isFinite(currentLat) || !Number.isFinite(currentLon)) continue;
        const segmentMeters = distanceMeters({{ lat: prevLat, lon: prevLon }}, {{ lat: currentLat, lon: currentLon }});
        if (!Number.isFinite(segmentMeters) || segmentMeters < 2) continue;
        segments.push({{
          start: [prevLat, prevLon],
          end: [currentLat, currentLon],
          color: routeColorFor(current, prev),
          timestamp: Number.isFinite(Number(current.timestamp)) ? Number(current.timestamp) : index,
        }});
      }}
      return segments;
    }};
    const routeAnimationDurationMs = () => {{
      const value = Number(document.getElementById("routeAnimDuration")?.value);
      return (Number.isFinite(value) && value > 0 ? value : 60) * 1000;
    }};
    const routeAnimationStatus = (text) => {{
      const el = document.getElementById("routeAnimStatus");
      if (el) el.textContent = text;
    }};
    const syncRouteAnimationButton = () => {{
      const button = document.getElementById("routeAnimPlay");
      if (!button) return;
      button.textContent = routeAnimationRunning ? "Pause" : "Play";
      button.classList.toggle("active", routeAnimationRunning);
    }};
    const hideStaticRouteForAnimation = () => {{
      if (!routeAnimationStaticVisibility) {{
        routeAnimationStaticVisibility = {{
          edges: edgesVisible,
          arrows: arrowsVisible,
        }};
      }}
      edgeLayer.remove();
      arrowLayer.remove();
    }};
    const restoreStaticRouteAfterAnimation = () => {{
      if (!routeAnimationStaticVisibility) return;
      if (routeAnimationStaticVisibility.edges) edgeLayer.addTo(map);
      else edgeLayer.remove();
      if (routeAnimationStaticVisibility.arrows) arrowLayer.addTo(map);
      else arrowLayer.remove();
      routeAnimationStaticVisibility = null;
    }};
    const renderRouteAnimation = (elapsedMs) => {{
      const segments = routeAnimationSegments();
      animationLayer.clearLayers();
      if (!segments.length) {{
        routeAnimationStatus("no route points");
        return true;
      }}
      const durationMs = routeAnimationDurationMs();
      const progress = Math.max(0, Math.min(1, elapsedMs / durationMs));
      const maxIndex = Math.max(0, Math.ceil(progress * segments.length) - 1);
      for (let index = 0; index <= maxIndex && index < segments.length; index += 1) {{
        const segment = segments[index];
        L.polyline([segment.start, segment.end], {{
          renderer: routeRenderer,
          color: segment.color,
          weight: routeEdgeWeight(),
          opacity: 1,
          noClip: true,
          interactive: false,
        }}).addTo(animationLayer);
      }}
      const current = segments[Math.min(maxIndex, segments.length - 1)];
      const timeText = current && Number.isFinite(current.timestamp)
        ? new Date(current.timestamp * 1000).toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit" }})
        : "";
      routeAnimationStatus(`${{Math.round(progress * 100)}}%${{timeText ? " · " + timeText : ""}}`);
      return progress >= 1;
    }};
    const stopRouteAnimation = () => {{
      routeAnimationRunning = false;
      if (routeAnimationFrame) cancelAnimationFrame(routeAnimationFrame);
      routeAnimationFrame = null;
      routeAnimationStartMs = null;
      restoreStaticRouteAfterAnimation();
      syncRouteAnimationButton();
    }};
    const tickRouteAnimation = (now) => {{
      if (!routeAnimationRunning) return;
      if (routeAnimationStartMs == null) routeAnimationStartMs = now - routeAnimationElapsedMs;
      routeAnimationElapsedMs = now - routeAnimationStartMs;
      const finished = renderRouteAnimation(routeAnimationElapsedMs);
      if (finished) {{
        routeAnimationElapsedMs = routeAnimationDurationMs();
        stopRouteAnimation();
        return;
      }}
      routeAnimationFrame = requestAnimationFrame(tickRouteAnimation);
    }};
    const playRouteAnimation = () => {{
      if (routeAnimationRunning) {{
        stopRouteAnimation();
        return;
      }}
      hideStaticRouteForAnimation();
      routeAnimationRunning = true;
      routeAnimationStartMs = null;
      syncRouteAnimationButton();
      routeAnimationFrame = requestAnimationFrame(tickRouteAnimation);
    }};
    const resetRouteAnimation = () => {{
      stopRouteAnimation();
      routeAnimationElapsedMs = 0;
      animationLayer.clearLayers();
      routeAnimationStatus("ready");
    }};
    const renderRouteLegend = () => {{
      const target = routeLegendControl && routeLegendControl._container ? routeLegendControl._container : routeLegend;
      if (!target) return;
      const renderItems = (items, title) => `
        <span class="legend-title">${{escapeHtml(title)}}</span>
        ${{items.map((item) => `
          <span class="legend-item">
            <span class="legend-swatch" style="background:${{item.color}}"></span>
            <span>${{escapeHtml(item.label)}}</span>
          </span>
        `).join("")}}
      `;
      if (routeColorMode === "motion") {{
        target.innerHTML = renderItems([
          {{ color: motionColors.stationary, label: "stationary" }},
          {{ color: motionColors.walking, label: "walking" }},
          {{ color: motionColors.cycling, label: "cycling" }},
          {{ color: motionColors.automotive, label: "automotive" }},
          {{ color: motionColors.moving, label: "moving" }},
        ], "Motion");
        return;
      }}
      if (routeColorMode === "bands") {{
        const minAlt = Number.isFinite(Number(elevationBandBounds.min)) ? Number(elevationBandBounds.min) : elevationMin;
        const maxAlt = Number.isFinite(Number(elevationBandBounds.max)) ? Number(elevationBandBounds.max) : elevationMax;
        const span = Math.max(1, maxAlt - minAlt);
        const swatches = elevationPalette.map((color, index) => {{
          const start = minAlt + (index / elevationPalette.length) * span;
          const end = minAlt + ((index + 1) / elevationPalette.length) * span;
          return {{ color, label: `${{Math.round(start)}}-${{Math.round(end)}}m` }};
        }});
        target.innerHTML = renderItems(swatches, "Elevation bands");
        return;
      }}
      if (routeColorMode === "speed") {{
        const gradient = `linear-gradient(90deg, ${{speedPalette.join(", ")}})`;
        target.innerHTML = `
          <span class="legend-title">Speed</span>
          <span class="legend-gradient" style="background: ${{gradient}}"></span>
          <span class="legend-scale">
            <span>${{speedBounds.min.toFixed(1)}} km/h</span>
            <span>${{speedBounds.max.toFixed(1)}} km/h</span>
          </span>
        `;
        return;
      }}
      target.innerHTML = renderItems([
        {{ color: slopeColors.descentStrong, label: "strong descent" }},
        {{ color: slopeColors.descent, label: "descent" }},
        {{ color: slopeColors.flat, label: "flat" }},
        {{ color: slopeColors.ascent, label: "ascent" }},
        {{ color: slopeColors.ascentStrong, label: "strong ascent" }},
      ], "Elevation slope");
    }};
    const setActiveMotionMode = (mode) => {{
      resetRouteAnimation();
      activeMotionMode = motionModes.includes(mode) ? mode : "all";
      document.querySelectorAll("[data-motion-mode]").forEach((button) => {{
        button.classList.toggle("active", button.dataset.motionMode === activeMotionMode);
      }});
      drawRoute();
      renderFilteredPoints();
    }};
    const syncEdgeButton = () => {{
      const button = document.getElementById("toggleEdges");
      if (!button) return;
      button.textContent = edgesVisible ? "Hide edges" : "Show edges";
      button.classList.toggle("active", edgesVisible);
    }};
    const syncArrowButton = () => {{
      const button = document.getElementById("toggleArrows");
      if (!button) return;
      button.textContent = arrowsVisible ? "Hide arrows" : "Show arrows";
      button.classList.toggle("active", arrowsVisible);
    }};
    const setEdgesVisible = (visible) => {{
      edgesVisible = visible;
      if (edgesVisible) edgeLayer.addTo(map);
      else edgeLayer.remove();
      drawEdges();
      syncEdgeButton();
    }};
    const setArrowsVisible = (visible) => {{
      arrowsVisible = visible;
      if (arrowsVisible) arrowLayer.addTo(map);
      else arrowLayer.remove();
      drawEdges();
      syncArrowButton();
    }};
    const syncLabelButtons = () => {{
      const stopButton = document.getElementById("toggleStopLabels");
      if (stopButton) {{
        stopButton.textContent = stopLabelsVisible ? "Hide stop labels" : "Show stop labels";
        stopButton.classList.toggle("active", stopLabelsVisible);
      }}
      const placeButton = document.getElementById("togglePlaceLabels");
      if (placeButton) {{
        placeButton.textContent = placeLabelsVisible ? "Hide point labels" : "Show point labels";
        placeButton.classList.toggle("active", placeLabelsVisible);
      }}
    }};
    const syncFilteredPointsButton = () => {{
      const button = document.getElementById("toggleFilteredPoints");
      if (!button) return;
      const count = filteredTrackPointsForMode().length;
      button.textContent = filteredPointsVisible ? `Hide filtered points (${{count}})` : `Show filtered points (${{count}})`;
      button.classList.toggle("active", filteredPointsVisible);
      button.disabled = count === 0;
    }};
    const renderFilteredPoints = () => {{
      filteredPointLayer.clearLayers();
      const points = filteredTrackPointsForMode();
      if (!filteredPointsVisible || !points.length) {{
        filteredPointLayer.remove();
        syncFilteredPointsButton();
        return;
      }}
      for (const point of points) {{
        const lat = Number(point.lat);
        const lon = Number(point.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        L.circleMarker([lat, lon], {{
          radius: Math.max(3, routePointRadius() + 1),
          color: "#111827",
          fillColor: "#f8fafc",
          fillOpacity: 0.7,
          opacity: 0.85,
          weight: 1.5,
          dashArray: "3 3",
        }}).bindPopup(filteredPointPopupHtml(point)).addTo(filteredPointLayer);
      }}
      filteredPointLayer.addTo(map);
      syncFilteredPointsButton();
    }};
    const setFilteredPointsVisible = (visible) => {{
      filteredPointsVisible = visible;
      renderFilteredPoints();
    }};
    const applyLabelVisibility = () => {{
      for (const stop of data.stops) {{
        const marker = markers.get(stop.alias);
        if (!marker) continue;
        marker.setTooltipContent(escapeHtml(stopLabelTextForZoom(stop)));
      }}
      for (const marker of markers.values()) {{
        if (stopLabelsVisible && stopLabelMode() !== "hidden") marker.openTooltip();
        else marker.closeTooltip();
      }}
      for (const marker of placeMarkers) {{
        if (placeLabelsVisible && placeLabelsAllowedByZoom()) marker.openTooltip();
        else marker.closeTooltip();
      }}
      syncLabelButtons();
    }};
    const setStopLabelsVisible = (visible) => {{
      stopLabelsVisible = visible;
      applyLabelVisibility();
    }};
    const setPlaceLabelsVisible = (visible) => {{
      placeLabelsVisible = visible;
      applyLabelVisibility();
    }};
    const refreshZoomSensitiveMarkers = () => {{
      for (const stop of data.stops) refreshStop(stop, false);
      const radius = placeMarkerRadius();
      for (const marker of placeMarkers) {{
        if (marker.setRadius) marker.setRadius(radius);
      }}
      applyLabelVisibility();
    }};
    const syncRouteColorButtons = () => {{
      document.getElementById("routeMotion")?.classList.toggle("active", routeColorMode === "motion");
      document.getElementById("routeSpeed")?.classList.toggle("active", routeColorMode === "speed");
      document.getElementById("routeElevationBands")?.classList.toggle("active", routeColorMode === "bands");
      document.getElementById("routeElevationSlope")?.classList.toggle("active", routeColorMode === "slope");
    }};
    const setRouteColorMode = (mode) => {{
      resetRouteAnimation();
      routeColorMode = routeColorModes.includes(mode) ? mode : "motion";
      syncRouteColorButtons();
      drawRoute();
      renderElevationProfile();
    }};
    const syncProfileAxisButtons = () => {{
      document.getElementById("profileDistance")?.classList.toggle("active", profileAxis === "distance");
      document.getElementById("profileTime")?.classList.toggle("active", profileAxis === "time");
    }};
    const formatProfileValue = (value) => {{
      if (profileAxis === "time") return `${{value.toFixed(1)}} h`;
      return `${{value.toFixed(1)}} km`;
    }};
    const renderElevationProfile = () => {{
      if (!elevationChart || !elevationSummary) return;
      const samples = visibleTrackPoints().filter((point) => Number.isFinite(point.alt_m));
      if (samples.length < 2) {{
        elevationChart.innerHTML = '<text x="300" y="82" text-anchor="middle" fill="#64748b" font-size="13">No altitude data</text>';
        elevationSummary.textContent = "No altitude samples in this track.";
        return;
      }}
      let ascent = 0;
      let descent = 0;
      for (let index = 1; index < samples.length; index += 1) {{
        const previousAlt = Number(samples[index - 1].alt_m);
        const currentAlt = Number(samples[index].alt_m);
        if (!Number.isFinite(previousAlt) || !Number.isFinite(currentAlt)) continue;
        const delta = currentAlt - previousAlt;
        if (delta > 0) ascent += delta;
        else descent += Math.abs(delta);
      }}
      const profilePoints = samples.map((point) => {{
        const x = profileAxis === "time"
          ? ((Number(point.timestamp) || 0) - (timeOrigin || 0)) / 3600
          : Number(point.cumulativeDistanceKm) || 0;
        return {{ x, alt: Number(point.alt_m) }};
      }});
      const minX = Math.min(...profilePoints.map((point) => point.x));
      const maxX = Math.max(...profilePoints.map((point) => point.x));
      const minY = Math.min(...profilePoints.map((point) => point.alt));
      const maxY = Math.max(...profilePoints.map((point) => point.alt));
      const width = 600;
      const height = 160;
      const pad = {{ left: 46, right: 10, top: 10, bottom: 26 }};
      const spanX = Math.max(0.001, maxX - minX);
      const spanY = Math.max(1, maxY - minY);
      const sx = (x) => pad.left + ((x - minX) / spanX) * (width - pad.left - pad.right);
      const sy = (y) => height - pad.bottom - ((y - minY) / spanY) * (height - pad.top - pad.bottom);
      const line = profilePoints.map((point, index) => `${{index ? "L" : "M"}} ${{sx(point.x).toFixed(1)}} ${{sy(point.alt).toFixed(1)}}`).join(" ");
      const area = `${{line}} L ${{sx(maxX).toFixed(1)}} ${{sy(minY).toFixed(1)}} L ${{sx(minX).toFixed(1)}} ${{sy(minY).toFixed(1)}} Z`;
      const gridLines = Array.from({{ length: 4 }}, (_, index) => {{
        const value = minY + ((index + 1) / 5) * spanY;
        const y = sy(value).toFixed(1);
        return `
          <line x1="${{pad.left}}" y1="${{y}}" x2="${{width - pad.right}}" y2="${{y}}" stroke="#e2e8f0" stroke-width="1" />
          <text x="6" y="${{Number(y) + 4}}" fill="#64748b" font-size="11">${{Math.round(value)}} m</text>
        `;
      }}).join("");
      const axisLabelLeft = formatProfileValue(minX);
      const axisLabelRight = formatProfileValue(maxX);
      elevationChart.innerHTML = `
        <rect x="0" y="0" width="${{width}}" height="${{height}}" fill="#f8fafc" rx="6"></rect>
        ${{gridLines}}
        <path d="${{area}}" fill="rgba(37, 99, 235, 0.12)" stroke="none"></path>
        <path d="${{line}}" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"></path>
        <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#94a3b8" stroke-width="1" />
        <text x="${{pad.left}}" y="${{height - 8}}" fill="#64748b" font-size="11">${{axisLabelLeft}}</text>
        <text x="${{width - pad.right}}" y="${{height - 8}}" text-anchor="end" fill="#64748b" font-size="11">${{axisLabelRight}}</text>
      `;
      elevationSummary.textContent = [
        `min ${{Math.round(minY)}} m`,
        `max ${{Math.round(maxY)}} m`,
        `gain ${{Math.round(ascent)}} m`,
        `loss ${{Math.round(descent)}} m`,
      ].join(" · ");
    }};
    const stopMarkerSize = () => {{
      const zoom = map.getZoom();
      if (zoom <= 12) return 8;
      if (zoom <= 15) return 12;
      return 18;
    }};
    const stopLabelMode = () => {{
      const zoom = map.getZoom();
      if (zoom <= 12) return "hidden";
      if (zoom <= 15) return "alias";
      return "short";
    }};
    const placeLabelsAllowedByZoom = () => map.getZoom() >= 14;
    const placeMarkerRadius = () => {{
      const zoom = map.getZoom();
      if (zoom <= 12) return 3;
      if (zoom <= 15) return 4.5;
      return 6;
    }};
    const stopLabelTextForZoom = (stop) => {{
      const mode = stopLabelMode();
      if (mode === "alias") return stop.alias;
      return shortLabelFor(stop);
    }};
    const iconFor = (stop) => {{
      const size = stopMarkerSize();
      const wrapperSize = size + 6;
      const anchor = wrapperSize / 2;
      return L.divIcon({{
      className: "",
      html: `<div style="background:${{selected.has(stop.alias) ? "#f59e0b" : "#dc2626"}};border:2px solid white;border-radius:999px;box-shadow:0 1px 7px rgb(0 0 0 / .35);height:${{size}}px;width:${{size}}px"></div>`,
      iconSize: [wrapperSize, wrapperSize],
      iconAnchor: [anchor, anchor]
      }});
    }};
    const shorten = (value, maxLength = 18) => {{
      const text = String(value == null ? "" : value);
      return text.length > maxLength ? text.slice(0, maxLength - 3) + "..." : text;
    }};
    const labelFor = (stop) => `${{stop.alias}}: ${{stop.name}}`;
    const shortLabelFor = (stop) => `${{stop.alias}}: ${{shorten(stop.name)}}`;
    const popupFor = (stop) => `
      <div class="stop-popup">
        <strong>${{escapeHtml(labelFor(stop))}}</strong>
        <div class="popup-meta">
          ${{escapeHtml(stop.start)}} to ${{escapeHtml(stop.end)}}<br>
          ${{escapeHtml(stop.duration)}} · ${{escapeHtml(stop.points)}} points<br>
          <a href="${{escapeHtml(stop.maps)}}" target="_blank" rel="noreferrer">Google Maps</a>
        </div>
        <label for="popup-name-${{escapeHtml(stop.alias)}}">Name</label>
        <input id="popup-name-${{escapeHtml(stop.alias)}}" data-popup-name="${{escapeHtml(stop.alias)}}" value="${{escapeHtml(stop.name)}}">
        <label for="popup-tags-${{escapeHtml(stop.alias)}}">Tags</label>
        <input id="popup-tags-${{escapeHtml(stop.alias)}}" data-popup-tags="${{escapeHtml(stop.alias)}}" value="${{escapeHtml((stop.tags || []).join(" "))}}" placeholder="tags">
        <label for="popup-note-${{escapeHtml(stop.alias)}}">Note</label>
        <textarea id="popup-note-${{escapeHtml(stop.alias)}}" data-popup-note="${{escapeHtml(stop.alias)}}" placeholder="note">${{escapeHtml(stop.note || "")}}</textarea>
      </div>
    `;
    const attachPopupHandlers = (stop) => {{
      const marker = markers.get(stop.alias);
      const popup = marker && marker.getPopup ? marker.getPopup() : null;
      const element = popup && popup.getElement ? popup.getElement() : null;
      if (!element) return;
      L.DomEvent.disableClickPropagation(element);
    }};
    const refreshStop = (stop, refreshPopup = true) => {{
      const marker = markers.get(stop.alias);
      if (!marker) return;
      marker.setIcon(iconFor(stop));
      marker.setTooltipContent(escapeHtml(stopLabelTextForZoom(stop)));
      if (stopLabelsVisible && stopLabelMode() !== "hidden") marker.openTooltip();
      else marker.closeTooltip();
      const element = marker.getElement ? marker.getElement() : null;
      if (element) element.setAttribute("title", labelFor(stop));
      if (refreshPopup) marker.setPopupContent(popupFor(stop));
    }};
    const applyPopupEdit = (target) => {{
      const alias = target.dataset.popupName || target.dataset.popupTags || target.dataset.popupNote;
      if (!alias) return;
      const stop = data.stops.find((item) => item.alias === alias);
      if (!stop) return;
      if (target.dataset.popupName) {{
        stop.name = target.value.trim() || originalNames.get(stop.alias);
        refreshStop(stop, false);
      }} else if (target.dataset.popupTags) {{
        stop.tags = parseTags(target.value);
      }} else if (target.dataset.popupNote) {{
        stop.note = target.value.trim();
      }}
      updateCommands();
    }};
    const handlePopupEditEvent = (event) => {{
      const target = event.target;
      if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) return;
      if (!target.matches("[data-popup-name], [data-popup-tags], [data-popup-note]")) return;
      applyPopupEdit(target);
    }};
    document.addEventListener("input", handlePopupEditEvent);
    document.addEventListener("change", handlePopupEditEvent);
    const renderList = () => {{
      const stopList = document.getElementById("stopList");
      stopList.innerHTML = data.stops.map((stop) => `
        <div class="stop-row ${{selected.has(stop.alias) ? "selected" : ""}}">
          <div class="stop-head">
            <input type="checkbox" data-check="${{escapeHtml(stop.alias)}}" ${{selected.has(stop.alias) ? "checked" : ""}}>
            <span class="alias">${{escapeHtml(stop.alias)}}</span>
            <a href="${{escapeHtml(stop.maps)}}" target="_blank" rel="noreferrer">Google Maps</a>
          </div>
          <input data-name="${{escapeHtml(stop.alias)}}" value="${{escapeHtml(stop.name)}}">
          <input data-tags="${{escapeHtml(stop.alias)}}" value="${{escapeHtml((stop.tags || []).join(" "))}}" placeholder="tags">
          <textarea data-note="${{escapeHtml(stop.alias)}}" placeholder="note">${{escapeHtml(stop.note || "")}}</textarea>
          <div class="meta">${{escapeHtml(stop.start)}} to ${{escapeHtml(stop.end)}} · ${{escapeHtml(stop.duration)}} · ${{escapeHtml(stop.points)}} points</div>
        </div>
      `).join("");
      stopList.querySelectorAll("[data-check]").forEach((input) => {{
        input.addEventListener("change", () => setSelected(input.dataset.check, input.checked, true));
      }});
      stopList.querySelectorAll("[data-name]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.name);
          if (!stop) return;
          stop.name = input.value.trim() || originalNames.get(stop.alias);
          refreshStop(stop);
          updateCommands();
        }});
      }});
      stopList.querySelectorAll("[data-tags]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.tags);
          if (!stop) return;
          stop.tags = parseTags(input.value);
          updateCommands();
        }});
      }});
      stopList.querySelectorAll("[data-note]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.note);
          if (!stop) return;
          stop.note = input.value.trim();
          updateCommands();
        }});
      }});
    }};
    const refreshSelectedStops = () => {{
      for (const stop of data.stops) refreshStop(stop);
      renderList();
      updateCommands();
    }};
    const centerStop = (stop) => {{
      map.panTo([stop.lat, stop.lon]);
    }};
    const addFitPoint = (lat, lon) => {{
      const latNum = Number(lat);
      const lonNum = Number(lon);
      if (!Number.isFinite(latNum) || !Number.isFinite(lonNum)) return;
      fitPoints.push([latNum, lonNum]);
      fitMinLat = Math.min(fitMinLat, latNum);
      fitMaxLat = Math.max(fitMaxLat, latNum);
      fitMinLon = Math.min(fitMinLon, lonNum);
      fitMaxLon = Math.max(fitMaxLon, lonNum);
      fitCount += 1;
    }};
    const setSelected = (alias, isSelected, center = false) => {{
      const stop = data.stops.find((item) => item.alias === alias);
      if (!stop) return;
      if (isSelected) selected.add(alias);
      else selected.delete(alias);
      refreshStop(stop);
      renderList();
      updateCommands();
      if (isSelected && center) centerStop(stop);
    }};
    const toggleStop = (stop) => {{
      setSelected(stop.alias, !selected.has(stop.alias), true);
    }};
    if (data.track.length) {{
      drawRoute();
      renderRideSegments();
      data.track.forEach((point) => addFitPoint(point[0], point[1]));
    }}
    for (const place of data.namedPlaces) {{
      const label = `${{place.action || ""}} ${{place.name}}`.trim();
      const marker = L.circleMarker([place.lat, place.lon], {{ radius: placeMarkerRadius(), color: "#2563eb", fillColor: "#2563eb", fillOpacity: 1, weight: 2 }}).addTo(map);
      placeMarkers.push(marker);
      marker.bindTooltip(escapeHtml(label), {{ permanent: true, direction: "right", className: "place-label" }});
      marker.bindPopup(`<strong>${{escapeHtml(label)}}</strong><br>${{escapeHtml(place.time)}}`);
      addFitPoint(place.lat, place.lon);
    }}
    for (const stop of data.stops) {{
      const marker = L.marker([stop.lat, stop.lon], {{ icon: iconFor(stop) }}).addTo(map);
      markers.set(stop.alias, marker);
      marker.bindTooltip(escapeHtml(shortLabelFor(stop)), {{ permanent: true, direction: "top", offset: [0, -12], className: "stop-label" }});
      marker.bindPopup(popupFor(stop), {{ className: "stop-popup-shell", maxWidth: 320 }});
      marker.on("click", () => {{
        toggleStop(stop);
        marker.openPopup();
      }});
      marker.on("mouseover", () => {{
        if (!stopLabelsVisible || stopLabelMode() === "hidden") return;
        marker.setTooltipContent(escapeHtml(labelFor(stop)));
      }});
      marker.on("mouseout", () => {{
        marker.setTooltipContent(escapeHtml(stopLabelTextForZoom(stop)));
        if (!stopLabelsVisible || stopLabelMode() === "hidden") marker.closeTooltip();
      }});
      marker.on("popupopen", () => attachPopupHandlers(stop));
      addFitPoint(stop.lat, stop.lon);
      refreshStop(stop);
    }}
    if (fitCount) {{
      if (fitCount === 1) {{
        map.setView([fitMinLat, fitMinLon], zoomAtLeast(14));
      }} else {{
        map.fitBounds([[fitMinLat, fitMinLon], [fitMaxLat, fitMaxLon]], {{ padding: [70, 70] }});
      }}
    }} else {{
      map.setView([0, 0], 2);
      document.body.insertAdjacentHTML("beforeend", `<div class="empty"><strong>No OwnTracks points for ${{escapeHtml(data.date)}}</strong><br>Try another date.</div>`);
    }}
    document.getElementById("applyName").addEventListener("click", () => {{
      const value = document.getElementById("bulkName").value.trim();
      if (!value) return;
      selectedStops().forEach((stop) => stop.name = value);
      refreshSelectedStops();
    }});
    document.getElementById("selectAll").addEventListener("click", () => {{
      data.stops.forEach((stop) => selected.add(stop.alias));
      refreshSelectedStops();
    }});
    document.getElementById("clearSelection").addEventListener("click", () => {{
      selected.clear();
      refreshSelectedStops();
    }});
    document.getElementById("prevDay").addEventListener("click", () => {{
      navigateDay(-1);
    }});
    document.getElementById("nextDay").addEventListener("click", () => {{
      navigateDay(1);
    }});
    document.getElementById("centerSelected").addEventListener("click", () => {{
      const stop = selectedStops()[0];
      if (stop) centerStop(stop);
    }});
    document.getElementById("toggleEdges").addEventListener("click", () => {{
      setEdgesVisible(!edgesVisible);
    }});
    document.getElementById("toggleArrows").addEventListener("click", () => {{
      setArrowsVisible(!arrowsVisible);
    }});
    document.getElementById("toggleStopLabels").addEventListener("click", () => {{
      setStopLabelsVisible(!stopLabelsVisible);
    }});
    document.getElementById("togglePlaceLabels").addEventListener("click", () => {{
      setPlaceLabelsVisible(!placeLabelsVisible);
    }});
    document.getElementById("toggleFilteredPoints").addEventListener("click", () => {{
      setFilteredPointsVisible(!filteredPointsVisible);
    }});
    document.getElementById("routeAnimPlay").addEventListener("click", () => {{
      playRouteAnimation();
    }});
    document.getElementById("routeAnimReset").addEventListener("click", () => {{
      resetRouteAnimation();
    }});
    document.getElementById("routeAnimDuration").addEventListener("change", () => {{
      routeAnimationElapsedMs = Math.min(routeAnimationElapsedMs, routeAnimationDurationMs());
      if (!routeAnimationRunning) renderRouteAnimation(routeAnimationElapsedMs);
    }});
    document.getElementById("routeMotion").addEventListener("click", () => {{
      setRouteColorMode("motion");
    }});
    document.getElementById("routeSpeed").addEventListener("click", () => {{
      setRouteColorMode("speed");
    }});
    document.getElementById("routeElevationBands").addEventListener("click", () => {{
      setRouteColorMode("bands");
    }});
    document.getElementById("routeElevationSlope").addEventListener("click", () => {{
      setRouteColorMode("slope");
    }});
    document.getElementById("profileDistance").addEventListener("click", () => {{
      profileAxis = "distance";
      syncProfileAxisButtons();
      renderElevationProfile();
    }});
    document.getElementById("profileTime").addEventListener("click", () => {{
      profileAxis = "time";
      syncProfileAxisButtons();
      renderElevationProfile();
    }});
    document.querySelectorAll("[data-motion-mode]").forEach((button) => {{
      button.addEventListener("click", () => {{
        setActiveMotionMode(button.dataset.motionMode || "all");
      }});
    }});
    map.on("zoomend", () => {{
      drawRoute();
      renderFilteredPoints();
      refreshZoomSensitiveMarkers();
      if (!routeAnimationRunning && routeAnimationElapsedMs > 0) renderRouteAnimation(routeAnimationElapsedMs);
    }});
    syncEdgeButton();
    syncArrowButton();
    syncDayNavigationButtons();
    syncLabelButtons();
    syncFilteredPointsButton();
    applyLabelVisibility();
    syncRouteColorButtons();
    syncProfileAxisButtons();
    syncRouteAnimationButton();
    routeAnimationStatus("ready");
    renderRouteLegend();
    setActiveMotionMode("all");
    setEdgesVisible(true);
    setArrowsVisible(true);
    setFilteredPointsVisible(false);
    renderElevationProfile();
    document.getElementById("selectNearby").addEventListener("click", () => {{
      const picked = selectedStops()[0] || data.stops[0];
      const threshold = Number(document.getElementById("groupDistance").value) || 150;
      if (!picked) return;
      selected.clear();
      data.stops.filter((stop) => distanceMeters(picked, stop) <= threshold).forEach((stop) => selected.add(stop.alias));
      refreshSelectedStops();
      centerStop(picked);
    }});
    document.getElementById("groupSelected").addEventListener("click", () => {{
      const stops = selectedStops();
      if (!stops.length) return;
      const existing = stops.find((stop) => !/^unnamed-stop-\\d+$/.test(stop.name));
      const name = document.getElementById("bulkName").value.trim() || (existing ? existing.name : stops[0].name);
      stops.forEach((stop) => stop.name = name);
      document.getElementById("bulkName").value = name;
      refreshSelectedStops();
    }});
    document.getElementById("copyCommands").addEventListener("click", async () => {{
      updateCommands();
      await copyCommandsToClipboard(false);
    }});
    document.getElementById("fitAll").addEventListener("click", () => {{
      if (fitPoints.length === 1) {{
        map.setView(fitPoints[0], zoomAtLeast(14));
      }} else if (fitPoints.length) {{
        map.fitBounds(L.latLngBounds(fitPoints).pad(0.18));
      }}
    }});
    map.on("zoomend moveend", updateStatus);
    tiles.on("tileloadstart tileload tileerror", updateStatus);
    toggleToolsButton.addEventListener("click", () => {{
      tools.classList.toggle("collapsed");
      toggleToolsButton.textContent = tools.classList.contains("collapsed") ? "Tools" : "Hide";
      setTimeout(() => map.invalidateSize(), 0);
    }});
    if (window.innerWidth <= 700) {{
      tools.classList.add("collapsed");
      toggleToolsButton.textContent = "Tools";
    }}
    renderList();
    updateCommands(false);
  </script>
</body>
</html>"""


def build_plan(
    events: list[Event],
    target_date: date,
    user_tags: dict | None = None,
    home_filter: HomeFilterConfig | None = None,
    stop_jitter_filter: StopJitterFilterConfig | None = None,
) -> tuple[dict, list[Event]]:
    day_events = [event for event in events if event_date(event) == target_date]
    day_events.sort(key=event_time)
    start, end, basis = infer_ride_window(day_events)
    if start is None and end is None and day_events:
        start = event_time(day_events[0])
        end = event_time(day_events[-1])
        basis = "full day activity review"
    window_events = [event for event in day_events if in_window(event, start, end)]
    track_points = [event for event in window_events if event.is_location]
    ride_points = [event for event in track_points if is_moving_ride_point(event)]
    places = named_place_events(window_events)
    stops = candidate_stops(window_events)
    home_anchor_points = home_anchors(events, home_filter)
    stop_jitter_anchor_points = stop_jitter_anchors(events, stops, stop_jitter_filter)
    if stop_jitter_filter and stop_jitter_filter.enabled:
        preserve_lines = {
            int(line)
            for stop in stops
            for line in (stop.get("start_line"), stop.get("end_line"))
            if isinstance(line, int)
        }
        visual_track_points, filtered_stop_jitter_points = filter_stop_jitter_points(
            track_points,
            stop_jitter_filter,
            stop_jitter_anchor_points,
            preserve_lines,
        )
        filtered_home_points = 0
    else:
        visual_track_points, filtered_home_points = filter_home_area_points(track_points, home_filter, home_anchor_points)
        filtered_stop_jitter_points = 0
    speeds = [event.speed_kmh for event in track_points if event.speed_kmh is not None]
    batteries = [event.payload.get("batt") for event in track_points if event.payload.get("batt") is not None]
    motion = motion_summary(track_points)
    elevation = summarize_elevation(track_points)
    place_tags = [tag for place in places for tag in place["tags"]]
    stop_tags = [tag for stop in stops for tag in stop["tags"]]
    recommended_tags = ["activity:daily-review", "source:owntracks", *place_tags, *stop_tags]
    if ride_points:
        recommended_tags.append("activity:cycle-ride")
    plan = {
        "date": target_date.isoformat(),
        "activity_window": {"start": fmt_dt(start), "end": fmt_dt(end), "basis": basis},
        "ride_window": {"start": fmt_dt(start), "end": fmt_dt(end), "basis": basis},
        "stats": {
            "events_on_day": len(day_events),
            "events_in_window": len(window_events),
            "track_points": len(track_points),
            "visual_track_points": len(visual_track_points),
            "filtered_home_points": filtered_home_points,
            "filtered_stop_jitter_points": filtered_stop_jitter_points,
            "ride_points": len(ride_points),
            "approx_distance_km": round(summarize_distance(track_points), 2),
            "max_speed_kmh": max(speeds) if speeds else None,
            "battery_start": batteries[0] if batteries else None,
            "battery_end": batteries[-1] if batteries else None,
        },
        "motion_summary": motion,
        "elevation_summary": elevation,
        "recommended_tags": sorted(set(recommended_tags)),
        "named_places": places,
        "candidate_stops": stops,
        "raw_sampled_track": [point_dict(event) for event in track_points],
        "sampled_track": [point_dict(event) for event in visual_track_points],
        "home_filter": {
            "enabled": bool(home_filter and home_filter.enabled),
            "radius_m": home_filter.radius_m if home_filter and home_filter.enabled else None,
            "anchor_count": len(home_anchor_points),
            "filtered_points": filtered_home_points,
        },
        "stop_jitter_filter": {
            "enabled": bool(stop_jitter_filter and stop_jitter_filter.enabled),
            "radius_m": stop_jitter_filter.radius_m if stop_jitter_filter and stop_jitter_filter.enabled else None,
            "anchor_count": len(stop_jitter_anchor_points),
            "filtered_points": filtered_stop_jitter_points,
            "min_dwell_minutes": (
                stop_jitter_filter.min_dwell_minutes
                if stop_jitter_filter and stop_jitter_filter.enabled
                else None
            ),
        },
    }
    plan = apply_user_tags(plan, user_tags or {})
    for index, stop in enumerate(plan["candidate_stops"], start=1):
        stop["alias"] = f"s{index}"
    plan["ride_segments"] = build_ride_segments(track_points, plan["candidate_stops"], places)
    return plan, track_points


def target_date_from_text(value: str | None, local_tz: ZoneInfo) -> date:
    today = datetime.now(local_tz).date()
    text = (value or "").strip().lower()
    if not text or text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    if re.fullmatch(r"\d{1,2}", text):
        return date(today.year, today.month, int(text))
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
    if match:
        month, day = (int(part) for part in match.groups())
        return date(today.year, month, day)
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day)
    return date.fromisoformat(text)


def target_scope_from_text(value: str | None, local_tz: ZoneInfo) -> OwnTracksScope:
    today = datetime.now(local_tz).date()
    text = (value or "").strip().lower()
    if not text or text == "today":
        return OwnTracksScope("day", today.isoformat(), today, today)
    if text == "yesterday":
        target = today - timedelta(days=1)
        return OwnTracksScope("day", target.isoformat(), target, target)
    if re.fullmatch(r"\d{1,2}", text):
        target = date(today.year, today.month, int(text))
        return OwnTracksScope("day", target.isoformat(), target, target)
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
    if match:
        month, day = (int(part) for part in match.groups())
        target = date(today.year, month, day)
        return OwnTracksScope("day", target.isoformat(), target, target)
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        target = date(year, month, day)
        return OwnTracksScope("day", target.isoformat(), target, target)
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if match:
        year, month = (int(part) for part in match.groups())
        last_day = calendar.monthrange(year, month)[1]
        start = date(year, month, 1)
        end = date(year, month, last_day)
        return OwnTracksScope("month", f"{year:04d}-{month:02d}", start, end)
    match = re.fullmatch(r"(\d{4})", text)
    if match:
        year = int(match.group(1))
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        return OwnTracksScope("year", f"{year:04d}", start, end)
    target = date.fromisoformat(text)
    return OwnTracksScope("day", target.isoformat(), target, target)


def render_digest(plan: dict) -> str:
    window = plan.get("activity_window") or plan["ride_window"]

    def is_reviewed_stop(stop: dict) -> bool:
        return bool(stop.get("reviewed_name") or stop.get("user_tags") or stop.get("user_note"))

    def append_stop_lines(stop: dict) -> None:
        display_name = stop.get("reviewed_name") or stop["name"]
        lines.append(f"- {stop['alias']} ({stop['id']}): {display_name}")
        lines.append(f"  Time: {stop['start']} to {stop['end']} ({stop['duration']})")
        lines.append(f"  Motion: {stop['motion']} | Points: {stop['points']}")
        lines.append(f"  Map: {stop['maps']}")
        if stop.get("user_tags"):
            lines.append(f"  Saved tags: {', '.join(stop['user_tags'])}")
        if stop.get("user_note"):
            lines.append(f"  Note: {stop['user_note']}")

    lines = [
        f"OwnTracks activity digest - {plan['date']}",
        "",
        f"Review window: {window['start']} to {window['end']}",
        f"Basis: {window['basis']}",
        f"Location points: {plan['stats']['track_points']} | Movement points: {plan['stats']['ride_points']}",
        f"Approx sampled distance: {plan['stats']['approx_distance_km']} km",
        f"Max logged speed: {plan['stats']['max_speed_kmh']} km/h",
        f"Battery: {plan['stats']['battery_start']}% to {plan['stats']['battery_end']}%",
        "",
        "Named places",
    ]
    stop_jitter_filter = plan.get("stop_jitter_filter") or {}
    home_filter = plan.get("home_filter") or {}
    if stop_jitter_filter.get("enabled") and stop_jitter_filter.get("filtered_points"):
        lines.insert(
            9,
            "Visualization filter: "
            f"hid {stop_jitter_filter['filtered_points']} stop-jitter points near "
            f"{stop_jitter_filter.get('anchor_count')} anchors within {stop_jitter_filter.get('radius_m')} m",
        )
    elif home_filter.get("enabled") and home_filter.get("filtered_points"):
        lines.insert(
            9,
            f"Visualization filter: hid {home_filter['filtered_points']} home-area points within {home_filter.get('radius_m')} m",
        )
    if not plan["named_places"]:
        lines.append("- None")
    for place in plan["named_places"]:
        lines.append(f"- {place['time']}: {place['action']} {place['name']}")
        if place.get("maps"):
            lines.append(f"  {place['maps']}")

    lines.extend(["", "Ride segments"])
    if not plan.get("ride_segments"):
        lines.append("- None")
    for segment in plan.get("ride_segments", []):
        lines.append(f"- {segment['id']}: {segment['label']}")
        lines.append(
            "  "
            f"{segment['distance_km']} km | elapsed {segment['duration']} | moving {segment['moving_duration']} | "
            f"moving avg {segment.get('moving_average_speed_kmh')} km/h | max {segment.get('max_speed_kmh')} km/h"
        )
        lines.append(
            "  "
            f"Motion: {segment.get('dominant_moving_motion') or segment.get('dominant_motion')} | "
            f"Points: {segment['point_count']} | {segment['start_time']} to {segment['end_time']}"
        )

    reviewed_stops = [stop for stop in plan["candidate_stops"] if is_reviewed_stop(stop)]
    pending_stops = [stop for stop in plan["candidate_stops"] if not is_reviewed_stop(stop)]
    lines.extend(["", "Reviewed stops"])
    if not reviewed_stops:
        lines.append("- None")
    for stop in reviewed_stops:
        append_stop_lines(stop)

    lines.extend(["", "Stops to review"])
    if not pending_stops:
        lines.append("- None")
    for stop in pending_stops:
        append_stop_lines(stop)

    lines.extend(["", "Tags"])
    lines.append(", ".join(plan["recommended_tags"]))
    lines.extend(
        [
            "",
            "Commands:",
            "/otd [today|yesterday|DD|MM-DD|YYYY-MM-DD]",
            "/otm [today|yesterday|DD|MM-DD|YYYY-MM-DD]",
            "/otme [today|yesterday|DD|MM-DD|YYYY-MM-DD]",
            "/otb DD|MM-DD|YYYY-MM-DD",
            "/ott s1 tag1 tag2",
            "/otn s1 place name",
            "/oto s1 what happened there",
        ]
    )
    return "\n".join(lines)
