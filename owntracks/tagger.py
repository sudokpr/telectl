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
from urllib.parse import quote, urlencode

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

DEFAULT_VISIT_RADIUS_M = 180
VISIT_BOUNDARY_INTERPOLATION_MAX_GAP_SECONDS = 20 * 60


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
        if event.kind not in {"transition", "waypoint"} or event.lat is None or event.lon is None:
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


def distance_to_nearest_anchor_m(event: Event, anchors: list[tuple[float, float, str]]) -> float | None:
    if event.lat is None or event.lon is None or not anchors:
        return None
    return min(haversine_km(event.lat, event.lon, lat, lon) * 1000 for lat, lon, _name in anchors)


def is_near_home_boundary(event: Event, config: HomeFilterConfig | None, anchors: list[tuple[float, float, str]]) -> bool:
    if not config or not config.enabled or not event.is_location:
        return False
    names = home_region_names(config)
    if names and event_region_names(event) & names:
        return True
    distance_m = distance_to_nearest_anchor_m(event, anchors)
    if distance_m is None:
        return False
    return distance_m <= max(float(config.radius_m), 300.0)


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
            if stop.get("manual") and not stop.get("place"):
                continue
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


def stop_jitter_anchor_key(
    event: Event,
    config: StopJitterFilterConfig | None,
    anchors: list[StopJitterAnchor],
) -> tuple[str, str, float, float] | None:
    if not config or not config.enabled or not event.is_location or event.lat is None or event.lon is None:
        return None
    nearest: tuple[float, StopJitterAnchor] | None = None
    for anchor in anchors:
        distance_m = haversine_km(event.lat, event.lon, anchor.lat, anchor.lon) * 1000
        if distance_m > config.radius_m:
            continue
        if nearest is None or distance_m < nearest[0]:
            nearest = (distance_m, anchor)
    if nearest is None:
        return None
    anchor = nearest[1]
    return (anchor.kind, anchor.label.casefold(), round(anchor.lat, 5), round(anchor.lon, 5))


def filter_stop_jitter_points(
    points: list[Event],
    config: StopJitterFilterConfig | None,
    anchors: list[StopJitterAnchor],
    preserve_lines: set[int] | None = None,
) -> tuple[list[Event], int]:
    if not config or not config.enabled:
        return points, 0
    preserve_lines = preserve_lines or set()
    jitter_anchor_keys = [stop_jitter_anchor_key(event, config, anchors) for event in points]
    jitter_flags = [key is not None for key in jitter_anchor_keys]
    keep_indices = {index for index, is_jitter in enumerate(jitter_flags) if not is_jitter}
    if len(points) > 1:
        keep_indices.add(0)
        keep_indices.add(len(points) - 1)

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
        for boundary_index in range(start + 1, end + 1):
            if jitter_anchor_keys[boundary_index] != jitter_anchor_keys[boundary_index - 1]:
                keep_indices.add(boundary_index - 1)
                keep_indices.add(boundary_index)
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
    received_timestamp = int(event.received_at.timestamp()) if event.received_at and event.received_at.tzinfo is not None else None
    recorded_timestamp = int(dt.timestamp()) if dt.tzinfo is not None else None
    altitude = event.payload.get("alt")
    if altitude is None:
        altitude = event.payload.get("ele")
    point = {
        "line": event.line_no,
        "time": fmt_dt(dt),
        "timestamp": recorded_timestamp,
        "received_time": fmt_dt(event.received_at),
        "received_timestamp": received_timestamp,
        "upload_delay_seconds": (
            received_timestamp - recorded_timestamp
            if isinstance(received_timestamp, int) and isinstance(recorded_timestamp, int)
            else None
        ),
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
    poi = str(event.payload.get("poi") or "").strip()
    if poi:
        point["poi"] = poi
    if event.payload.get("imagename"):
        point["imagename"] = str(event.payload.get("imagename") or "")
    if event.payload.get("image"):
        point["has_image"] = True
    return point


def poi_event_dict(event: Event) -> dict | None:
    poi = str(event.payload.get("poi") or "").strip()
    if not poi or not event.is_location:
        return None
    point = point_dict(event)
    point["name"] = poi
    image = str(event.payload.get("image") or "").strip()
    if image:
        point["has_image"] = True
        point["image_data_url"] = f"data:image/jpeg;base64,{image}"
    if event.payload.get("imagename"):
        point["imagename"] = str(event.payload.get("imagename") or "")
    return point


def poi_event_dicts(events: list[Event]) -> list[dict]:
    return [item for event in events if (item := poi_event_dict(event)) is not None]


def point_dicts(events: list[Event], all_events: list[Event]) -> list[dict]:
    points = []
    for event in events:
        point = point_dict(event)
        label = waypoint_name_for(event.lat, event.lon, all_events, use_default_radius=False)
        if label:
            point["place_name"] = label
        points.append(point)
    return points


def apply_stop_labels_to_point_dicts(plan: dict) -> None:
    reviewed_ranges = []
    for stop in plan.get("candidate_stops", []):
        label = str(stop.get("reviewed_name") or "").strip()
        if not stop.get("user_reviewed"):
            continue
        start_timestamp = stop.get("start_timestamp")
        end_timestamp = stop.get("end_timestamp")
        if not label or not isinstance(start_timestamp, int) or not isinstance(end_timestamp, int):
            continue
        reviewed_ranges.append((start_timestamp, end_timestamp, label))
    if not reviewed_ranges:
        return
    for key in ("raw_sampled_track", "sampled_track"):
        for point in plan.get(key, []):
            timestamp = point.get("timestamp")
            if not isinstance(timestamp, int):
                continue
            for start_timestamp, end_timestamp, label in reviewed_ranges:
                if start_timestamp <= timestamp <= end_timestamp:
                    point["place_name"] = label
                    break


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
    leaves = [event_time(event) for event in home_events if event.payload.get("event") == "leave"]
    enters = [event_time(event) for event in home_events if event.payload.get("event") == "enter"]
    prior_leaves = [dt for dt in leaves if dt <= first_ride]
    later_enters = [dt for dt in enters if dt >= last_ride]
    if prior_leaves and later_enters:
        return max(prior_leaves), min(later_enters), "Home leave to Home enter around ride points"
    return None, None, "full day activity review; ride points not bracketed by Home transitions"


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


def stop_anchor_name(stop: dict) -> str:
    return str(stop.get("reviewed_name") or stop.get("name") or stop.get("alias") or "stop")


def travel_anchor_key(name: object) -> str:
    clean = str(name or "").strip()
    return f"name:{clean.casefold()}" if clean else "name:unknown"


def stop_travel_anchors(stops: list[dict], places: list[dict] | None = None) -> list[dict]:
    anchors: list[dict] = []
    for stop in stops:
        start_ts = stop.get("visit_start_timestamp", stop.get("start_timestamp"))
        end_ts = stop.get("visit_end_timestamp", stop.get("end_timestamp"))
        if not isinstance(start_ts, int) or not isinstance(end_ts, int):
            continue
        name = stop_anchor_name(stop)
        anchors.append(
            {
                "entry": start_ts,
                "exit": end_ts,
                "name": name,
                "key": travel_anchor_key(name),
                "kind": "stop",
                "alias": stop.get("alias", ""),
                "id": stop.get("id", ""),
                "lat": stop.get("lat"),
                "lon": stop.get("lon"),
                "entry_status": stop.get("entry_status"),
                "exit_status": stop.get("exit_status"),
                "entry_window": stop.get("entry_window"),
                "exit_window": stop.get("exit_window"),
            }
        )
    for place in places or []:
        timestamp = place.get("timestamp")
        if not isinstance(timestamp, int):
            continue
        name = str(place.get("name") or "place")
        action = str(place.get("action") or "event")
        anchors.append(
            {
                "entry": timestamp,
                "exit": timestamp,
                "name": name,
                "key": travel_anchor_key(name),
                "kind": f"geofence:{action}",
                "alias": "",
                "id": str(place.get("id") or ""),
                "lat": place.get("lat"),
                "lon": place.get("lon"),
            }
        )

    deduped: list[dict] = []
    for anchor in sorted(anchors, key=lambda item: (item["entry"], item["exit"], item["name"], item["kind"])):
        if (
            deduped
            and abs(anchor["entry"] - deduped[-1]["entry"]) <= 30
            and anchor["key"] == deduped[-1]["key"]
        ):
            deduped[-1]["exit"] = max(deduped[-1]["exit"], anchor["exit"])
            deduped[-1]["kind"] = f"{deduped[-1]['kind']}+{anchor['kind']}"
            if anchor.get("alias") and not deduped[-1].get("alias"):
                deduped[-1]["alias"] = anchor.get("alias")
            continue
        deduped.append(anchor)
    return deduped


def build_stop_travel_segments(points: list[Event], stops: list[dict], places: list[dict] | None = None) -> list[dict]:
    anchors = stop_travel_anchors(stops, places)
    for point in points:
        if point.payload.get("t") != "c":
            continue
        regions = [str(region).strip() for region in (point.payload.get("inregions") or []) if str(region).strip()]
        if not regions or point.recorded_at is None:
            continue
        name = regions[0]
        timestamp = int(point.recorded_at.timestamp())
        anchors.append(
            {
                "entry": timestamp,
                "exit": timestamp,
                "name": name,
                "key": travel_anchor_key(name),
                "kind": "connector",
                "alias": "",
                "id": f"connector:{slug(name)}-{point.line_no}",
                "lat": point.lat,
                "lon": point.lon,
            }
        )
    deduped: list[dict] = []
    for anchor in sorted(anchors, key=lambda item: (item["entry"], item["exit"], item["name"], item["kind"])):
        if (
            deduped
            and abs(anchor["entry"] - deduped[-1]["entry"]) <= 30
            and anchor["key"] == deduped[-1]["key"]
        ):
            deduped[-1]["exit"] = max(deduped[-1]["exit"], anchor["exit"])
            deduped[-1]["kind"] = f"{deduped[-1]['kind']}+{anchor['kind']}"
            if anchor.get("alias") and not deduped[-1].get("alias"):
                deduped[-1]["alias"] = anchor.get("alias")
            continue
        deduped.append(anchor)
    anchors = deduped
    segments: list[dict] = []
    for previous_anchor, next_anchor in zip(anchors, anchors[1:]):
        start_ts = previous_anchor["exit"]
        end_ts = next_anchor["entry"]
        if end_ts <= start_ts or previous_anchor["key"] == next_anchor["key"]:
            continue
        segment_points = [
            point
            for point in points
            if point.is_location
            and point.recorded_at is not None
            and start_ts <= int(point.recorded_at.timestamp()) <= end_ts
        ]
        distance_km = round(summarize_distance(segment_points), 2) if len(segment_points) >= 2 else 0.0
        duration_seconds = end_ts - start_ts
        start_name = str(previous_anchor["name"])
        end_name = str(next_anchor["name"])
        segment = {
            "id": f"t{len(segments) + 1}",
            "label": f"{start_name} to {end_name}",
            "start_name": start_name,
            "end_name": end_name,
            "start_key": previous_anchor["key"],
            "end_key": next_anchor["key"],
            "start_alias": previous_anchor.get("alias", ""),
            "end_alias": next_anchor.get("alias", ""),
            "start_kind": previous_anchor.get("kind", ""),
            "end_kind": next_anchor.get("kind", ""),
            "start_status": previous_anchor.get("exit_status"),
            "end_status": next_anchor.get("entry_status"),
            "start_window": previous_anchor.get("exit_window"),
            "end_window": next_anchor.get("entry_window"),
            "start_time": fmt_dt(datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(points[0].local_tz)) if points else "",
            "end_time": fmt_dt(datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone(points[0].local_tz)) if points else "",
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "duration_seconds": duration_seconds,
            "duration_minutes": max(0, round(duration_seconds / 60)),
            "duration": fmt_duration(max(0, round(duration_seconds / 60))),
            "distance_km": distance_km,
            "point_count": len(segment_points),
            "start_lat": previous_anchor.get("lat"),
            "start_lon": previous_anchor.get("lon"),
            "end_lat": next_anchor.get("lat"),
            "end_lon": next_anchor.get("lon"),
        }
        segments.append(segment)
    return segments


def attach_stop_travel_context(stops: list[dict], segments: list[dict]) -> None:
    for stop in stops:
        alias = stop.get("alias")
        if not alias:
            continue
        previous_segment = next((segment for segment in segments if segment.get("end_alias") == alias), None)
        next_segment = next((segment for segment in segments if segment.get("start_alias") == alias), None)
        if previous_segment:
            stop["previous_travel"] = previous_segment
        if next_segment:
            stop["next_travel"] = next_segment


def travel_pair_summaries(segments: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for segment in segments:
        key = (str(segment.get("start_key") or ""), str(segment.get("end_key") or ""))
        if not key[0] or not key[1]:
            continue
        grouped.setdefault(key, []).append(segment)

    summaries: list[dict] = []
    for (_start_key, _end_key), items in grouped.items():
        durations = sorted(int(item.get("duration_seconds") or 0) for item in items if int(item.get("duration_seconds") or 0) > 0)
        if not durations:
            continue
        fastest = min(items, key=lambda item: int(item.get("duration_seconds") or 0))
        average_seconds = round(sum(durations) / len(durations))
        median_seconds = durations[len(durations) // 2] if len(durations) % 2 else round((durations[len(durations) // 2 - 1] + durations[len(durations) // 2]) / 2)
        summaries.append(
            {
                "start_key": fastest.get("start_key"),
                "end_key": fastest.get("end_key"),
                "start_name": fastest.get("start_name"),
                "end_name": fastest.get("end_name"),
                "count": len(durations),
                "min_seconds": durations[0],
                "min_duration": fmt_duration(round(durations[0] / 60)),
                "avg_seconds": average_seconds,
                "avg_duration": fmt_duration(round(average_seconds / 60)),
                "median_seconds": median_seconds,
                "median_duration": fmt_duration(round(median_seconds / 60)),
                "fastest": fastest,
            }
        )
    return sorted(summaries, key=lambda item: (str(item["start_name"]).casefold(), str(item["end_name"]).casefold()))


def unique_location_points(events: list[Event], target_date: date) -> list[Event]:
    points = [event for event in events if event_date(event) == target_date and event.is_location]
    points.sort(key=lambda event: (event_time(event), event.line_no))
    deduped: list[Event] = []
    seen: set[tuple[int, float, float]] = set()
    for event in points:
        timestamp = int(event_time(event).timestamp()) if event_time(event).tzinfo is not None else event.line_no
        key = (timestamp, round(event.lat or 0, 6), round(event.lon or 0, 6))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def trip_place_definitions(
    events: list[Event],
    user_tags: dict | None = None,
    target_date: date | None = None,
    home_filter: HomeFilterConfig | None = None,
) -> list[dict]:
    places: dict[str, dict] = {}

    def add_place(
        name: object,
        lat: object,
        lon: object,
        radius_m: object = None,
        source: str = "place",
        *,
        key: str | None = None,
        display_name: object = None,
        visit: dict | None = None,
    ) -> None:
        label = str(name or "").strip()
        place_lat = as_float(lat)
        place_lon = as_float(lon)
        if not label or place_lat is None or place_lon is None:
            return
        key = key or f"name:{label.casefold()}"
        radius = max(150.0, as_float(radius_m) or 0.0)
        existing = places.get(key)
        if existing is None:
            place = {
                "key": key,
                "name": label,
                "display_name": str(display_name or label).strip() or label,
                "lat": place_lat,
                "lon": place_lon,
                "radius_m": round(radius),
                "sources": [source],
            }
            if visit:
                place.update(visit)
            places[key] = place
            return
        existing["radius_m"] = max(float(existing.get("radius_m") or 0), round(radius))
        if source not in existing["sources"]:
            existing["sources"].append(source)

    for event in events:
        if event.kind == "waypoint":
            add_place(event.payload.get("desc"), event.lat, event.lon, event.payload.get("rad"), "waypoint")
        elif event.kind == "transition":
            add_place(event.payload.get("desc"), event.lat, event.lon, 150, "transition")

    for day_tags in (user_tags or {}).values():
        if not isinstance(day_tags, dict):
            continue
        for saved in (day_tags.get("stops") or {}).values():
            if not isinstance(saved, dict):
                continue
            if not saved.get("place"):
                continue
            add_place(saved.get("name"), saved.get("lat"), saved.get("lon"), 150, "review")

    if target_date is not None:
        day_events = [event for event in events if event_date(event) == target_date]
        day_events.sort(key=event_time)
        stops = candidate_stops(day_events)
        home_anchor_points = home_anchors(events, home_filter)
        stops.extend(boundary_home_stops(day_events, stops, home_filter, home_anchor_points))
        detected_stop_ids = {stop["id"] for stop in stops}
        stops.extend(
            stop
            for stop in manual_stops_for_date(user_tags or {}, target_date.isoformat())
            if stop["id"] not in detected_stop_ids
        )
        stop_overrides = ((user_tags or {}).get(target_date.isoformat(), {}) or {}).get("stops", {})
        stops = [stop for stop in stops if not stop_is_ignored(stop, stop_overrides)]
        annotate_visit_boundaries(stops, [event for event in day_events if event.is_location])
        stops.sort(
            key=lambda stop: (
                int(stop.get("start_timestamp") or 0),
                int(stop.get("start_line") or 0),
                str(stop.get("id") or ""),
            )
        )
        plan = {
            "date": target_date.isoformat(),
            "candidate_stops": stops,
            "recommended_tags": [],
        }
        apply_user_tags(plan, user_tags or {}, events)
        for index, stop in enumerate(plan["candidate_stops"], start=1):
            stop["alias"] = f"s{index}"
            name = stop_anchor_name(stop)
            start = str(stop.get("start") or "").strip()
            end = str(stop.get("end") or "").strip()
            display_name = f"{stop['alias']}: {name}"
            entry_display = str(stop.get("entry_display") or start).strip()
            exit_display = str(stop.get("exit_display") or end).strip()
            if entry_display and exit_display and exit_display != entry_display:
                display_name = f"{display_name} · {entry_display} -> {exit_display}"
            elif entry_display:
                display_name = f"{display_name} · {entry_display}"
            add_place(
                name,
                stop.get("lat"),
                stop.get("lon"),
                stop.get("radius_m") or DEFAULT_VISIT_RADIUS_M,
                "visit",
                key=f"visit:{stop.get('id') or stop['alias']}",
                display_name=display_name,
                visit={
                    "visit_id": stop.get("id") or stop["alias"],
                    "visit_alias": stop["alias"],
                    "visit_start_timestamp": stop.get("visit_start_timestamp", stop.get("start_timestamp")),
                    "visit_end_timestamp": stop.get("visit_end_timestamp", stop.get("end_timestamp")),
                    "visit_start": entry_display,
                    "visit_end": exit_display,
                    "visit_entry_status": stop.get("entry_status"),
                    "visit_exit_status": stop.get("exit_status"),
                    "visit_entry_window": stop.get("entry_window"),
                    "visit_exit_window": stop.get("exit_window"),
                    "manual": bool(stop.get("manual")),
                },
            )

    return sorted(
        places.values(),
        key=lambda item: (
            0 if "visit" in item.get("sources", []) else 1,
            int(item.get("visit_start_timestamp") or 0),
            str(item.get("display_name") or item["name"]).casefold(),
        ),
    )


def point_distance_m(point: Event, place: dict) -> float:
    return haversine_km(float(place["lat"]), float(place["lon"]), point.lat or 0, point.lon or 0) * 1000


def event_timestamp(event: Event) -> int | None:
    dt = event_time(event)
    return int(dt.timestamp()) if dt.tzinfo is not None else None


def timestamp_tz(points: list[Event], fallback: ZoneInfo | timezone = timezone.utc) -> ZoneInfo | timezone:
    return points[0].local_tz if points else fallback


def datetime_from_timestamp_with_tz(timestamp: int, local_tz: ZoneInfo | timezone) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(local_tz)


def fmt_time_only(timestamp: int | None, local_tz: ZoneInfo | timezone) -> str:
    if not isinstance(timestamp, int):
        return "unknown"
    return datetime_from_timestamp_with_tz(timestamp, local_tz).strftime("%H:%M:%S")


def fmt_visit_window(start_timestamp: int | None, end_timestamp: int | None, local_tz: ZoneInfo | timezone) -> str:
    return f"unknown/window {fmt_time_only(start_timestamp, local_tz)}-{fmt_time_only(end_timestamp, local_tz)}"


def visit_radius_m(stop: dict) -> float:
    radius = as_float(stop.get("radius_m"))
    return float(radius or DEFAULT_VISIT_RADIUS_M)


def visit_place_for_stop(stop: dict) -> dict:
    return {
        "lat": stop.get("lat"),
        "lon": stop.get("lon"),
        "radius_m": visit_radius_m(stop),
    }


def sample_for_line(points: list[Event], line_no: object) -> Event | None:
    try:
        target = int(line_no)
    except (TypeError, ValueError):
        return None
    return next((point for point in points if point.line_no == target), None)


def boundary_gap_seconds(left: Event, right: Event) -> float:
    return abs((event_time(right) - event_time(left)).total_seconds())


def interpolation_allowed(left: Event, right: Event) -> bool:
    return boundary_gap_seconds(left, right) <= VISIT_BOUNDARY_INTERPOLATION_MAX_GAP_SECONDS


def annotated_visit_duration(stop: dict) -> None:
    start_ts = stop.get("visit_start_timestamp", stop.get("start_timestamp"))
    end_ts = stop.get("visit_end_timestamp", stop.get("end_timestamp"))
    if not isinstance(start_ts, int) or not isinstance(end_ts, int) or end_ts < start_ts:
        return
    duration_minutes = max(0, round((end_ts - start_ts) / 60))
    stop["visit_duration_minutes"] = duration_minutes
    stop["visit_duration"] = fmt_duration(duration_minutes)


def annotate_visit_boundaries(stops: list[dict], track_points: list[Event]) -> None:
    points = sorted([point for point in track_points if point.is_location and event_timestamp(point) is not None], key=event_time)
    local_tz = timestamp_tz(points)
    point_timestamps = [(point, event_timestamp(point)) for point in points]

    for stop in stops:
        start_ts = stop.get("start_timestamp")
        end_ts = stop.get("end_timestamp")
        if not isinstance(start_ts, int) or not isinstance(end_ts, int):
            continue
        stop.setdefault("radius_m", DEFAULT_VISIT_RADIUS_M)
        stop["raw_start"] = stop.get("start", "")
        stop["raw_end"] = stop.get("end", "")
        stop["raw_start_timestamp"] = start_ts
        stop["raw_end_timestamp"] = end_ts
        stop["visit_start_timestamp"] = start_ts
        stop["visit_end_timestamp"] = end_ts
        stop["entry_display"] = stop.get("start", "")
        stop["exit_display"] = stop.get("end", "")
        stop["entry_status"] = "sample"
        stop["exit_status"] = "sample"
        stop["visit_source"] = "manual" if stop.get("manual") else "home-boundary" if stop.get("boundary") else "detected-stop"

        first_point = sample_for_line(points, stop.get("start_line"))
        last_point = sample_for_line(points, stop.get("end_line"))
        previous_point = next((point for point, timestamp in reversed(point_timestamps) if timestamp is not None and timestamp < start_ts), None)
        next_point = next((point for point, timestamp in point_timestamps if timestamp is not None and timestamp > end_ts), None)
        place = visit_place_for_stop(stop)
        radius_m = visit_radius_m(stop)

        stop["evidence"] = {
            "start_line": stop.get("start_line"),
            "end_line": stop.get("end_line"),
            "raw_start": stop.get("raw_start"),
            "raw_end": stop.get("raw_end"),
            "previous_line": previous_point.line_no if previous_point else None,
            "previous_time": fmt_dt(event_time(previous_point)) if previous_point else "",
            "next_line": next_point.line_no if next_point else None,
            "next_time": fmt_dt(event_time(next_point)) if next_point else "",
        }

        if previous_point is not None and first_point is not None:
            previous_ts = event_timestamp(previous_point)
            if isinstance(previous_ts, int) and interpolation_allowed(previous_point, first_point):
                interpolated = interpolated_arrival_timestamp(previous_point, first_point, place, radius_m)
                if isinstance(interpolated, int) and interpolated != start_ts:
                    stop["visit_start_timestamp"] = interpolated
                    stop["entry_status"] = "interpolated"
                    stop["entry_display"] = fmt_dt(datetime_from_timestamp_with_tz(interpolated, local_tz))

        if next_point is not None and last_point is not None:
            next_ts = event_timestamp(next_point)
            if isinstance(next_ts, int) and next_ts - end_ts > VISIT_BOUNDARY_INTERPOLATION_MAX_GAP_SECONDS:
                stop["exit_status"] = "window"
                stop["exit_window"] = {
                    "start_timestamp": end_ts,
                    "end_timestamp": next_ts,
                    "start": stop.get("raw_end", ""),
                    "end": fmt_dt(event_time(next_point)),
                }
                stop["exit_display"] = fmt_visit_window(end_ts, next_ts, local_tz)
            elif interpolation_allowed(last_point, next_point):
                interpolated = interpolated_boundary_timestamp(last_point, next_point, place, radius_m)
                if isinstance(interpolated, int) and interpolated != end_ts:
                    stop["visit_end_timestamp"] = interpolated
                    stop["exit_status"] = "interpolated"
                    stop["exit_display"] = fmt_dt(datetime_from_timestamp_with_tz(interpolated, local_tz))

        if stop.get("entry_status") == "window" or stop.get("exit_status") == "window":
            stop["confidence"] = "low"
        elif stop.get("entry_status") == "interpolated" or stop.get("exit_status") == "interpolated":
            stop["confidence"] = "medium"
        elif int(stop.get("points") or 0) >= 2:
            stop["confidence"] = "high"
        else:
            stop["confidence"] = "medium" if stop.get("manual") else "low"
        annotated_visit_duration(stop)


def parse_visit_override_timestamp(value: object, date_text: str, local_tz: ZoneInfo | timezone) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    approximate = text.startswith("~")
    if approximate:
        text = text[1:].strip()
    if re.fullmatch(r"\d{10,}", text):
        return int(text)
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
        text = f"{date_text}T{text}"
    normalized = text.replace(" ", "T", 1)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return int(parsed.astimezone(timezone.utc).timestamp())


def override_timestamp_for(override: dict, prefix: str, date_text: str, local_tz: ZoneInfo | timezone) -> int | None:
    return parse_visit_override_timestamp(
        override.get(f"{prefix}_timestamp", override.get(f"{prefix}_time")),
        date_text,
        local_tz,
    )


def apply_visit_timing_override(stop: dict, override: dict, date_text: str, local_tz: ZoneInfo | timezone) -> None:
    radius = as_float(override.get("radius_m"))
    if radius is not None and 10 <= radius <= 5000:
        stop["radius_m"] = round(radius)
    if "place" in override:
        stop["place"] = bool(override.get("place"))

    entry_ts = override_timestamp_for(override, "entry", date_text, local_tz)
    exit_ts = override_timestamp_for(override, "exit", date_text, local_tz)
    if entry_ts is not None:
        stop["entry_override"] = str(override.get("entry_time") or override.get("entry_timestamp") or "")
        stop["visit_start_timestamp"] = entry_ts
        stop["start_timestamp"] = entry_ts
        stop["start"] = fmt_dt(datetime_from_timestamp_with_tz(entry_ts, local_tz))
        stop["entry_display"] = stop["start"]
        stop["entry_status"] = "corrected"
        stop["entry_corrected"] = True
        stop["user_reviewed"] = True
    if exit_ts is not None:
        stop["exit_override"] = str(override.get("exit_time") or override.get("exit_timestamp") or "")
        stop["visit_end_timestamp"] = exit_ts
        stop["end_timestamp"] = exit_ts
        stop["end"] = fmt_dt(datetime_from_timestamp_with_tz(exit_ts, local_tz))
        stop["exit_display"] = stop["end"]
        stop["exit_status"] = "corrected"
        stop["exit_corrected"] = True
        stop["user_reviewed"] = True
    if entry_ts is not None or exit_ts is not None:
        start_ts = stop.get("visit_start_timestamp")
        end_ts = stop.get("visit_end_timestamp")
        if isinstance(start_ts, int) and isinstance(end_ts, int) and end_ts >= start_ts:
            duration_minutes = max(0, round((end_ts - start_ts) / 60))
            stop["duration_minutes"] = duration_minutes
            stop["duration"] = fmt_duration(duration_minutes)
            stop["visit_duration_minutes"] = duration_minutes
            stop["visit_duration"] = stop["duration"]
        stop["confidence"] = "corrected"


def interpolated_boundary_timestamp(
    inside_point: Event,
    outside_point: Event,
    place: dict,
    radius_m: float,
) -> int | None:
    inside_time = event_time(inside_point)
    outside_time = event_time(outside_point)
    if inside_time.tzinfo is None or outside_time.tzinfo is None:
        return None
    inside_distance = point_distance_m(inside_point, place)
    outside_distance = point_distance_m(outside_point, place)
    if inside_distance > radius_m or outside_distance <= radius_m or outside_distance <= inside_distance:
        return None
    elapsed = (outside_time - inside_time).total_seconds()
    if elapsed <= 0:
        return None
    ratio = max(0.0, min(1.0, (radius_m - inside_distance) / (outside_distance - inside_distance)))
    return round(inside_time.timestamp() + elapsed * ratio)


def interpolated_arrival_timestamp(
    outside_point: Event,
    inside_point: Event,
    place: dict,
    radius_m: float,
) -> int | None:
    outside_time = event_time(outside_point)
    inside_time = event_time(inside_point)
    if outside_time.tzinfo is None or inside_time.tzinfo is None:
        return None
    outside_distance = point_distance_m(outside_point, place)
    inside_distance = point_distance_m(inside_point, place)
    if outside_distance <= radius_m or inside_distance > radius_m or outside_distance <= inside_distance:
        return None
    elapsed = (inside_time - outside_time).total_seconds()
    if elapsed <= 0:
        return None
    ratio = max(0.0, min(1.0, (outside_distance - radius_m) / (outside_distance - inside_distance)))
    return round(outside_time.timestamp() + elapsed * ratio)


def datetime_from_timestamp(timestamp: int, points: list[Event]) -> datetime:
    local_tz = points[0].local_tz if points else timezone.utc
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(local_tz)


def trip_timeline_rows(points: list[Event], places: list[dict]) -> list[dict]:
    rows = []
    for point in points:
        nearest = None
        nearest_distance = None
        for place in places:
            distance_m = point_distance_m(point, place)
            if nearest_distance is None or distance_m < nearest_distance:
                nearest = place
                nearest_distance = distance_m
        rows.append(
            {
                "line": point.line_no,
                "time": fmt_dt(event_time(point)),
                "timestamp": int(event_time(point).timestamp()) if event_time(point).tzinfo is not None else None,
                "motion_mode": motion_mode(point),
                "t": point.payload.get("t"),
                "speed_kmh": point.speed_kmh,
                "nearest_place": nearest.get("name") if nearest else "",
                "nearest_distance_m": round(nearest_distance) if nearest_distance is not None else None,
                "lat": point.lat,
                "lon": point.lon,
            }
        )
    return rows


def trip_query_result(points: list[Event], origin: dict, destination: dict) -> dict:
    dest_radius = float(destination.get("radius_m") or 150)
    origin_radius = float(origin.get("radius_m") or 150)

    def visit_window_points(place: dict, candidates: list[Event]) -> list[Event]:
        start_ts = place.get("visit_start_timestamp")
        end_ts = place.get("visit_end_timestamp")
        if not isinstance(start_ts, int) or not isinstance(end_ts, int):
            return []
        return [
            point
            for point in candidates
            if event_time(point).tzinfo is not None
            and start_ts <= int(event_time(point).timestamp()) <= end_ts
            and point_distance_m(point, place) <= float(place.get("radius_m") or 150)
        ]

    destination_visit_points = visit_window_points(destination, points)
    arrival = destination_visit_points[0] if destination_visit_points else next((point for point in points if point_distance_m(point, destination) <= dest_radius), None)
    if arrival is None:
        return {
            "ok": False,
            "reason": f"No sample within {round(dest_radius)} m of {destination.get('name')}.",
            "origin_key": origin.get("key"),
            "destination_key": destination.get("key"),
        }

    before_arrival = [point for point in points if event_time(point) <= event_time(arrival)]
    origin_visit_points = visit_window_points(origin, before_arrival)
    origin_points = origin_visit_points or [point for point in before_arrival if point_distance_m(point, origin) <= origin_radius]
    last_origin = origin_points[-1] if origin_points else None
    after_origin = [
        point
        for point in before_arrival
        if last_origin is None or event_time(point) > event_time(last_origin)
    ]
    outbound = [
        point
        for point in after_origin
        if point_distance_m(point, origin) > origin_radius
    ]
    preferred = next(
        (
            point
            for point in outbound
            if point.payload.get("t") in {"v", "c"}
            and motion_mode(point) in {"automotive", "moving", "cycling", "walking"}
        ),
        None,
    )
    if preferred is None:
        preferred = next(
            (point for point in outbound if motion_mode(point) in {"automotive", "moving", "cycling", "walking"}),
            outbound[0] if outbound else last_origin,
        )
    if preferred is None:
        return {
            "ok": False,
            "reason": f"No origin sample before arrival at {destination.get('name')}.",
            "origin_key": origin.get("key"),
            "destination_key": destination.get("key"),
        }

    arrival_index = points.index(arrival)
    previous_arrival_point = points[arrival_index - 1] if arrival_index > 0 else None
    visit_arrival_timestamp = destination.get("visit_start_timestamp")
    arrival_timestamp = visit_arrival_timestamp if isinstance(visit_arrival_timestamp, int) else None
    arrival_source = "visit"
    if arrival_timestamp is None:
        arrival_source = "sample"
        arrival_timestamp = (
            interpolated_arrival_timestamp(previous_arrival_point, arrival, destination, dest_radius)
            if previous_arrival_point and interpolation_allowed(previous_arrival_point, arrival)
            else None
        )
        if arrival_timestamp is not None:
            arrival_source = "interpolated"
    if arrival_timestamp is None:
        arrival_timestamp = int(event_time(arrival).timestamp()) if event_time(arrival).tzinfo is not None else None

    last_origin_index = points.index(last_origin) if last_origin in points else None
    next_origin_point = points[last_origin_index + 1] if isinstance(last_origin_index, int) and last_origin_index + 1 < len(points) else None
    visit_departure_timestamp = origin.get("visit_end_timestamp")
    departure_timestamp = visit_departure_timestamp if isinstance(visit_departure_timestamp, int) else None
    departure_source = "visit"
    departure_boundary_sample = next_origin_point or last_origin or preferred
    if departure_timestamp is None:
        departure_source = "sample"
        departure_timestamp = (
            interpolated_boundary_timestamp(last_origin, next_origin_point, origin, origin_radius)
            if last_origin and next_origin_point and interpolation_allowed(last_origin, next_origin_point)
            else None
        )
        if departure_timestamp is not None:
            departure_source = "interpolated"
            departure_boundary_sample = next_origin_point or preferred
    if departure_timestamp is None:
        departure_boundary_sample = outbound[0] if outbound else preferred
        departure_timestamp = int(event_time(departure_boundary_sample).timestamp()) if event_time(departure_boundary_sample).tzinfo is not None else None

    if departure_timestamp is None or arrival_timestamp is None:
        start = event_time(preferred)
        end = event_time(arrival)
        preferred_seconds = int((end - start).total_seconds())
        departure_sample = preferred
    else:
        start = datetime_from_timestamp(departure_timestamp, points)
        end = datetime_from_timestamp(arrival_timestamp, points)
        preferred_seconds = max(0, arrival_timestamp - departure_timestamp)
        departure_sample = departure_boundary_sample
    last_origin_seconds = int((end - event_time(last_origin)).total_seconds()) if last_origin else None
    return {
        "ok": True,
        "origin_key": origin.get("key"),
        "destination_key": destination.get("key"),
        "origin_name": origin.get("name"),
        "destination_name": destination.get("name"),
        "departure": {
            "line": departure_sample.line_no,
            "time": fmt_dt(start),
            "sample_time": fmt_dt(event_time(departure_sample)),
            "timestamp": int(start.timestamp()) if start.tzinfo is not None else None,
            "estimated": departure_source == "interpolated",
            "corrected": origin.get("visit_exit_status") == "corrected",
            "source": departure_source,
            "window": origin.get("visit_exit_window"),
            "motion_mode": motion_mode(departure_sample),
            "t": departure_sample.payload.get("t"),
            "distance_from_origin_m": round(point_distance_m(departure_sample, origin)),
        },
        "arrival": {
            "line": arrival.line_no,
            "time": fmt_dt(end),
            "sample_time": fmt_dt(event_time(arrival)),
            "timestamp": int(end.timestamp()) if end.tzinfo is not None else None,
            "estimated": arrival_source == "interpolated",
            "corrected": destination.get("visit_entry_status") == "corrected",
            "source": arrival_source,
            "window": destination.get("visit_entry_window"),
            "motion_mode": motion_mode(arrival),
            "t": arrival.payload.get("t"),
            "distance_to_destination_m": round(point_distance_m(arrival, destination)),
        },
        "duration_seconds": preferred_seconds,
        "duration": fmt_duration(round(preferred_seconds / 60)),
        "last_origin": (
            {
                "line": last_origin.line_no,
                "time": fmt_dt(event_time(last_origin)),
                "duration_seconds": last_origin_seconds,
                "duration": fmt_duration(round((last_origin_seconds or 0) / 60)),
                "distance_from_origin_m": round(point_distance_m(last_origin, origin)),
            }
            if last_origin
            else None
        ),
        "heuristic": "uses corrected same-day visit boundaries when present; otherwise interpolates only across short adjacent boundary gaps",
    }


def build_trip_summary(
    events: list[Event],
    user_tags: dict | None = None,
    *,
    target_date: date,
    origin_key: str | None = None,
    destination_key: str | None = None,
    home_filter: HomeFilterConfig | None = None,
) -> dict:
    places = trip_place_definitions(events, user_tags, target_date, home_filter)
    points = unique_location_points(events, target_date)
    selected_origin = next((place for place in places if place["key"] == origin_key), None)
    selected_destination = next((place for place in places if place["key"] == destination_key), None)
    if selected_origin is None:
        selected_origin = next((place for place in places if str(place["name"]).casefold() == "home"), places[0] if places else None)
    if selected_destination is None:
        selected_destination = next(
            (place for place in places if "sugganahalli" in str(place["name"]).casefold()),
            places[1] if len(places) > 1 else None,
        )
    query = (
        trip_query_result(points, selected_origin, selected_destination)
        if selected_origin and selected_destination and selected_origin["key"] != selected_destination["key"]
        else {"ok": False, "reason": "Select two different places."}
    )
    return {
        "title": "OwnTracks trips",
        "date": target_date.isoformat(),
        "places": places,
        "selected": {
            "origin_key": selected_origin.get("key") if selected_origin else "",
            "destination_key": selected_destination.get("key") if selected_destination else "",
        },
        "query": query,
        "timeline": trip_timeline_rows(points, places),
    }


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


def waypoint_index_events(events: list[Event], target_date: date, skip_proximity_names: set[str] | None = None) -> list[dict]:
    skip_names = skip_proximity_names or set()
    waypoints = []
    definitions: list[dict] = []
    for event in events:
        if event.kind != "waypoint":
            continue
        desc = str(event.payload.get("desc") or "").strip()
        if not desc:
            continue
        lat = event.lat
        lon = event.lon
        radius_m = as_float(event.payload.get("rad")) or 30
        waypoint_id = str(event.payload.get("rid") or f"{slug(desc)}-{event.line_no}")
        if lat is not None and lon is not None:
            definitions.append(
                {
                    "id": waypoint_id,
                    "name": desc,
                    "lat": lat,
                    "lon": lon,
                    "radius_m": max(30, radius_m),
                }
            )
        if event_date(event) != target_date:
            continue
        timestamp = int(event_time(event).timestamp()) if event_time(event).tzinfo is not None else None
        waypoints.append(
            {
                "date": target_date.isoformat(),
                "id": f"waypoint:{waypoint_id}",
                "name": desc,
                "raw_name": desc,
                "start": fmt_dt(event_time(event)),
                "end": fmt_dt(event_time(event)),
                "start_timestamp": timestamp,
                "end_timestamp": timestamp,
                "duration": "0 min",
                "duration_minutes": 0,
                "lat": lat,
                "lon": lon,
                "points": 0,
                "motion": "waypoint",
                "motion_mode": "waypoint",
                "tags": [f"waypoint:{slug(desc)}"],
                "note": "",
                "maps": maps_url(lat, lon) if lat is not None and lon is not None else "",
                "reviewed": False,
                "manual": False,
                "source": "waypoint",
            }
        )
    deduped_definitions = {
        str(definition["id"]): definition
        for definition in definitions
    }
    day_locations = [
        event
        for event in events
        if event_date(event) == target_date and event.is_location and event.lat is not None and event.lon is not None
    ]
    for definition in deduped_definitions.values():
        name_key = str(definition["name"]).casefold()
        if name_key in skip_names:
            continue
        matches = [
            event
            for event in day_locations
            if haversine_km(definition["lat"], definition["lon"], event.lat or 0, event.lon or 0) * 1000
            <= float(definition["radius_m"])
        ]
        if not matches:
            continue
        clusters: list[list[Event]] = []
        current: list[Event] = []
        for event in sorted(matches, key=event_time):
            if current and (event_time(event) - event_time(current[-1])).total_seconds() > 60 * 60:
                clusters.append(current)
                current = []
            current.append(event)
        if current:
            clusters.append(current)
        for cluster in clusters:
            start = event_time(cluster[0])
            end = event_time(cluster[-1])
            duration_minutes = max(0, round((end - start).total_seconds() / 60))
            lat = sum(event.lat or 0 for event in cluster) / len(cluster)
            lon = sum(event.lon or 0 for event in cluster) / len(cluster)
            modes = Counter(motion_mode(event) for event in cluster)
            dominant_mode = modes.most_common(1)[0][0] if modes else "waypoint"
            waypoints.append(
                {
                    "date": target_date.isoformat(),
                    "id": f"waypoint-proximity:{definition['id']}-{cluster[0].line_no}-{cluster[-1].line_no}",
                    "name": definition["name"],
                    "raw_name": definition["name"],
                    "start": fmt_dt(start),
                    "end": fmt_dt(end),
                    "start_timestamp": int(start.timestamp()) if start.tzinfo is not None else None,
                    "end_timestamp": int(end.timestamp()) if end.tzinfo is not None else None,
                    "duration": fmt_duration(duration_minutes),
                    "duration_minutes": duration_minutes,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "points": len(cluster),
                    "motion": ", ".join(f"{name}:{count}" for name, count in modes.most_common()) or "waypoint",
                    "motion_mode": dominant_mode,
                    "tags": [f"waypoint:{slug(definition['name'])}", "source:waypoint-proximity"],
                    "note": "",
                    "maps": maps_url(lat, lon),
                    "reviewed": False,
                    "manual": False,
                    "source": "waypoint-proximity",
                }
            )
    return waypoints


def saved_place_index_events(
    events: list[Event],
    target_date: date,
    user_tags: dict | None = None,
    skip_proximity_names: set[str] | None = None,
) -> list[dict]:
    skip_names = skip_proximity_names or set()
    definitions: dict[str, dict] = {}
    for day_tags in (user_tags or {}).values():
        if not isinstance(day_tags, dict):
            continue
        for saved in (day_tags.get("stops") or {}).values():
            if not isinstance(saved, dict) or not saved.get("place"):
                continue
            name = str(saved.get("name") or "").strip()
            lat = as_float(saved.get("lat"))
            lon = as_float(saved.get("lon"))
            if not name or lat is None or lon is None:
                continue
            key = f"name:{name.casefold()}"
            if key not in definitions:
                definitions[key] = {
                    "id": slug(name),
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "radius_m": max(150.0, as_float(saved.get("radius_m")) or 0.0),
                    "tags": [str(tag) for tag in saved.get("tags", []) if str(tag).strip()],
                }

    day_locations = [
        event
        for event in events
        if event_date(event) == target_date and event.is_location and event.lat is not None and event.lon is not None
    ]
    visits: list[dict] = []
    for definition in definitions.values():
        name_key = str(definition["name"]).casefold()
        if name_key in skip_names:
            continue
        matches = [
            event
            for event in day_locations
            if haversine_km(definition["lat"], definition["lon"], event.lat or 0, event.lon or 0) * 1000
            <= float(definition["radius_m"])
        ]
        if not matches:
            continue
        clusters: list[list[Event]] = []
        current: list[Event] = []
        for event in sorted(matches, key=event_time):
            if current and (event_time(event) - event_time(current[-1])).total_seconds() > 60 * 60:
                clusters.append(current)
                current = []
            current.append(event)
        if current:
            clusters.append(current)
        for cluster in clusters:
            start = event_time(cluster[0])
            end = event_time(cluster[-1])
            duration_minutes = max(0, round((end - start).total_seconds() / 60))
            lat = sum(event.lat or 0 for event in cluster) / len(cluster)
            lon = sum(event.lon or 0 for event in cluster) / len(cluster)
            modes = Counter(motion_mode(event) for event in cluster)
            dominant_mode = modes.most_common(1)[0][0] if modes else "review"
            tags = [
                f"place:{slug(definition['name'])}",
                "source:review-proximity",
                *definition.get("tags", []),
            ]
            visits.append(
                {
                    "date": target_date.isoformat(),
                    "id": f"review-proximity:{definition['id']}-{cluster[0].line_no}-{cluster[-1].line_no}",
                    "name": definition["name"],
                    "raw_name": definition["name"],
                    "start": fmt_dt(start),
                    "end": fmt_dt(end),
                    "start_timestamp": int(start.timestamp()) if start.tzinfo is not None else None,
                    "end_timestamp": int(end.timestamp()) if end.tzinfo is not None else None,
                    "duration": fmt_duration(duration_minutes),
                    "duration_minutes": duration_minutes,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "points": len(cluster),
                    "motion": ", ".join(f"{name}:{count}" for name, count in modes.most_common()) or "review",
                    "motion_mode": dominant_mode,
                    "tags": sorted(set(tags)),
                    "note": "",
                    "maps": maps_url(lat, lon),
                    "reviewed": False,
                    "manual": False,
                    "source": "review-proximity",
                }
            )
    return visits


def transition_index_events(events: list[Event], target_date: date) -> list[dict]:
    transitions = [
        event
        for event in events
        if event.kind == "transition" and event_date(event) == target_date and str(event.payload.get("desc") or "").strip()
    ]
    transitions.sort(key=event_time)
    visits: list[dict] = []
    open_entries: dict[str, Event] = {}

    def visit_from_events(desc: str, start_event: Event, end_event: Event | None = None) -> dict:
        start_dt = event_time(start_event)
        end_dt = event_time(end_event) if end_event is not None else start_dt
        duration_minutes = max(0, round((end_dt - start_dt).total_seconds() / 60))
        lat = end_event.lat if end_event is not None and end_event.lat is not None else start_event.lat
        lon = end_event.lon if end_event is not None and end_event.lon is not None else start_event.lon
        action = str(start_event.payload.get("event") or "transition")
        if end_event is not None:
            action = f"{start_event.payload.get('event') or 'enter'}->{end_event.payload.get('event') or 'leave'}"
        return {
            "date": target_date.isoformat(),
            "id": f"transition:{slug(desc)}-{start_event.line_no}" + (f"-{end_event.line_no}" if end_event else ""),
            "name": desc,
            "raw_name": desc,
            "start": fmt_dt(start_dt),
            "end": fmt_dt(end_dt),
            "start_timestamp": int(start_dt.timestamp()) if start_dt.tzinfo is not None else None,
            "end_timestamp": int(end_dt.timestamp()) if end_dt.tzinfo is not None else None,
            "duration": fmt_duration(duration_minutes),
            "duration_minutes": duration_minutes,
            "lat": lat,
            "lon": lon,
            "points": 0,
            "motion": action,
            "motion_mode": "transition",
            "tags": [f"place:{slug(desc)}", "geofence:transition"],
            "note": "",
            "maps": maps_url(lat, lon) if lat is not None and lon is not None else "",
            "reviewed": False,
            "manual": False,
            "source": "transition",
        }

    for event in transitions:
        desc = str(event.payload.get("desc") or "").strip()
        key = desc.casefold()
        action = str(event.payload.get("event") or "").casefold()
        if action == "enter":
            if key in open_entries:
                visits.append(visit_from_events(desc, open_entries.pop(key)))
            open_entries[key] = event
        elif action == "leave" and key in open_entries:
            visits.append(visit_from_events(desc, open_entries.pop(key), event))
        else:
            visits.append(visit_from_events(desc, event))

    for key, event in open_entries.items():
        desc = str(event.payload.get("desc") or key).strip()
        visits.append(visit_from_events(desc, event))
    return visits


def proximity_named_place_events(
    events: list[Event],
    target_date: date,
    user_tags: dict | None = None,
    skip_names: set[str] | None = None,
) -> list[dict]:
    places: list[dict] = []
    covered = set(skip_names or set())
    proximity_visits = waypoint_index_events(events, target_date, covered)
    for visit in proximity_visits:
        covered.add(str(visit.get("name") or "").casefold())
    proximity_visits.extend(saved_place_index_events(events, target_date, user_tags or {}, covered))
    for visit in proximity_visits:
        places.append(
            {
                "id": visit.get("id", ""),
                "name": visit.get("name", ""),
                "action": "visit",
                "time": visit.get("start", ""),
                "timestamp": visit.get("start_timestamp"),
                "lat": visit.get("lat"),
                "lon": visit.get("lon"),
                "line": "",
                "tags": visit.get("tags", []),
                "maps": visit.get("maps", ""),
                "source": visit.get("source", "proximity"),
                "points": visit.get("points", 0),
                "duration": visit.get("duration", ""),
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


def stop_from_cluster(cluster: list[Event], index: int) -> dict | None:
    if not cluster:
        return None
    start = event_time(cluster[0])
    end = event_time(cluster[-1])
    duration_minutes = max(0, round((end - start).total_seconds() / 60))
    lat = sum(item.lat or 0 for item in cluster) / len(cluster)
    lon = sum(item.lon or 0 for item in cluster) / len(cluster)
    motions = Counter(motion for item in cluster for motion in item.motion)
    regions = Counter(region for item in cluster for region in (item.payload.get("inregions") or []))
    motion_modes = Counter(motion_mode(item) for item in cluster)
    dominant_motion = motion_modes.most_common(1)[0][0] if motion_modes else "unknown"
    name = regions.most_common(1)[0][0] if regions else f"unnamed-stop-{index}"
    stop_id = f"{slug(name)}-{cluster[0].line_no}"
    return {
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


def stop_covers_event(stop: dict, event: Event) -> bool:
    timestamp = int(event_time(event).timestamp()) if event_time(event).tzinfo is not None else None
    start_timestamp = stop.get("start_timestamp")
    end_timestamp = stop.get("end_timestamp")
    if isinstance(timestamp, int) and isinstance(start_timestamp, int) and isinstance(end_timestamp, int):
        return start_timestamp <= timestamp <= end_timestamp
    return int(stop.get("start_line") or -1) <= event.line_no <= int(stop.get("end_line") or -1)


def boundary_home_stop(event: Event, index: int, boundary: str) -> dict | None:
    stop = stop_from_cluster([event], index)
    if stop is None:
        return None
    stop["id"] = f"boundary-home-{boundary}-{event.line_no}"
    stop["name"] = "Home"
    stop["tags"] = [f"stop:{stop['id']}", "candidate:stop", "source:home-boundary"]
    stop["boundary"] = boundary
    stop["estimated_boundary"] = True
    return stop


def boundary_home_stops(
    events: list[Event],
    existing_stops: list[dict],
    config: HomeFilterConfig | None,
    anchors: list[tuple[float, float, str]],
) -> list[dict]:
    points = [event for event in events if event.is_location]
    if not points or not config or not config.enabled:
        return []
    points.sort(key=event_time)
    inferred: list[dict] = []
    next_index = len(existing_stops) + 1

    first = points[0]
    second = points[1] if len(points) > 1 else None
    if (
        second is not None
        and is_near_home_boundary(first, config, anchors)
        and not is_near_home_boundary(second, config, anchors)
        and not any(stop_covers_event(stop, first) for stop in existing_stops)
    ):
        stop = boundary_home_stop(first, next_index, "start")
        if stop is not None:
            inferred.append(stop)
            next_index += 1

    last = points[-1]
    previous = points[-2] if len(points) > 1 else None
    if (
        previous is not None
        and is_near_home_boundary(last, config, anchors)
        and not is_near_home_boundary(previous, config, anchors)
        and not any(stop_covers_event(stop, last) for stop in [*existing_stops, *inferred])
    ):
        stop = boundary_home_stop(last, next_index, "end")
        if stop is not None:
            inferred.append(stop)
    return inferred


def same_place_dwell_clusters(events: list[Event], min_minutes: int, radius_m: int) -> list[list[Event]]:
    tight_radius_m = min(80, radius_m)
    points = [
        event
        for event in events
        if event.is_location and "Home" not in (event.payload.get("inregions") or [])
    ]
    clusters: list[list[Event]] = []
    current: list[Event] = []
    for event in points:
        if not current:
            current = [event]
            continue
        center_lat = sum(item.lat or 0 for item in current) / len(current)
        center_lon = sum(item.lon or 0 for item in current) / len(current)
        dt_gap = (event_time(event) - event_time(current[-1])).total_seconds()
        dist_m = haversine_km(center_lat, center_lon, event.lat or 0, event.lon or 0) * 1000
        if dist_m <= tight_radius_m and dt_gap <= 45 * 60:
            current.append(event)
        else:
            clusters.append(current)
            current = [event]
    if current:
        clusters.append(current)
    return [
        cluster
        for cluster in clusters
        if len(cluster) >= 2
        and (event_time(cluster[-1]) - event_time(cluster[0])).total_seconds() >= min_minutes * 60
    ]


def candidate_stops(events: list[Event], min_minutes: int = 10, radius_m: int = 180) -> list[dict]:
    sparse_same_place_radius_m = min(50, radius_m)
    location_points = [event for event in events if event.is_location]
    location_indexes = {id(event): index for index, event in enumerate(location_points)}
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
        previous_index = location_indexes[id(current[-1])]
        event_index = location_indexes[id(event)]
        stayed_nearby = all(
            haversine_km(center_lat, center_lon, item.lat or 0, item.lon or 0) * 1000 <= radius_m
            for item in location_points[previous_index + 1:event_index]
        )
        bridge_sparse_gap = dist_m <= sparse_same_place_radius_m and stayed_nearby
        if dist_m <= radius_m and (dt_gap <= 45 * 60 or bridge_sparse_gap):
            current.append(event)
        else:
            clusters.append(current)
            current = [event]
    if current:
        clusters.append(current)

    stops = []
    for index, cluster in enumerate(clusters, start=1):
        duration_minutes = max(0, round((event_time(cluster[-1]) - event_time(cluster[0])).total_seconds() / 60))
        if duration_minutes < min_minutes and len(cluster) < 3:
            continue
        stop = stop_from_cluster(cluster, index)
        if stop is not None:
            stops.append(stop)

    covered_lines = {
        line
        for stop in stops
        for line in range(int(stop["start_line"]), int(stop["end_line"]) + 1)
    }
    next_index = len(stops) + 1
    for cluster in same_place_dwell_clusters(events, min_minutes, radius_m):
        cluster_lines = {event.line_no for event in cluster}
        if cluster_lines & covered_lines:
            continue
        stop = stop_from_cluster(cluster, next_index)
        if stop is None:
            continue
        stops.append(stop)
        covered_lines.update(range(int(stop["start_line"]), int(stop["end_line"]) + 1))
        next_index += 1
    return sorted(stops, key=lambda stop: (int(stop["start_line"]), int(stop["end_line"])))


def possible_missed_stops(
    events: list[Event],
    existing_stops: list[dict],
    *,
    min_gap_minutes: int = 20,
    max_accuracy_m: int = 250,
) -> list[dict]:
    points = [event for event in events if event.is_location]
    points.sort(key=event_time)
    if len(points) < 3:
        return []

    suggestions: list[dict] = []
    seen_locations: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        if any(stop_covers_event(stop, point) for stop in existing_stops):
            continue
        accuracy = as_float(point.payload.get("acc"))
        if accuracy is not None and accuracy > max_accuracy_m:
            continue
        mode = motion_mode(point)
        if mode in {"automotive", "cycling", "moving"}:
            continue
        if point.speed_kmh is not None and point.speed_kmh > 5:
            continue

        previous = points[index - 1] if index > 0 else None
        next_point = points[index + 1] if index + 1 < len(points) else None
        previous_gap = (event_time(point) - event_time(previous)).total_seconds() if previous else None
        next_gap = (event_time(next_point) - event_time(point)).total_seconds() if next_point else None
        if not (
            (previous_gap is not None and previous_gap >= min_gap_minutes * 60)
            or (next_gap is not None and next_gap >= min_gap_minutes * 60)
        ):
            continue

        previous_distance = (
            haversine_km(previous.lat or 0, previous.lon or 0, point.lat or 0, point.lon or 0) * 1000
            if previous
            else None
        )
        next_distance = (
            haversine_km(point.lat or 0, point.lon or 0, next_point.lat or 0, next_point.lon or 0) * 1000
            if next_point
            else None
        )
        if previous_distance is not None and previous_distance <= DEFAULT_VISIT_RADIUS_M and previous_gap is not None and previous_gap >= min_gap_minutes * 60:
            continue
        if next_distance is not None and next_distance <= DEFAULT_VISIT_RADIUS_M and next_gap is not None and next_gap >= min_gap_minutes * 60:
            continue
        if any(haversine_km(lat, lon, point.lat or 0, point.lon or 0) * 1000 <= DEFAULT_VISIT_RADIUS_M for lat, lon in seen_locations):
            continue

        recorded = event_time(point)
        received = point.received_at
        upload_delay_seconds = (
            int((received - recorded).total_seconds())
            if received is not None and received.tzinfo is not None and recorded.tzinfo is not None
            else None
        )
        reasons = []
        if previous_gap is not None and previous_gap >= min_gap_minutes * 60:
            reasons.append(f"{fmt_duration(round(previous_gap / 60))} since previous sample")
        if next_gap is not None and next_gap >= min_gap_minutes * 60:
            reasons.append(f"{fmt_duration(round(next_gap / 60))} until next sample")
        if upload_delay_seconds is not None and upload_delay_seconds >= 30 * 60:
            reasons.append(f"buffered upload delayed {fmt_duration(round(upload_delay_seconds / 60))}")
        if mode in {"stationary", "unknown"} or point.speed_kmh in (None, 0):
            reasons.append("not clearly moving")

        suggestions.append(
            {
                "id": f"possible-stop-{point.line_no}",
                "line": point.line_no,
                "lat": round(point.lat or 0, 6),
                "lon": round(point.lon or 0, 6),
                "time": fmt_dt(recorded),
                "timestamp": int(recorded.timestamp()) if recorded.tzinfo is not None else None,
                "received_time": fmt_dt(received),
                "received_timestamp": int(received.timestamp()) if received and received.tzinfo is not None else None,
                "upload_delay_seconds": upload_delay_seconds,
                "motion_mode": mode,
                "speed_kmh": point.speed_kmh,
                "accuracy_m": accuracy,
                "previous_gap_minutes": round(previous_gap / 60) if previous_gap is not None else None,
                "next_gap_minutes": round(next_gap / 60) if next_gap is not None else None,
                "previous_distance_m": round(previous_distance) if previous_distance is not None else None,
                "next_distance_m": round(next_distance) if next_distance is not None else None,
                "confidence": "low",
                "reason": "; ".join(reasons),
                "maps": maps_url(point.lat or 0, point.lon or 0),
            }
        )
        seen_locations.append((point.lat or 0, point.lon or 0))
    return suggestions


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


def manual_stops_for_date(user_tags: dict, date_text: str) -> list[dict]:
    """Restore route points that a user explicitly promoted to stops."""
    day_tags = user_tags.get(date_text, {})
    saved_stops = day_tags.get("stops", {}) if isinstance(day_tags, dict) else {}
    manual_stops = []
    for stop_id, saved in saved_stops.items():
        if not isinstance(saved, dict) or not saved.get("manual") or saved.get("ignored"):
            continue
        lat = as_float(saved.get("lat"))
        lon = as_float(saved.get("lon"))
        if lat is None or lon is None:
            continue
        line = saved.get("line")
        timestamp = saved.get("timestamp")
        time_text = str(saved.get("time") or "")
        manual_stops.append(
            {
                "id": str(stop_id),
                "name": str(saved.get("name") or "manual stop"),
                "start": time_text,
                "end": time_text,
                "start_line": line,
                "end_line": line,
                "start_timestamp": timestamp,
                "end_timestamp": timestamp,
                "duration_minutes": 0,
                "duration": "0 min",
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "points": 1,
                "motion": str(saved.get("motion_mode") or "unknown"),
                "motion_mode": str(saved.get("motion_mode") or "unknown"),
                "motion_modes": str(saved.get("motion_mode") or "unknown") + ":1",
                "tags": [f"stop:{stop_id}", "candidate:stop", "source:manual", *[str(tag) for tag in saved.get("tags", []) if str(tag).strip()]],
                "user_tags": [str(tag) for tag in saved.get("tags", []) if str(tag).strip()],
                "user_note": str(saved.get("note") or ""),
                "media": [item for item in saved.get("media", []) if isinstance(item, dict)],
                "user_reviewed": True,
                "maps": maps_url(lat, lon),
                "manual": True,
                "place": bool(saved.get("place")),
            }
        )
    return manual_stops


def saved_stop_matches_stop(saved_stop: dict, stop: dict) -> bool:
    saved_timestamp = saved_stop.get("timestamp")
    start_timestamp = stop.get("start_timestamp")
    end_timestamp = stop.get("end_timestamp")
    if isinstance(saved_timestamp, int) and isinstance(start_timestamp, int) and isinstance(end_timestamp, int):
        return start_timestamp <= saved_timestamp <= end_timestamp
    saved_line = saved_stop.get("line")
    start_line = stop.get("start_line")
    end_line = stop.get("end_line")
    if isinstance(saved_line, int) and isinstance(start_line, int) and isinstance(end_line, int):
        if start_line <= saved_line <= end_line:
            return True
    return False


def location_override_for(stop: dict, user_tags: dict, current_date: str, radius_m: int = 150) -> dict:
    stop_lat = as_float(stop.get("lat"))
    stop_lon = as_float(stop.get("lon"))
    if stop_lat is None or stop_lon is None:
        return {}
    stop_name = str(stop.get("name") or "").strip()
    exact_override: dict | None = None
    exact_key: tuple[bool, str, int] | None = None
    for date_key, day_tags in user_tags.items():
        if not isinstance(day_tags, dict):
            continue
        if date_key > current_date:
            continue
        for saved_stop in day_tags.get("stops", {}).values():
            if not isinstance(saved_stop, dict) or not saved_stop.get("name"):
                continue
            if saved_stop.get("manual"):
                continue
            if not saved_stop_matches_stop(saved_stop, stop):
                continue
            saved_timestamp = saved_stop.get("timestamp") if isinstance(saved_stop.get("timestamp"), int) else 0
            candidate_key = (date_key == current_date, date_key, saved_timestamp)
            if exact_key is None or candidate_key > exact_key:
                exact_key = candidate_key
                exact_override = {**saved_stop, "_match": "exact"}
    if exact_override is not None:
        return exact_override
    if stop_name and not re.fullmatch(r"unnamed-stop-\d+", stop_name):
        return {}
    best_key: tuple[bool, str, float] | None = None
    best_override: dict | None = None
    for date_key, day_tags in user_tags.items():
        if not isinstance(day_tags, dict):
            continue
        if date_key >= current_date:
            continue
        for saved_stop in day_tags.get("stops", {}).values():
            if not isinstance(saved_stop, dict) or not saved_stop.get("name"):
                continue
            if saved_stop.get("manual") and not saved_stop.get("place"):
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
                best_override = {**saved_stop, "_date": date_key, "_distance_m": round(distance_m), "_match": "proximity"}
    if best_override is None:
        return {}
    override = best_override.copy()
    override.pop("note", None)
    override.pop("entry_time", None)
    override.pop("entry_timestamp", None)
    override.pop("exit_time", None)
    override.pop("exit_timestamp", None)
    override.pop("radius_m", None)
    override.pop("media", None)
    override.pop("_date", None)
    return override


def waypoint_name_for(
    lat: float | None,
    lon: float | None,
    events: list[Event],
    radius_m: int = 150,
    *,
    use_default_radius: bool = True,
) -> str | None:
    if lat is None or lon is None:
        return None
    best_key: tuple[int, float] | None = None
    best_name: str | None = None
    for event in events:
        if event.kind != "waypoint" or event.lat is None or event.lon is None:
            continue
        name = str(event.payload.get("desc") or "").strip()
        if not name:
            continue
        waypoint_radius = as_float(event.payload.get("rad")) or 0
        match_radius = max(radius_m, waypoint_radius) if use_default_radius else waypoint_radius
        if match_radius <= 0:
            continue
        distance_m = haversine_km(lat, lon, event.lat, event.lon) * 1000
        if distance_m > match_radius:
            continue
        timestamp = int(event_time(event).timestamp()) if event_time(event).tzinfo is not None else 0
        candidate_key = (timestamp, -distance_m)
        if best_key is None or candidate_key > best_key:
            best_key = candidate_key
            best_name = name
    return best_name


def waypoint_override_for(stop: dict, events: list[Event], radius_m: int = 150) -> dict:
    stop_lat = as_float(stop.get("lat"))
    stop_lon = as_float(stop.get("lon"))
    best_name = waypoint_name_for(stop_lat, stop_lon, events, radius_m)
    return {"name": best_name} if best_name else {}


def apply_user_tags(plan: dict, user_tags: dict, events: list[Event] | None = None) -> dict:
    day_tags = user_tags.get(plan["date"], {})
    global_tags = day_tags.get("activity", day_tags.get("ride", {})).get("tags", [])
    plan["recommended_tags"] = sorted(set(plan["recommended_tags"] + global_tags))
    stop_overrides = day_tags.get("stops", {})
    local_tz = events[0].local_tz if events else ZoneInfo("Asia/Kolkata")
    for stop in plan["candidate_stops"]:
        waypoint_override = waypoint_override_for(stop, events or [])
        location_override = location_override_for(stop, user_tags, plan["date"])
        if waypoint_override.get("name") and location_override.get("_match") == "proximity":
            location_override = {}
        explicit_override = merge_stop_overrides([location_override, stop_override_for(stop, stop_overrides)])
        override = merge_stop_overrides(
            [
                waypoint_override,
                location_override,
                stop_override_for(stop, stop_overrides),
            ]
        )
        if override.get("name"):
            stop["reviewed_name"] = override["name"]
            if explicit_override.get("name"):
                stop["user_reviewed"] = True
        if override.get("tags"):
            stop["user_tags"] = override["tags"]
            stop["user_reviewed"] = True
            plan["recommended_tags"] = sorted(set(plan["recommended_tags"] + override["tags"]))
        if override.get("note"):
            stop["user_note"] = override["note"]
            stop["user_reviewed"] = True
        media = override.get("media")
        if isinstance(media, list):
            stop["media"] = [item for item in media if isinstance(item, dict)]
            if stop["media"]:
                stop["user_reviewed"] = True
        apply_visit_timing_override(stop, override, plan["date"], local_tz)
    return plan


def stop_is_ignored(stop: dict, stop_overrides: dict) -> bool:
    return bool(stop_override_for(stop, stop_overrides).get("ignored"))


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
    current_range = stop_line_range(stop)
    if current_range is not None:
        for saved_id, override in stop_overrides.items():
            if saved_id == stop_id or saved_id in fallback_stop_ids(stop_id):
                continue
            saved_range = stop_id_line_range(str(saved_id))
            if saved_range is None:
                continue
            if ranges_overlap(current_range, saved_range):
                matches.append(override)
    if stop_id in stop_overrides:
        matches.append(stop_overrides[stop_id])
    return merge_stop_overrides(matches)


def stop_line_range(stop: dict) -> tuple[int, int] | None:
    start_line = stop.get("start_line")
    end_line = stop.get("end_line")
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return None
    return (min(start_line, end_line), max(start_line, end_line))


def stop_id_line_range(stop_id: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"unnamed-stop-\d+-(\d+)(?:-(\d+))?", stop_id)
    if not match:
        return None
    start_line = int(match.group(1))
    end_line = int(match.group(2) or match.group(1))
    return (min(start_line, end_line), max(start_line, end_line))


def ranges_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def merge_stop_overrides(overrides: list[dict]) -> dict:
    merged: dict = {}
    merged_tags: list[str] = []
    for override in overrides:
        if not isinstance(override, dict):
            continue
        if override.get("name"):
            merged["name"] = override["name"]
        if override.get("ignored"):
            merged["ignored"] = True
        if override.get("note"):
            merged["note"] = override["note"]
        if "place" in override:
            merged["place"] = bool(override.get("place"))
        for key in ("entry_time", "entry_timestamp", "exit_time", "exit_timestamp", "radius_m"):
            if override.get(key) not in (None, ""):
                merged[key] = override[key]
        if isinstance(override.get("media"), list):
            merged["media"] = [item for item in override["media"] if isinstance(item, dict)]
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


OWNTRACKS_NAV_CSS = """
    .ot-nav {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 10px 0 0;
    }
    .ot-nav a {
      align-items: center;
      background: #e5e7eb;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      color: #111827;
      display: inline-flex;
      font-size: 12px;
      font-weight: 800;
      justify-content: center;
      min-height: 32px;
      padding: 6px 9px;
      text-decoration: none;
    }
    .ot-nav a.active {
      background: #111827;
      border-color: #111827;
      color: white;
    }
    .ot-nav a:hover {
      border-color: #6b7280;
    }
    @media (max-width: 700px) {
      .ot-nav {
        flex-wrap: nowrap;
        margin-top: 8px;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: thin;
      }
      .ot-nav a {
        flex: 0 0 auto;
        min-height: 30px;
        padding: 5px 8px;
        white-space: nowrap;
      }
    }
"""


OWNTRACKS_NAV_SCRIPT = """
    const syncOwnTracksNav = () => {
      const token = new URLSearchParams(window.location.search).get("token");
      if (!token) return;
      document.querySelectorAll(".ot-nav a").forEach((link) => {
        const url = new URL(link.getAttribute("href"), window.location.origin);
        url.searchParams.set("token", token);
        link.setAttribute("href", `${url.pathname}${url.search}${url.hash}`);
      });
    };
    syncOwnTracksNav();
"""


def owntracks_heatmap_scope(start: str | None = None, end: str | None = None, date_text: str | None = None) -> str:
    start_text = (start or "").strip()
    end_text = (end or "").strip()
    date_value = (date_text or start_text or end_text).strip()
    if start_text and end_text and start_text[:7] == end_text[:7]:
        return start_text[:7]
    if start_text and end_text and start_text[:4] == end_text[:4]:
        return start_text[:4]
    if len(date_value) >= 7:
        return date_value[:7]
    if len(date_value) >= 4:
        return date_value[:4]
    return ""


def owntracks_nav_html(
    active: str,
    *,
    date_text: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> str:
    date_value = (date_text or end or start or "").strip()
    start_value = (start or date_value or "").strip()
    end_value = (end or date_value or "").strip()
    heat_scope = owntracks_heatmap_scope(start_value, end_value, date_value)
    day_href = f"/owntracks/map/{quote(date_value)}" if date_value else "/owntracks/map/today"
    heat_href = f"/owntracks/map/{quote(heat_scope)}" if heat_scope else "/owntracks/map"
    range_query = urlencode({key: value for key, value in (("start", start_value), ("end", end_value)) if value})
    range_suffix = f"?{range_query}" if range_query else ""
    links = [
        ("day", "Day map", day_href),
        ("trips", "Trips", f"/owntracks/trips?{urlencode({'date': date_value})}" if date_value else "/owntracks/trips"),
        ("heat", "Heat map", heat_href),
        ("stops", "Stops", f"/owntracks/stops{range_suffix}"),
        ("dashboard", "Dashboard", f"/owntracks/dashboard{range_suffix}"),
    ]
    items = []
    for key, label, href in links:
        active_class = " active" if key == active else ""
        items.append(f'<a class="{active_class.strip()}" href="{escape(href)}">{escape(label)}</a>')
    return '<nav class="ot-nav" aria-label="OwnTracks views">' + "".join(items) + "</nav>"


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
            "start_line": stop.get("start_line"),
            "end_line": stop.get("end_line"),
            "lat": stop.get("lat"),
            "lon": stop.get("lon"),
            "start": stop.get("start", ""),
            "end": stop.get("end", ""),
            "start_timestamp": stop.get("start_timestamp"),
            "end_timestamp": stop.get("end_timestamp"),
            "duration": stop.get("duration", ""),
            "points": stop.get("points", ""),
            "maps": stop.get("maps", ""),
            "tags": stop.get("user_tags", []),
            "note": stop.get("user_note", ""),
            "previous_travel": stop.get("previous_travel"),
            "next_travel": stop.get("next_travel"),
        }
        for stop in plan.get("candidate_stops", [])
        if stop.get("lat") is not None and stop.get("lon") is not None
    ]
    named_places = [
        {
            "name": place.get("name", ""),
            "action": place.get("action", ""),
            "id": place.get("id", ""),
            "lat": place.get("lat"),
            "lon": place.get("lon"),
            "time": place.get("time", ""),
            "timestamp": place.get("timestamp"),
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
            "possibleMissedStops": plan.get("possible_missed_stops", []),
            "namedPlaces": named_places,
            "travelSegments": plan.get("travel_segments", []),
            "motionSummary": plan.get("motion_summary") or {},
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    escaped_title = escape(title)
    nav = owntracks_nav_html("day", date_text=str(plan.get("date") or ""))
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
{OWNTRACKS_NAV_CSS}
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
    .inline-check {{
      align-items: center;
      border: 1px solid #e5e7eb;
      border-radius: 6px;
      color: #374151;
      display: inline-flex;
      font-size: 12px;
      font-weight: 750;
      gap: 6px;
      margin: 0;
      min-height: 34px;
      padding: 6px 8px;
    }}
    .inline-check input {{
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
    .travel-time-marker {{
      background: transparent;
      border: 0;
    }}
    .travel-time-label {{
      background: rgb(17 24 39 / 0.88);
      border: 1px solid rgb(255 255 255 / 0.85);
      border-radius: 6px;
      box-shadow: 0 1px 6px rgb(15 23 42 / 0.25);
      color: white;
      font-size: 12px;
      font-weight: 850;
      line-height: 1;
      padding: 5px 7px;
      white-space: nowrap;
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
        {nav}
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
{OWNTRACKS_NAV_SCRIPT}
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
    const originalEntryTimes = new Map(data.stops.map((stop) => [stop.alias, stop.entry_override || ""]));
    const originalExitTimes = new Map(data.stops.map((stop) => [stop.alias, stop.exit_override || ""]));
    const originalRadii = new Map(data.stops.map((stop) => [stop.alias, String(stop.radius_m || {DEFAULT_VISIT_RADIUS_M})]));
    const originalPlaces = new Map(data.stops.map((stop) => [stop.alias, Boolean(stop.place)]));
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


def stop_index_date_range(events: list[Event], start: date | None = None, end: date | None = None) -> tuple[date | None, date | None]:
    event_dates = sorted({event_date(event) for event in events if event_date(event) is not None and event.is_location})
    if not event_dates:
        return start, end
    range_start = start or event_dates[0]
    range_end = end or event_dates[-1]
    if range_start > range_end:
        raise ValueError("start date must be before or equal to end date")
    return range_start, range_end


def stop_index_identity(stop: dict) -> tuple[str, str]:
    label = str(stop.get("reviewed_name") or "").strip()
    if label:
        return f"name:{label.casefold()}", label
    name = str(stop.get("name") or "").strip()
    if name and not re.fullmatch(r"unnamed-stop-\d+", name):
        return f"name:{name.casefold()}", name
    lat = as_float(stop.get("lat"))
    lon = as_float(stop.get("lon"))
    if lat is not None and lon is not None:
        rounded_lat = round(lat, 3)
        rounded_lon = round(lon, 3)
        return f"coord:{rounded_lat:.3f},{rounded_lon:.3f}", f"Near {rounded_lat:.3f}, {rounded_lon:.3f}"
    stop_id = str(stop.get("id") or "unknown stop")
    return f"id:{stop_id}", stop_id


def build_stop_index_summary(
    events: list[Event],
    user_tags: dict | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    home_filter: HomeFilterConfig | None = None,
    stop_jitter_filter: StopJitterFilterConfig | None = None,
) -> dict:
    start_date, end_date = stop_index_date_range(events, start, end)
    if start_date is None or end_date is None:
        return {
            "title": "OwnTracks stop index",
            "scope": {"start": "", "end": "", "days": 0},
            "stats": {"places": 0, "visits": 0, "reviewed_visits": 0, "total_minutes": 0, "travel_pairs": 0},
            "places": [],
            "travel_segments": [],
            "travel_pairs": [],
        }

    grouped: dict[str, dict] = {}
    all_travel_segments: list[dict] = []

    def add_visit(key: str, label: str, visit: dict) -> None:
        tags = [str(tag) for tag in (visit.get("tags") or []) if str(tag).strip()]
        duration_minutes = int(visit.get("duration_minutes") or 0)
        place = grouped.setdefault(
            key,
            {
                "key": key,
                "name": label,
                "lat": visit.get("lat"),
                "lon": visit.get("lon"),
                "visit_count": 0,
                "total_minutes": 0,
                "latest_visit": "",
                "first_visit": "",
                "tags": [],
                "notes": 0,
                "reviewed_visits": 0,
                "visits": [],
            },
        )
        place["visit_count"] += 1
        place["total_minutes"] += duration_minutes
        if tags:
            place["tags"] = sorted(set(place["tags"]) | set(tags))
        if visit.get("note"):
            place["notes"] += 1
        if visit.get("reviewed"):
            place["reviewed_visits"] += 1
        place["visits"].append(visit)
        dates = [item["date"] for item in place["visits"]]
        place["first_visit"] = min(dates)
        place["latest_visit"] = max(dates)

    current = start_date
    while current <= end_date:
        plan, _track_points = build_plan(events, current, user_tags or {}, home_filter, stop_jitter_filter)
        for segment in plan.get("travel_segments", []):
            all_travel_segments.append({**segment, "date": plan["date"], "map": f"/owntracks/map/{plan['date']}"})
        covered_names: set[str] = set()
        transition_visits = transition_index_events(events, current)
        for stop in plan.get("candidate_stops", []):
            lat = as_float(stop.get("lat"))
            lon = as_float(stop.get("lon"))
            key, label = stop_index_identity(stop)
            covered_names.add(label.casefold())
            tags = [str(tag) for tag in (stop.get("user_tags") or stop.get("tags") or []) if str(tag).strip()]
            duration_minutes = int(stop.get("duration_minutes") or 0)
            visit = {
                "date": plan["date"],
                "alias": stop.get("alias", ""),
                "id": stop.get("id", ""),
                "name": label,
                "raw_name": stop.get("name", ""),
                "start": stop.get("entry_display") or stop.get("start", ""),
                "end": stop.get("exit_display") or stop.get("end", ""),
                "start_timestamp": stop.get("visit_start_timestamp", stop.get("start_timestamp")),
                "end_timestamp": stop.get("visit_end_timestamp", stop.get("end_timestamp")),
                "duration": stop.get("visit_duration") or stop.get("duration", ""),
                "duration_minutes": int(stop.get("visit_duration_minutes", duration_minutes) or 0),
                "lat": lat,
                "lon": lon,
                "points": stop.get("points", 0),
                "motion": stop.get("motion", ""),
                "motion_mode": stop.get("motion_mode", "unknown"),
                "tags": tags,
                "note": stop.get("user_note", ""),
                "media": [item for item in stop.get("media", []) if isinstance(item, dict)],
                "maps": stop.get("maps", ""),
                "reviewed": bool(stop.get("reviewed_name") or stop.get("user_tags") or stop.get("user_note") or stop.get("media")),
                "manual": bool(stop.get("manual")),
                "source": "stop",
                "confidence": stop.get("confidence"),
                "entry_status": stop.get("entry_status"),
                "exit_status": stop.get("exit_status"),
                "entry_window": stop.get("entry_window"),
                "exit_window": stop.get("exit_window"),
            }
            add_visit(key, label, visit)
        for transition in transition_visits:
            transition_key = f"name:{str(transition.get('name') or '').casefold()}"
            covered_names.add(str(transition.get("name") or "").casefold())
            add_visit(transition_key, str(transition.get("name") or "Transition"), transition)
        for waypoint in waypoint_index_events(events, current, covered_names):
            waypoint_key = f"name:{str(waypoint.get('name') or '').casefold()}"
            covered_names.add(str(waypoint.get("name") or "").casefold())
            add_visit(waypoint_key, str(waypoint.get("name") or "Waypoint"), waypoint)
        for saved_place in saved_place_index_events(events, current, user_tags or {}, covered_names):
            saved_place_key = f"name:{str(saved_place.get('name') or '').casefold()}"
            add_visit(saved_place_key, str(saved_place.get("name") or "Saved place"), saved_place)
        current += timedelta(days=1)

    places = list(grouped.values())
    for place in places:
        place["visits"].sort(key=lambda item: (item["date"], item.get("start_timestamp") or 0), reverse=True)
    places.sort(key=lambda item: (str(item["name"]).casefold(), -int(item["visit_count"])))
    travel_pairs = travel_pair_summaries(all_travel_segments)
    return {
        "title": "OwnTracks stop index",
        "scope": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": (end_date - start_date).days + 1,
        },
        "stats": {
            "places": len(places),
            "visits": sum(int(place["visit_count"]) for place in places),
            "reviewed_visits": sum(int(place["reviewed_visits"]) for place in places),
            "total_minutes": sum(int(place["total_minutes"]) for place in places),
            "travel_pairs": len(travel_pairs),
        },
        "places": places,
        "travel_segments": all_travel_segments,
        "travel_pairs": travel_pairs,
    }


def build_activity_dashboard_summary(
    events: list[Event],
    user_tags: dict | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    home_filter: HomeFilterConfig | None = None,
    stop_jitter_filter: StopJitterFilterConfig | None = None,
) -> dict:
    start_date, end_date = stop_index_date_range(events, start, end)
    if start_date is None or end_date is None:
        return {
            "title": "OwnTracks activity dashboard",
            "scope": {"start": "", "end": "", "days": 0},
            "stats": {},
            "daily": [],
            "top_places": [],
        }

    dashboard_home_filter = home_filter
    if home_filter and not home_filter.enabled:
        dashboard_home_filter = HomeFilterConfig(True, home_filter.region_names, home_filter.radius_m)
    anchors = home_anchors(events, dashboard_home_filter)
    stop_index = build_stop_index_summary(
        events,
        user_tags,
        start=start_date,
        end=end_date,
        home_filter=home_filter,
        stop_jitter_filter=stop_jitter_filter,
    )
    visits_by_date: dict[str, list[dict]] = {}
    for place in stop_index.get("places", []):
        for visit in place.get("visits", []):
            visits_by_date.setdefault(str(visit.get("date") or ""), []).append({**visit, "place_name": place.get("name")})

    daily: list[dict] = []
    current = start_date
    while current <= end_date:
        date_text = current.isoformat()
        day_points = [event for event in events if event_date(event) == current and event.is_location]
        day_points.sort(key=event_time)
        outside_points = [
            event for event in day_points if not is_home_area_point(event, dashboard_home_filter, anchors)
        ]
        distance_km = round(summarize_distance(day_points), 2)
        outside_distance_km = round(summarize_distance(outside_points), 2)
        outside_minutes = estimate_outside_home_minutes(day_points, dashboard_home_filter, anchors)
        motion = motion_summary(day_points)
        day_visits = visits_by_date.get(date_text, [])
        top_visit_names = sorted({str(visit.get("place_name") or visit.get("name") or "") for visit in day_visits if str(visit.get("place_name") or visit.get("name") or "").strip()})
        has_outside = bool(outside_points)
        travel_day = distance_km >= 20 or outside_distance_km >= 10 or outside_minutes >= 180
        daily.append(
            {
                "date": date_text,
                "points": len(day_points),
                "outside_points": len(outside_points),
                "distance_km": distance_km,
                "outside_distance_km": outside_distance_km,
                "outside_minutes": outside_minutes,
                "home_only": bool(day_points) and not has_outside,
                "out_of_home": has_outside,
                "travel_day": travel_day,
                "dominant_motion": motion.get("dominant") or "unknown",
                "motion_counts": motion.get("counts") or {},
                "visit_count": len(day_visits),
                "places": top_visit_names[:6],
            }
        )
        current += timedelta(days=1)

    observed_days = [day for day in daily if int(day["points"]) > 0]
    out_days = [day for day in observed_days if day["out_of_home"]]
    home_only_days = [day for day in observed_days if day["home_only"]]
    travel_days = [day for day in observed_days if day["travel_day"]]
    current_out_streak = current_activity_streak_days(daily, "out_of_home")
    current_home_streak = current_activity_streak_days(daily, "home_only")
    longest_out_streak = longest_activity_streak_days(daily, "out_of_home")
    longest_home_streak = longest_activity_streak_days(daily, "home_only")
    longest_travel_streak = longest_activity_streak_days(daily, "travel_day")
    total_distance = round(sum(float(day["distance_km"]) for day in observed_days), 2)
    outside_distance = round(sum(float(day["outside_distance_km"]) for day in observed_days), 2)
    outside_minutes_total = sum(int(day["outside_minutes"]) for day in observed_days)
    top_places = sorted(
        stop_index.get("places", []),
        key=lambda place: (int(place.get("visit_count") or 0), int(place.get("total_minutes") or 0), str(place.get("name") or "")),
        reverse=True,
    )[:15]
    return {
        "title": "OwnTracks activity dashboard",
        "scope": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": (end_date - start_date).days + 1,
        },
        "stats": {
            "observed_days": len(observed_days),
            "home_only_days": len(home_only_days),
            "out_of_home_days": len(out_days),
            "travel_days": len(travel_days),
            "total_distance_km": total_distance,
            "outside_distance_km": outside_distance,
            "outside_minutes": outside_minutes_total,
            "places": len(stop_index.get("places", [])),
            "visits": sum(int(place.get("visit_count") or 0) for place in stop_index.get("places", [])),
            "current_out_of_home_streak_days": current_out_streak,
            "current_home_only_streak_days": current_home_streak,
            "longest_out_of_home_streak_days": longest_out_streak,
            "longest_home_only_streak_days": longest_home_streak,
            "longest_travel_streak_days": longest_travel_streak,
        },
        "daily": daily,
        "streaks": {
            "out_of_home": activity_streaks(daily, "out_of_home"),
            "home_only": activity_streaks(daily, "home_only"),
            "travel": activity_streaks(daily, "travel_day"),
        },
        "top_places": top_places,
    }


def current_activity_streak_days(daily: list[dict], key: str) -> int:
    count = 0
    for day in reversed(daily):
        if int(day.get("points") or 0) <= 0:
            if count:
                break
            continue
        if not day.get(key):
            break
        count += 1
    return count


def longest_activity_streak_days(daily: list[dict], key: str) -> int:
    streak = longest_activity_streak(daily, key)
    return int(streak.get("days") or 0) if streak else 0


def longest_activity_streak(daily: list[dict], key: str) -> dict | None:
    streaks = activity_streaks(daily, key, min_days=1)
    return streaks[0] if streaks else None


def activity_streaks(daily: list[dict], key: str, min_days: int = 2) -> list[dict]:
    streaks: list[dict] = []
    current: list[dict] = []
    for day in daily:
        if day.get(key):
            current.append(day)
            continue
        if len(current) >= min_days:
            streaks.append(activity_streak_from_days(current))
        current = []
    if len(current) >= min_days:
        streaks.append(activity_streak_from_days(current))
    return sorted(streaks, key=lambda item: (int(item["days"]), str(item["end"])), reverse=True)


def activity_streak_from_days(days: list[dict]) -> dict:
    return {
        "start": days[0]["date"],
        "end": days[-1]["date"],
        "days": len(days),
        "distance_km": round(sum(float(day.get("distance_km") or 0) for day in days), 2),
        "outside_minutes": sum(int(day.get("outside_minutes") or 0) for day in days),
        "travel_days": sum(1 for day in days if day.get("travel_day")),
    }


def estimate_outside_home_minutes(
    points: list[Event],
    home_filter: HomeFilterConfig | None,
    anchors: list[tuple[float, float, str]],
) -> int:
    if len(points) < 2:
        return 0
    minutes = 0.0
    sorted_points = sorted(points, key=event_time)
    for previous, current in zip(sorted_points, sorted_points[1:]):
        elapsed = (event_time(current) - event_time(previous)).total_seconds() / 60
        if elapsed <= 0 or elapsed > 180:
            continue
        if not is_home_area_point(previous, home_filter, anchors) or not is_home_area_point(current, home_filter, anchors):
            minutes += min(elapsed, 60)
    return int(round(minutes))


def render_activity_dashboard_html(summary: dict) -> str:
    title = escape(summary["title"])
    scope = summary.get("scope") or {}
    stats = summary.get("stats") or {}
    payload = json.dumps(summary, ensure_ascii=False).replace("</", "<\\/")
    nav = owntracks_nav_html("dashboard", start=scope.get("start"), end=scope.get("end"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ background: #f8fafc; color: #111827; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; }}
    [hidden] {{ display: none !important; }}
    header {{ background: white; border-bottom: 1px solid #d1d5db; padding: 14px 18px; position: sticky; top: 0; z-index: 5; }}
    h1 {{ font-size: 20px; margin: 0 0 8px; }}
    .subtle {{ color: #4b5563; font-size: 13px; line-height: 1.4; }}
{OWNTRACKS_NAV_CSS}
    .mobile-filter-toggle {{ display: none; margin-top: 10px; width: 100%; }}
    .filters {{ align-items: end; display: grid; gap: 10px; grid-template-columns: minmax(160px, 210px) repeat(2, minmax(140px, 180px)) auto minmax(180px, 1fr) auto; margin-top: 12px; }}
    label {{ color: #374151; display: grid; font-size: 12px; font-weight: 800; gap: 4px; }}
    input, select {{ border: 1px solid #d1d5db; border-radius: 6px; font: inherit; min-height: 36px; padding: 7px 9px; width: 100%; }}
    input[type="checkbox"] {{ min-height: 0; width: auto; }}
    .toggle {{ align-items: center; border: 1px solid #d1d5db; border-radius: 6px; display: inline-flex; gap: 7px; min-height: 36px; padding: 7px 9px; white-space: nowrap; }}
    button, .button {{ background: #111827; border: 0; border-radius: 6px; color: white; cursor: pointer; display: inline-flex; font: inherit; font-size: 13px; font-weight: 800; justify-content: center; min-height: 36px; padding: 7px 11px; text-decoration: none; }}
    button.secondary, .button.secondary {{ background: #e5e7eb; color: #111827; }}
    main {{ display: grid; gap: 14px; padding: 14px; }}
    .cards {{ display: grid; gap: 10px; grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .card, .panel {{ background: white; border: 1px solid #d1d5db; border-radius: 8px; }}
    .card {{ padding: 10px 12px; }}
    .card span {{ color: #6b7280; display: block; font-size: 11px; font-weight: 850; text-transform: uppercase; }}
    .card strong {{ display: block; font-size: 20px; margin-top: 3px; }}
    .grid {{ display: grid; gap: 14px; grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr); }}
    .panel {{ overflow: hidden; }}
    .panel h2 {{ border-bottom: 1px solid #e5e7eb; font-size: 15px; margin: 0; padding: 10px 12px; }}
    .panel-head {{ align-items: center; border-bottom: 1px solid #e5e7eb; display: flex; flex-wrap: wrap; gap: 10px; justify-content: space-between; padding: 10px 12px; }}
    .panel-head h2 {{ border-bottom: 0; padding: 0; }}
    .legend {{ align-items: center; display: flex; flex-wrap: wrap; gap: 8px 12px; }}
    .legend-item {{ align-items: center; color: #4b5563; display: inline-flex; font-size: 12px; font-weight: 750; gap: 5px; }}
    .legend-swatch {{ border: 1px solid #d1d5db; border-radius: 4px; display: inline-block; height: 12px; width: 22px; }}
    .legend-swatch.empty {{ background: #f3f4f6; border-color: #e5e7eb; }}
    .legend-swatch.home {{ background: #ecfdf5; border-color: #a7f3d0; }}
    .legend-swatch.out {{ background: #eff6ff; border-color: #bfdbfe; }}
    .legend-swatch.travel {{ background: #fff7ed; border-color: #fed7aa; }}
    .calendar {{ display: grid; gap: 6px; grid-template-columns: repeat(7, minmax(0, 1fr)); padding: 12px; }}
    .weekday {{ color: #6b7280; font-size: 11px; font-weight: 850; padding: 0 7px 3px; text-transform: uppercase; }}
    .day {{ border: 1px solid #e5e7eb; border-radius: 7px; color: #111827; min-height: 74px; padding: 7px; position: relative; text-decoration: none; z-index: 1; }}
    .day.pad {{ background: #f9fafb; border-color: #eef2f7; pointer-events: none; }}
    .day.empty {{ background: #f3f4f6; color: #9ca3af; }}
    .day.home {{ background: #ecfdf5; border-color: #a7f3d0; }}
    .day.out {{ background: #eff6ff; border-color: #bfdbfe; }}
    .day.travel {{ background: #fff7ed; border-color: #fed7aa; }}
    .day.connect-prev::before, .day.connect-next::after {{ content: ""; height: 4px; position: absolute; top: 50%; transform: translateY(-50%); width: 7px; z-index: -1; }}
    .day.connect-prev::before {{ left: -7px; }}
    .day.connect-next::after {{ right: -7px; }}
    .day.week-prev::before, .day.week-next::after {{ content: ""; height: 7px; left: 50%; position: absolute; transform: translateX(-50%); width: 4px; z-index: -1; }}
    .day.week-prev::before {{ top: -7px; }}
    .day.week-next::after {{ bottom: -7px; }}
    .day.home.connect-prev::before, .day.home.connect-next::after, .day.home.week-prev::before, .day.home.week-next::after {{ background: #a7f3d0; }}
    .day.out.connect-prev::before, .day.out.connect-next::after, .day.out.week-prev::before, .day.out.week-next::after {{ background: #bfdbfe; }}
    .day.travel.connect-prev::before, .day.travel.connect-next::after, .day.travel.week-prev::before, .day.travel.week-next::after {{ background: #fed7aa; }}
    .day strong {{ display: block; font-size: 13px; }}
    .day span {{ color: #4b5563; display: block; font-size: 11px; line-height: 1.35; margin-top: 2px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; font-size: 13px; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f9fafb; color: #374151; font-size: 12px; text-transform: uppercase; }}
    .places {{ display: grid; gap: 8px; padding: 12px; }}
    .place {{ border: 1px solid #e5e7eb; border-radius: 7px; padding: 9px; }}
    .place strong {{ display: block; font-size: 13px; }}
    .place span {{ color: #4b5563; display: block; font-size: 12px; margin-top: 3px; }}
    @media (max-width: 850px) {{ header {{ max-height: 46vh; overflow-y: auto; padding: 10px 12px; }} h1 {{ font-size: 18px; margin-bottom: 4px; }} .subtle {{ font-size: 12px; }} .mobile-filter-toggle {{ display: inline-flex; }} header:not(.filters-open) .filters {{ display: none; }} .filters, .grid {{ grid-template-columns: 1fr; }} .filters {{ margin-top: 10px; }} .toggle {{ justify-content: center; white-space: normal; }} main {{ padding: 10px; }} .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .card {{ padding: 9px; }} .card strong {{ font-size: 17px; }} .panel {{ overflow-x: auto; }} .calendar {{ gap: 4px; min-width: 560px; }} .weekday {{ font-size: 10px; padding-inline: 3px; }} .day {{ min-height: 68px; min-width: 74px; padding: 5px; }} table {{ min-width: 680px; }} .day.connect-prev::before, .day.connect-next::after {{ width: 5px; }} .day.connect-prev::before {{ left: -5px; }} .day.connect-next::after {{ right: -5px; }} .day.week-prev::before, .day.week-next::after {{ height: 5px; }} .day.week-prev::before {{ top: -5px; }} .day.week-next::after {{ bottom: -5px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="subtle">{escape(scope.get("start") or "")} to {escape(scope.get("end") or "")} · raw movement, home-area classification, and indexed place visits</div>
    {nav}
    <button id="filterToggle" class="mobile-filter-toggle secondary" type="button" aria-controls="rangeForm" aria-expanded="false">Show filters</button>
    <form id="rangeForm" class="filters">
      <label>Preset <select id="rangePreset">
        <option value="">Custom dates</option>
        <option value="last-week">Last week</option>
        <option value="last-7-days">Last 7 days</option>
        <option value="last-month">Last month</option>
        <option value="month-to-date">Month to date</option>
        <option value="year-to-date">Year to date</option>
        <option value="all">All data</option>
      </select></label>
      <label>Start <input id="startDate" name="start" type="date" value="{escape(scope.get("start") or "")}"></label>
      <label>End <input id="endDate" name="end" type="date" value="{escape(scope.get("end") or "")}"></label>
      <button type="submit">Apply range</button>
      <label>Calendar value <select id="metric"><option value="outside">Outside time</option><option value="distance">Distance</option><option value="visits">Visits</option></select></label>
      <label class="toggle"><input id="travelSplit" type="checkbox"> Travel split</label>
    </form>
  </header>
  <main>
    <section class="cards">
      <div class="card"><span>Observed days</span><strong>{int(stats.get("observed_days") or 0)}</strong></div>
      <div class="card"><span>Home-only days</span><strong>{int(stats.get("home_only_days") or 0)}</strong></div>
      <div class="card"><span>Out-of-home days</span><strong>{int(stats.get("out_of_home_days") or 0)}</strong></div>
      <div class="card"><span>Travel days</span><strong>{int(stats.get("travel_days") or 0)}</strong></div>
      <div class="card"><span>Total distance</span><strong>{float(stats.get("total_distance_km") or 0):.1f} km</strong></div>
      <div class="card"><span>Outside distance</span><strong>{float(stats.get("outside_distance_km") or 0):.1f} km</strong></div>
      <div class="card"><span>Outside time</span><strong>{fmt_duration(int(stats.get("outside_minutes") or 0))}</strong></div>
      <div class="card"><span>Place visits</span><strong>{int(stats.get("visits") or 0)}</strong></div>
      <div class="card"><span>Current out streak</span><strong>{int(stats.get("current_out_of_home_streak_days") or 0)} days</strong></div>
      <div class="card"><span>Current home streak</span><strong>{int(stats.get("current_home_only_streak_days") or 0)} days</strong></div>
      <div class="card"><span>Longest out streak</span><strong>{int(stats.get("longest_out_of_home_streak_days") or 0)} days</strong></div>
      <div class="card"><span>Longest home streak</span><strong>{int(stats.get("longest_home_only_streak_days") or 0)} days</strong></div>
      <div class="card"><span>Longest travel streak</span><strong>{int(stats.get("longest_travel_streak_days") or 0)} days</strong></div>
    </section>
    <section class="grid">
      <div class="panel">
        <div class="panel-head">
          <h2>Active day calendar</h2>
          <div class="legend" aria-label="Calendar color legend">
            <span class="legend-item"><span class="legend-swatch empty"></span>No data</span>
            <span class="legend-item"><span class="legend-swatch home"></span>Home only</span>
            <span class="legend-item"><span class="legend-swatch out"></span>Out of home</span>
            <span id="travelLegend" class="legend-item" hidden><span class="legend-swatch travel"></span>Travel day</span>
          </div>
        </div>
        <div id="calendar" class="calendar"></div>
      </div>
      <div class="panel"><h2>Most common places</h2><div id="places" class="places"></div></div>
    </section>
    <section class="panel"><h2>Daily detail</h2><div id="daily"></div></section>
  </main>
  <script>
    const data = {payload};
    const calendar = document.getElementById("calendar");
    const places = document.getElementById("places");
    const daily = document.getElementById("daily");
    const metric = document.getElementById("metric");
    const rangePreset = document.getElementById("rangePreset");
    const travelSplit = document.getElementById("travelSplit");
    const travelLegend = document.getElementById("travelLegend");
    const filterToggle = document.getElementById("filterToggle");
    const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[char]));
    const tokenQuery = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return token ? `?token=${{encodeURIComponent(token)}}` : "";
    }};
    const mapHref = (date) => `/owntracks/map/${{encodeURIComponent(date)}}${{tokenQuery()}}`;
    const stopHref = (name) => {{
      const params = new URLSearchParams(window.location.search);
      params.set("q", name);
      return `/owntracks/stops${{params.toString() ? "?" + params.toString() : ""}}`;
    }};
    const fmtMin = (minutes) => {{
      const value = Number(minutes) || 0;
      if (value < 60) return `${{Math.round(value)}} min`;
      return `${{Math.floor(value / 60)}}h ${{String(Math.round(value % 60)).padStart(2, "0")}}m`;
    }};
    const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const dateParts = (dateText) => {{
      const match = String(dateText || "").match(/^(\\d{{4}})-(\\d{{2}})-(\\d{{2}})$/);
      if (!match) return null;
      return {{ year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) }};
    }};
    const dateInputValue = (date) => {{
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${{year}}-${{month}}-${{day}}`;
    }};
    const addDays = (date, days) => {{
      const next = new Date(date);
      next.setDate(next.getDate() + days);
      return next;
    }};
    const presetRange = (preset) => {{
      const today = new Date();
      const endToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
      if (preset === "last-7-days") return {{ start: dateInputValue(addDays(endToday, -6)), end: dateInputValue(endToday) }};
      if (preset === "last-week") {{
        const mondayBasedDay = (endToday.getDay() + 6) % 7;
        const thisMonday = addDays(endToday, -mondayBasedDay);
        return {{ start: dateInputValue(addDays(thisMonday, -7)), end: dateInputValue(addDays(thisMonday, -1)) }};
      }}
      if (preset === "last-month") {{
        const firstThisMonth = new Date(endToday.getFullYear(), endToday.getMonth(), 1);
        return {{ start: dateInputValue(new Date(endToday.getFullYear(), endToday.getMonth() - 1, 1)), end: dateInputValue(addDays(firstThisMonth, -1)) }};
      }}
      if (preset === "month-to-date") return {{ start: dateInputValue(new Date(endToday.getFullYear(), endToday.getMonth(), 1)), end: dateInputValue(endToday) }};
      if (preset === "year-to-date") return {{ start: dateInputValue(new Date(endToday.getFullYear(), 0, 1)), end: dateInputValue(endToday) }};
      if (preset === "all") return {{ start: "", end: "" }};
      return null;
    }};
    const weekdayIndex = (dateText) => {{
      const parts = dateParts(dateText);
      if (!parts) return 0;
      return (new Date(Date.UTC(parts.year, parts.month - 1, parts.day)).getUTCDay() + 6) % 7;
    }};
    const calendarCells = () => {{
      const days = data.daily || [];
      const first = days.length ? weekdayIndex(days[0].date) : 0;
      const trailing = days.length ? (7 - ((first + days.length) % 7)) % 7 : 0;
      return [
        ...Array.from({{ length: first }}, () => null),
        ...days,
        ...Array.from({{ length: trailing }}, () => null),
      ];
    }};
    const dayValue = (day) => metric.value === "distance" ? day.distance_km : metric.value === "visits" ? day.visit_count : day.outside_minutes;
    const dayClass = (day) => !day.points ? "empty" : travelSplit.checked && day.travel_day ? "travel" : day.out_of_home ? "out" : "home";
    const dayLabel = (day) => !day.points ? "no data" : travelSplit.checked && day.travel_day ? "travel" : day.out_of_home ? "out" : "home";
    const streakKind = (day) => !day || !day.points ? "" : travelSplit.checked && day.travel_day ? "travel" : day.out_of_home ? "out" : day.home_only ? "home" : "";
    const connectorClass = (day, index, days) => {{
      const kind = streakKind(day);
      if (!kind) return "";
      const parts = [];
      const previous = days[index - 1];
      const next = days[index + 1];
      if (previous && streakKind(previous) === kind) parts.push(weekdayIndex(day.date) === 0 ? "week-prev" : "connect-prev");
      if (next && streakKind(next) === kind) parts.push(weekdayIndex(day.date) === 6 ? "week-next" : "connect-next");
      return parts.join(" ");
    }};
    const renderCalendar = () => {{
      travelLegend.hidden = !travelSplit.checked;
      const days = data.daily || [];
      calendar.innerHTML = `
        ${{weekdays.map((weekday) => `<div class="weekday">${{weekday}}</div>`).join("")}}
        ${{calendarCells().map((day) => day ? `
        <a class="day ${{dayClass(day)}} ${{connectorClass(day, days.indexOf(day), days)}}" href="${{escapeHtml(mapHref(day.date))}}">
          <strong>${{escapeHtml(day.date.slice(5))}}</strong>
          <span>${{escapeHtml(weekdays[weekdayIndex(day.date)])}}</span>
          <span>${{escapeHtml(day.points ? `${{dayValue(day)}} ${{metric.value === "distance" ? "km" : metric.value === "visits" ? "visits" : "min outside"}}` : "no data")}}</span>
          <span>${{escapeHtml(day.dominant_motion || "unknown")}}</span>
        </a>
      ` : '<div class="day pad" aria-hidden="true"></div>').join("")}}
      `;
    }};
    const renderPlaces = () => {{
      places.innerHTML = (data.top_places || []).map((place) => `
        <a class="place" href="${{escapeHtml(stopHref(place.name || ""))}}">
          <strong>${{escapeHtml(place.name || "Unknown place")}}</strong>
          <span>${{place.visit_count || 0}} visits · ${{fmtMin(place.total_minutes || 0)}} dwell · latest ${{escapeHtml(place.latest_visit || "")}}</span>
        </a>
      `).join("") || '<div class="place"><strong>No places found</strong></div>';
    }};
    const renderDaily = () => {{
      daily.innerHTML = `
        <table>
          <thead><tr><th>Date</th><th>Class</th><th>Distance</th><th>Outside</th><th>Motion</th><th>Places</th></tr></thead>
          <tbody>
            ${{(data.daily || []).map((day) => `
              <tr>
                <td><a href="${{escapeHtml(mapHref(day.date))}}">${{escapeHtml(day.date)}}</a></td>
                <td>${{dayLabel(day)}}</td>
                <td>${{Number(day.distance_km || 0).toFixed(2)}} km</td>
                <td>${{fmtMin(day.outside_minutes || 0)}} · ${{Number(day.outside_distance_km || 0).toFixed(2)}} km</td>
                <td>${{escapeHtml(day.dominant_motion || "unknown")}}</td>
                <td>${{(day.places || []).map(escapeHtml).join(", ")}}</td>
              </tr>
            `).join("")}}
          </tbody>
        </table>
      `;
    }};
    const applyDashboardRange = () => {{
      const params = new URLSearchParams(window.location.search);
      const start = document.getElementById("startDate").value;
      const end = document.getElementById("endDate").value;
      if (start) params.set("start", start); else params.delete("start");
      if (end) params.set("end", end); else params.delete("end");
      window.location.href = `/owntracks/dashboard?${{params.toString()}}`;
    }};
    document.getElementById("rangeForm").addEventListener("submit", (event) => {{
      event.preventDefault();
      applyDashboardRange();
    }});
    rangePreset.addEventListener("change", () => {{
      const range = presetRange(rangePreset.value);
      if (!range) return;
      document.getElementById("startDate").value = range.start;
      document.getElementById("endDate").value = range.end;
      applyDashboardRange();
    }});
    document.getElementById("startDate").addEventListener("input", () => {{ rangePreset.value = ""; }});
    document.getElementById("endDate").addEventListener("input", () => {{ rangePreset.value = ""; }});
    metric.addEventListener("change", renderCalendar);
    travelSplit.addEventListener("change", () => {{ renderCalendar(); renderDaily(); }});
    filterToggle.addEventListener("click", () => {{
      const header = filterToggle.closest("header");
      const expanded = !header.classList.contains("filters-open");
      header.classList.toggle("filters-open", expanded);
      filterToggle.setAttribute("aria-expanded", String(expanded));
      filterToggle.textContent = expanded ? "Hide filters" : "Show filters";
    }});
{OWNTRACKS_NAV_SCRIPT}
    renderCalendar();
    renderPlaces();
    renderDaily();
  </script>
</body>
</html>"""


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


def render_static_heatmap_html(summary: dict, *, initial_filter: str | None = None) -> str:
    title = escape(summary["title"])
    scope = summary["scope"]
    stats = summary["stats"]
    nav = owntracks_nav_html("heat", start=scope.get("start"), end=scope.get("end"))
    filter_text = (initial_filter or "").strip().lower()

    def matches_filter(spot: dict) -> bool:
        if not filter_text:
            return True
        haystack = " ".join(
            str(part)
            for part in [spot.get("label"), spot.get("mode"), *(spot.get("tags") or [])]
            if part is not None
        ).lower()
        return filter_text in haystack

    points = [spot for spot in summary.get("heat_points", []) if matches_filter(spot)]
    finite_points = [
        (float(spot["lat"]), float(spot["lon"]), spot)
        for spot in points
        if spot.get("lat") is not None and spot.get("lon") is not None
    ]
    width = 1200
    height = 760
    padding = 70
    if finite_points:
        min_lat = min(point[0] for point in finite_points)
        max_lat = max(point[0] for point in finite_points)
        min_lon = min(point[1] for point in finite_points)
        max_lon = max(point[1] for point in finite_points)
    else:
        min_lat = max_lat = min_lon = max_lon = 0.0
    lat_span = max(max_lat - min_lat, 0.00001)
    lon_span = max(max_lon - min_lon, 0.00001)
    scale = min((width - padding * 2) / lon_span, (height - padding * 2) / lat_span)
    x_offset = (width - lon_span * scale) / 2
    y_offset = (height - lat_span * scale) / 2
    max_minutes = max((float(spot.get("duration_minutes") or 0) for _lat, _lon, spot in finite_points), default=1.0)

    circles = []
    for lat, lon, spot in finite_points:
        x = x_offset + ((lon - min_lon) * scale)
        y = height - y_offset - ((lat - min_lat) * scale)
        minutes = float(spot.get("duration_minutes") or 0)
        visits = int(spot.get("visit_count") or 0)
        radius = max(8, min(34, 8 + math.sqrt(max(minutes, visits, 1)) * 1.8))
        opacity = max(0.35, min(0.85, 0.35 + (minutes / max_minutes) * 0.5))
        label = escape(str(spot.get("label") or f"{lat}, {lon}"))
        circles.append(
            f'<g><circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="#d32f2f" '
            f'fill-opacity="{opacity:.2f}" stroke="#7f1d1d" stroke-width="2"><title>{label} · '
            f'{round(minutes)} min · {visits} visits</title></circle>'
            f'<text x="{x + radius + 5:.1f}" y="{y + 4:.1f}">{label}</text></g>'
        )

    ranked = sorted(points, key=lambda spot: float(spot.get("duration_minutes") or 0), reverse=True)[:20]
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(str(spot.get('label') or 'unnamed'))}</td>"
        f"<td>{escape(str(spot.get('mode') or 'moving'))}</td>"
        f"<td>{int(spot.get('visit_count') or 0)}</td>"
        f"<td>{round(float(spot.get('duration_minutes') or 0))}</td>"
        f"<td>{escape(', '.join(str(tag) for tag in spot.get('tags') or []))}</td>"
        "</tr>"
        for spot in ranked
    )
    filter_note = f"<p>Filter: <strong>{escape(initial_filter or '')}</strong></p>" if initial_filter else ""
    empty = "" if points else '<div class="empty">No heatmap points matched this scope/filter.</div>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; color: #0f172a; }}
    header {{ padding: 16px 20px; background: white; border-bottom: 1px solid #cbd5e1; }}
    h1 {{ font-size: 20px; margin: 0 0 6px; }}
{OWNTRACKS_NAV_CSS}
    .subtle {{ color: #475569; font-size: 13px; }}
    .wrap {{ display: grid; gap: 16px; grid-template-columns: minmax(0, 1fr); padding: 16px; }}
    svg {{ background: #e2e8f0; border: 1px solid #cbd5e1; border-radius: 10px; height: auto; max-width: 100%; }}
    text {{ fill: #0f172a; font-size: 12px; font-weight: 700; paint-order: stroke; stroke: white; stroke-width: 3px; }}
    table {{ border-collapse: collapse; width: 100%; background: white; }}
    th, td {{ border: 1px solid #e2e8f0; padding: 7px 9px; text-align: left; font-size: 13px; }}
    th {{ background: #f1f5f9; }}
    .empty {{ background: white; border: 1px solid #cbd5e1; border-radius: 8px; padding: 16px; }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="subtle">{scope["start"]} to {scope["end"]} · {stats["location_points"]} points · {stats["unique_locations"]} locations</div>
    {nav}
    {filter_note}
  </header>
  <main class="wrap">
    {empty}
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}">
      <rect x="0" y="0" width="{width}" height="{height}" fill="#e2e8f0"></rect>
      {''.join(circles)}
    </svg>
    <section>
      <h2>Top locations</h2>
      <table><thead><tr><th>Location</th><th>Mode</th><th>Visits</th><th>Minutes</th><th>Tags</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
  </main>
  <script>
{OWNTRACKS_NAV_SCRIPT}
  </script>
</body>
</html>"""


def render_heatmap_html(summary: dict, *, initial_filter: str | None = None, self_contained: bool = False) -> str:
    if self_contained:
        return render_static_heatmap_html(summary, initial_filter=initial_filter)
    payload = json.dumps(summary, ensure_ascii=False).replace("</", "<\\/")
    initial_filter_payload = json.dumps(initial_filter or "", ensure_ascii=False).replace("</", "<\\/")
    title = escape(summary["title"])
    scope = summary["scope"]
    stats = summary["stats"]
    nav = owntracks_nav_html("heat", start=scope.get("start"), end=scope.get("end"))
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
{OWNTRACKS_NAV_CSS}
    .panel .ot-nav {{
      margin-bottom: 8px;
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
    {nav}
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
    const initialFilterText = {initial_filter_payload};
{OWNTRACKS_NAV_SCRIPT}
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
      marker.bindPopup(`<strong>${{escapeHtml(spot.label)}}</strong><br>${{metricConfig().title}}: ${{escapeHtml(metricLabel(spot))}}<br>Visits: ${{spot.visitCount || 0}}<br>Raw points: ${{spot.rawCount || 0}}`);
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
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"]/g, (char) => char === "&" ? "&amp;" : char === "<" ? "&lt;" : char === ">" ? "&gt;" : "&quot;");
    const normalizeFilterText = (value) => String(value || "").trim().toLowerCase();
    const params = new URLSearchParams(window.location.search || "");
    const requestedFilter = normalizeFilterText(initialFilterText || params.get("filter") || "");
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
    const textMatches = (spot) => !requestedFilter || [spot.label, spot.mode, ...(spot.tags || [])].some((part) => normalizeFilterText(part).includes(requestedFilter));
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
      filteredSpots = allSpots.filter((spot) => modeMatches(spot) && textMatches(spot));
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
          <div class="name">${{escapeHtml(spot.label)}}</div>
          <div class="count">${{escapeHtml(metricLabel(spot))}}</div>
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


def render_stop_index_html(summary: dict) -> str:
    title = escape(summary["title"])
    scope = summary.get("scope") or {}
    stats = summary.get("stats") or {}
    alias_meta = summary.get("search_aliases_meta") or {}
    generated_alias_meta = alias_meta.get("generated") or {}
    alias_updated_at = generated_alias_meta.get("updated_at")
    alias_status = (
        f"{int(alias_meta.get('categories') or 0)} active alias categories · "
        f"{int(alias_meta.get('terms') or 0)} terms · "
        f"last generated {alias_updated_at}"
        if alias_updated_at
        else f"{len(summary.get('search_aliases') or {})} active alias categories · not generated yet"
    )
    payload = json.dumps(summary, ensure_ascii=False).replace("</", "<\\/")
    nav = owntracks_nav_html("stops", start=scope.get("start"), end=scope.get("end"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --border: #d1d5db;
      --ink: #111827;
      --muted: #4b5563;
      --panel: #ffffff;
      --soft: #f3f4f6;
      --accent: #0f766e;
      --accent-soft: #ccfbf1;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      background: #f8fafc;
      color: var(--ink);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 14px 18px;
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    h1 {{
      font-size: 20px;
      line-height: 1.2;
      margin: 0 0 8px;
    }}
{OWNTRACKS_NAV_CSS}
    .mobile-filter-toggle {{
      display: none;
      margin-top: 10px;
      width: 100%;
    }}
    .subtle {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
    }}
    .filters {{
      align-items: end;
      display: grid;
      gap: 10px;
      grid-template-columns: minmax(180px, 1fr) repeat(2, minmax(130px, 160px)) auto;
      margin-top: 12px;
    }}
    label {{
      color: #374151;
      display: grid;
      font-size: 12px;
      font-weight: 800;
      gap: 4px;
    }}
    input, select {{
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--ink);
      font: inherit;
      min-height: 36px;
      padding: 7px 9px;
      width: 100%;
    }}
    button, .button {{
      align-items: center;
      background: var(--ink);
      border: 0;
      border-radius: 6px;
      color: white;
      cursor: pointer;
      display: inline-flex;
      font: inherit;
      font-size: 13px;
      font-weight: 800;
      justify-content: center;
      min-height: 36px;
      padding: 7px 11px;
      text-decoration: none;
    }}
    button.secondary, .button.secondary {{
      background: #e5e7eb;
      color: var(--ink);
    }}
    main {{
      display: grid;
      gap: 14px;
      grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
      padding: 14px;
    }}
    .stats {{
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin-top: 10px;
    }}
    .stat {{
      background: var(--soft);
      border: 1px solid #e5e7eb;
      border-radius: 7px;
      padding: 8px 10px;
    }}
    .stat span {{
      color: var(--muted);
      display: block;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .stat strong {{
      display: block;
      font-size: 17px;
      margin-top: 2px;
    }}
    .alias-actions {{
      align-items: center;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .alias-status {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
    }}
    .places, .details {{
      min-height: calc(100vh - 160px);
    }}
    .places {{
      display: grid;
      gap: 8px;
      max-height: calc(100vh - 164px);
      overflow: auto;
    }}
    .place {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      cursor: pointer;
      padding: 10px;
      text-align: left;
      width: 100%;
    }}
    .place.active {{
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent-soft) inset;
    }}
    .place-name {{
      display: block;
      font-size: 14px;
      font-weight: 850;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .place-meta {{
      color: var(--muted);
      display: block;
      font-size: 12px;
      line-height: 1.4;
      margin-top: 4px;
    }}
    .tags {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 7px;
    }}
    .tag {{
      background: #eef2ff;
      border-radius: 999px;
      color: #3730a3;
      font-size: 11px;
      font-weight: 750;
      padding: 2px 7px;
    }}
    .details {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    .detail-head {{
      border-bottom: 1px solid var(--border);
      padding: 14px;
    }}
    .detail-head h2 {{
      font-size: 18px;
      margin: 0 0 6px;
      overflow-wrap: anywhere;
    }}
    .visits {{
      display: grid;
      gap: 10px;
      padding: 12px;
    }}
    .visit {{
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 10px;
    }}
    .visit-top {{
      align-items: center;
      display: flex;
      gap: 8px;
      justify-content: space-between;
      margin-bottom: 6px;
    }}
    .visit-date {{
      font-size: 14px;
      font-weight: 850;
    }}
    .visit-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .visit-actions .button {{
      font-size: 12px;
      min-height: 30px;
      padding: 5px 8px;
    }}
    .note {{
      background: #fffbeb;
      border: 1px solid #fde68a;
      border-radius: 6px;
      color: #78350f;
      font-size: 13px;
      line-height: 1.4;
      margin-top: 8px;
      padding: 8px;
      white-space: pre-wrap;
    }}
    .media-grid {{
      display: grid;
      gap: 8px;
      grid-template-columns: repeat(auto-fill, minmax(104px, 1fr));
      margin-top: 8px;
    }}
    .media-card {{
      border: 1px solid #e5e7eb;
      border-radius: 7px;
      overflow: hidden;
      background: #f9fafb;
    }}
    .media-card img {{
      aspect-ratio: 4 / 3;
      display: block;
      object-fit: cover;
      width: 100%;
    }}
    .media-file {{
      align-items: center;
      aspect-ratio: 4 / 3;
      color: var(--muted);
      display: flex;
      font-size: 12px;
      font-weight: 800;
      justify-content: center;
      padding: 8px;
      text-align: center;
    }}
    .media-caption {{
      color: #374151;
      font-size: 12px;
      line-height: 1.35;
      padding: 6px;
      overflow-wrap: anywhere;
    }}
    .media-actions {{
      display: flex;
      gap: 6px;
      padding: 0 6px 6px;
    }}
    .media-actions button, .media-actions .button {{
      flex: 1;
      font-size: 11px;
      min-height: 28px;
      padding: 4px 6px;
    }}
    .media-upload {{
      background: #f8fafc;
      border: 1px dashed #cbd5e1;
      border-radius: 7px;
      display: grid;
      gap: 7px;
      margin-top: 8px;
      padding: 8px;
    }}
    .media-upload input[type="file"] {{
      min-height: 0;
      padding: 5px;
    }}
    .media-status {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
    }}
    .empty {{
      color: var(--muted);
      font-size: 14px;
      padding: 18px;
    }}
    @media (max-width: 800px) {{
      header {{
        max-height: 52vh;
        overflow-y: auto;
        padding: 10px 12px;
      }}
      h1 {{
        font-size: 18px;
        margin-bottom: 4px;
      }}
      .subtle {{
        font-size: 12px;
      }}
      .mobile-filter-toggle {{
        display: inline-flex;
      }}
      header:not(.filters-open) .filters {{
        display: none;
      }}
      .filters {{
        grid-template-columns: 1fr;
        margin-top: 10px;
      }}
      .stats {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      main {{
        grid-template-columns: 1fr;
      }}
      .places {{
        max-height: 42vh;
      }}
      .places, .details {{
        min-height: 0;
      }}
      .visit-top {{
        align-items: flex-start;
        flex-direction: column;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="subtle">{escape(scope.get("start") or "no data")} to {escape(scope.get("end") or "no data")} · built from raw OwnTracks logs and saved stop reviews</div>
    {nav}
    <button id="filterToggle" class="mobile-filter-toggle secondary" type="button" aria-controls="rangeForm" aria-expanded="false">Show filters</button>
    <form id="rangeForm" class="filters">
      <label>Search
        <input id="search" name="q" placeholder="doctor, dentist, office, tag, note, date">
      </label>
      <label>Start
        <input id="startDate" name="start" type="date" value="{escape(scope.get("start") or "")}">
      </label>
      <label>End
        <input id="endDate" name="end" type="date" value="{escape(scope.get("end") or "")}">
      </label>
      <button type="submit">Apply range</button>
    </form>
    <div class="alias-actions">
      <button id="refreshAliases" type="button" class="secondary">Refresh search aliases</button>
      <span id="aliasStatus" class="alias-status">{escape(alias_status)}</span>
    </div>
    <div class="stats">
      <div class="stat"><span>Places</span><strong>{int(stats.get("places") or 0)}</strong></div>
      <div class="stat"><span>Visits</span><strong>{int(stats.get("visits") or 0)}</strong></div>
      <div class="stat"><span>Reviewed</span><strong>{int(stats.get("reviewed_visits") or 0)}</strong></div>
      <div class="stat"><span>Total dwell</span><strong>{fmt_duration(int(stats.get("total_minutes") or 0))}</strong></div>
    </div>
  </header>
  <main>
    <section id="placeList" class="places" aria-label="Stop places"></section>
    <section id="details" class="details" aria-label="Visit details"></section>
  </main>
  <script>
    const data = {payload};
    const placeList = document.getElementById("placeList");
    const details = document.getElementById("details");
    const search = document.getElementById("search");
    const rangeForm = document.getElementById("rangeForm");
    const refreshAliases = document.getElementById("refreshAliases");
    const aliasStatus = document.getElementById("aliasStatus");
    const filterToggle = document.getElementById("filterToggle");
    const searchAliases = data.search_aliases || {{}};
    let activeKey = "";
    const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({{
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }}[char]));
    const normalize = (value) => String(value == null ? "" : value).trim().toLowerCase();
    const tokenQuery = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return token ? `?token=${{encodeURIComponent(token)}}` : "";
    }};
    const mapHref = (visit) => `/owntracks/map/${{encodeURIComponent(visit.date)}}${{tokenQuery()}}`;
    const mediaHref = (visit, media) => {{
      if (!media || !media.filename) return "#";
      const token = tokenQuery();
      return `/owntracks/media/${{encodeURIComponent(visit.date)}}/${{encodeURIComponent(media.filename)}}${{token}}`;
    }};
{OWNTRACKS_NAV_SCRIPT}
    const termsForText = (value) => normalize(value).split(/[^a-z0-9:_-]+/).filter(Boolean);
    const expandTerms = (parts) => {{
      const values = parts.flatMap((part) => termsForText(part));
      const expanded = new Set(values);
      for (const [category, aliases] of Object.entries(searchAliases)) {{
        const categoryTerm = normalize(category);
        const aliasTerms = (aliases || []).map(normalize).filter(Boolean);
        const matched = aliasTerms.some((term) => expanded.has(term)) || expanded.has(categoryTerm);
        if (!matched) continue;
        expanded.add(categoryTerm);
        expanded.add(`category:${{categoryTerm}}`);
        aliasTerms.forEach((term) => expanded.add(term));
      }}
      return [...expanded].join(" ");
    }};
    const rawPlaceParts = (place) => [
      place.name,
      place.first_visit,
      place.latest_visit,
      ...(place.tags || []),
      ...(place.visits || []).flatMap((visit) => [
        visit.date,
        visit.raw_name,
        visit.alias,
        visit.id,
        visit.note,
        visit.motion,
        visit.motion_mode,
        visit.source,
        ...(visit.media || []).flatMap((media) => [media.caption, media.original_name, media.content_type]),
        ...(visit.tags || []),
      ]),
    ];
    const placeText = (place) => expandTerms(rawPlaceParts(place));
    const containsTerm = (value, term) => normalize(value).includes(term);
    const exactTerm = (value, term) => normalize(value) === term;
    const termScore = (value, term, exactPoints, containsPoints) => {{
      const text = normalize(value);
      if (!text) return 0;
      if (text === term) return exactPoints;
      if (text.startsWith(`${{term}} `) || text.includes(` ${{term}} `) || text.endsWith(` ${{term}}`)) return Math.round(containsPoints * 1.2);
      return text.includes(term) ? containsPoints : 0;
    }};
    const placeSearchScore = (place, directTerms, expandedTerms) => {{
      const direct = new Set(directTerms);
      let score = 0;
      for (const term of expandedTerms) {{
        const multiplier = direct.has(term) ? 4 : 1;
        score += multiplier * termScore(place.name, term, 1000, 700);
        for (const tag of place.tags || []) score += multiplier * termScore(tag, term, 800, 550);
        for (const visit of place.visits || []) {{
          score += multiplier * termScore(visit.raw_name, term, 650, 420);
          score += multiplier * termScore(visit.note, term, 520, 320);
          score += multiplier * termScore(visit.id, term, 220, 120);
          score += multiplier * termScore(visit.alias, term, 180, 90);
          for (const tag of visit.tags || []) score += multiplier * termScore(tag, term, 720, 500);
        }}
        if (!direct.has(term) && placeText(place).includes(term)) score += 20;
      }}
      score += Math.min(Number(place.reviewed_visits) || 0, 10) * 8;
      return score;
    }};
    const filteredPlaces = () => {{
      const directTerms = termsForText(search.value);
      const query = expandTerms([search.value]);
      const places = data.places || [];
      if (!query) return places;
      const expandedTerms = query.split(" ").filter(Boolean);
      return places
        .filter((place) => expandedTerms.every((term) => placeText(place).includes(term)))
        .map((place) => [placeSearchScore(place, directTerms, expandedTerms), place])
        .sort((left, right) =>
          right[0] - left[0]
          || String(right[1].latest_visit || "").localeCompare(String(left[1].latest_visit || ""))
          || (Number(right[1].visit_count) || 0) - (Number(left[1].visit_count) || 0)
          || (Number(right[1].total_minutes) || 0) - (Number(left[1].total_minutes) || 0)
          || String(left[1].name || "").localeCompare(String(right[1].name || ""))
        )
        .map((item) => item[1]);
    }};
    const formatMinutes = (minutes) => {{
      const value = Number(minutes) || 0;
      if (value < 60) return `${{value}} min`;
      return `${{Math.floor(value / 60)}}h ${{String(value % 60).padStart(2, "0")}}m`;
    }};
    const renderTags = (tags) => (tags || []).length
      ? `<div class="tags">${{tags.map((tag) => `<span class="tag">${{escapeHtml(tag)}}</span>`).join("")}}</div>`
      : "";
    const visitMediaKey = (visit) => `${{visit.date || ""}}-${{visit.id || ""}}`;
    const renderMedia = (visit) => {{
      const media = visit.media || [];
      if (!media.length) return "";
      return `<div class="media-grid">${{media.map((item) => {{
        const href = mediaHref(visit, item);
        const preview = item.kind === "image" || String(item.content_type || "").startsWith("image/")
          ? `<a href="${{escapeHtml(href)}}" target="_blank" rel="noreferrer"><img src="${{escapeHtml(href)}}" alt="${{escapeHtml(item.caption || item.original_name || "attachment")}}"></a>`
          : `<a class="media-file" href="${{escapeHtml(href)}}" target="_blank" rel="noreferrer">${{escapeHtml(item.original_name || "Attachment")}}</a>`;
        return `<div class="media-card">
          ${{preview}}
          ${{item.caption ? `<div class="media-caption">${{escapeHtml(item.caption)}}</div>` : ""}}
          <div class="media-actions">
            <a class="button secondary" href="${{escapeHtml(href)}}" target="_blank" rel="noreferrer">Open</a>
            <button type="button" class="secondary" data-delete-media="${{escapeHtml(item.id || "")}}" data-visit-id="${{escapeHtml(visit.id || "")}}" data-visit-date="${{escapeHtml(visit.date || "")}}">Delete</button>
          </div>
        </div>`;
      }}).join("")}}</div>`;
    }};
    const uploadEndpoint = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return `/owntracks/media${{token ? `?token=${{encodeURIComponent(token)}}` : ""}}`;
    }};
    const deleteMediaEndpoint = uploadEndpoint;
    const uploadVisitMedia = async (visit) => {{
      const key = visitMediaKey(visit);
      const fileInput = details.querySelector(`[data-media-file="${{CSS.escape(key)}}"]`);
      const captionInput = details.querySelector(`[data-media-caption="${{CSS.escape(key)}}"]`);
      const status = details.querySelector(`[data-media-status="${{CSS.escape(key)}}"]`);
      const file = fileInput?.files?.[0];
      if (!file) {{
        if (status) status.textContent = "Choose a file first.";
        return;
      }}
      const form = new FormData();
      form.append("date", visit.date || "");
      form.append("id", visit.id || "");
      form.append("lat", visit.lat ?? "");
      form.append("lon", visit.lon ?? "");
      form.append("caption", captionInput?.value || "");
      form.append("file", file);
      if (status) status.textContent = "Uploading...";
      const response = await fetch(uploadEndpoint(), {{ method: "POST", body: form }});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
      visit.media = [...(visit.media || []), payload.media];
      if (fileInput) fileInput.value = "";
      if (captionInput) captionInput.value = "";
      if (status) status.textContent = "Uploaded.";
      renderDetails((data.places || []).find((place) => place.key === activeKey));
    }};
    const deleteVisitMedia = async (visit, mediaId) => {{
      if (!window.confirm("Delete this attachment?")) return;
      const response = await fetch(deleteMediaEndpoint(), {{
        method: "DELETE",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ date: visit.date, id: visit.id, media_id: mediaId }}),
      }});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
      visit.media = (visit.media || []).filter((item) => item.id !== mediaId);
      renderDetails((data.places || []).find((place) => place.key === activeKey));
    }};
    const setActive = (key) => {{
      activeKey = key;
      renderPlaces();
      renderDetails((data.places || []).find((place) => place.key === key));
    }};
    const renderPlaces = () => {{
      const places = filteredPlaces();
      if (!places.length) {{
        placeList.innerHTML = '<div class="empty">No stop places match this search.</div>';
        renderDetails(null);
        return;
      }}
      if (!places.some((place) => place.key === activeKey)) activeKey = places[0].key;
      placeList.innerHTML = places.map((place) => `
        <button type="button" class="place ${{place.key === activeKey ? "active" : ""}}" data-key="${{escapeHtml(place.key)}}">
          <span class="place-name">${{escapeHtml(place.name)}}</span>
          <span class="place-meta">${{place.visit_count}} visit${{place.visit_count === 1 ? "" : "s"}} · ${{formatMinutes(place.total_minutes)}} · latest ${{escapeHtml(place.latest_visit || "")}}</span>
          ${{renderTags((place.tags || []).slice(0, 5))}}
        </button>
      `).join("");
      placeList.querySelectorAll("[data-key]").forEach((button) => {{
        button.addEventListener("click", () => setActive(button.dataset.key || ""));
      }});
      renderDetails((data.places || []).find((place) => place.key === activeKey));
    }};
    const renderDetails = (place) => {{
      if (!place) {{
        details.innerHTML = '<div class="empty">Select a stop place to see visits.</div>';
        return;
      }}
      details.innerHTML = `
        <div class="detail-head">
          <h2>${{escapeHtml(place.name)}}</h2>
          <div class="subtle">
            ${{place.visit_count}} visit${{place.visit_count === 1 ? "" : "s"}} ·
            ${{formatMinutes(place.total_minutes)}} total dwell ·
            first ${{escapeHtml(place.first_visit || "")}} · latest ${{escapeHtml(place.latest_visit || "")}}
          </div>
          ${{renderTags(place.tags || [])}}
        </div>
        <div class="visits">
          ${{(place.visits || []).map((visit) => `
            <article class="visit">
              <div class="visit-top">
                <div>
                  <div class="visit-date">${{escapeHtml(visit.date)}} · ${{escapeHtml(visit.duration || formatMinutes(visit.duration_minutes))}}</div>
                  <div class="subtle">${{escapeHtml(visit.start)}} to ${{escapeHtml(visit.end)}} · ${{escapeHtml(visit.motion_mode || "unknown")}} · ${{escapeHtml(visit.points || 0)}} points</div>
                  <div class="subtle">${{escapeHtml(visit.source || "visit")}} · confidence ${{escapeHtml(visit.confidence || "unknown")}} · entry ${{escapeHtml(visit.entry_status || "sample")}} · exit ${{escapeHtml(visit.exit_status || "sample")}}</div>
                </div>
                <div class="visit-actions">
                  <a class="button secondary" href="${{escapeHtml(mapHref(visit))}}">Day map</a>
                  ${{visit.maps ? `<a class="button secondary" href="${{escapeHtml(visit.maps)}}" target="_blank" rel="noreferrer">Google Maps</a>` : ""}}
                </div>
              </div>
              ${{renderTags(visit.tags || [])}}
              ${{visit.note ? `<div class="note">${{escapeHtml(visit.note)}}</div>` : ""}}
              ${{renderMedia(visit)}}
              <div class="media-upload">
                <input type="file" data-media-file="${{escapeHtml(visitMediaKey(visit))}}" accept="image/*,.pdf">
                <input type="text" data-media-caption="${{escapeHtml(visitMediaKey(visit))}}" placeholder="Caption">
                <button type="button" class="secondary" data-upload-media="${{escapeHtml(visitMediaKey(visit))}}">Attach media</button>
                <div class="media-status" data-media-status="${{escapeHtml(visitMediaKey(visit))}}"></div>
              </div>
            </article>
          `).join("")}}
        </div>
      `;
      details.querySelectorAll("[data-upload-media]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const visit = (place.visits || []).find((item) => visitMediaKey(item) === button.dataset.uploadMedia);
          if (!visit) return;
          button.disabled = true;
          try {{
            await uploadVisitMedia(visit);
          }} catch (error) {{
            const status = details.querySelector(`[data-media-status="${{CSS.escape(visitMediaKey(visit))}}"]`);
            if (status) status.textContent = `Upload failed: ${{error.message || error}}`;
          }} finally {{
            button.disabled = false;
          }}
        }});
      }});
      details.querySelectorAll("[data-delete-media]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const visit = (place.visits || []).find((item) => item.id === button.dataset.visitId && item.date === button.dataset.visitDate);
          if (!visit) return;
          button.disabled = true;
          try {{
            await deleteVisitMedia(visit, button.dataset.deleteMedia || "");
          }} catch (error) {{
            button.textContent = `Failed`;
            button.title = String(error.message || error);
            button.disabled = false;
          }}
        }});
      }});
    }};
    rangeForm.addEventListener("submit", (event) => {{
      event.preventDefault();
      const params = new URLSearchParams(window.location.search);
      const start = document.getElementById("startDate").value;
      const end = document.getElementById("endDate").value;
      if (start) params.set("start", start); else params.delete("start");
      if (end) params.set("end", end); else params.delete("end");
      if (search.value.trim()) params.set("q", search.value.trim()); else params.delete("q");
      window.location.href = `/owntracks/stops${{params.toString() ? "?" + params.toString() : ""}}`;
    }});
    filterToggle.addEventListener("click", () => {{
      const header = filterToggle.closest("header");
      const expanded = !header.classList.contains("filters-open");
      header.classList.toggle("filters-open", expanded);
      filterToggle.setAttribute("aria-expanded", String(expanded));
      filterToggle.textContent = expanded ? "Hide filters" : "Show filters";
    }});
    const aliasEndpoint = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return `/owntracks/search-aliases${{token ? `?token=${{encodeURIComponent(token)}}` : ""}}`;
    }};
    refreshAliases.addEventListener("click", async () => {{
      refreshAliases.disabled = true;
      refreshAliases.textContent = "Refreshing...";
      aliasStatus.textContent = "Running Codex alias generation for this date range...";
      try {{
        const response = await fetch(aliasEndpoint(), {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            start: document.getElementById("startDate").value,
            end: document.getElementById("endDate").value,
          }}),
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
        aliasStatus.textContent = `Saved ${{payload.categories}} categories and ${{payload.terms}} terms. Reloading...`;
        window.location.reload();
      }} catch (error) {{
        aliasStatus.textContent = `Alias refresh failed: ${{error.message || error}}`;
        refreshAliases.disabled = false;
        refreshAliases.textContent = "Refresh search aliases";
      }}
    }});
    search.addEventListener("input", renderPlaces);
    const params = new URLSearchParams(window.location.search);
    search.value = params.get("q") || "";
    renderPlaces();
  </script>
</body>
</html>"""


def render_trip_html(summary: dict) -> str:
    title = escape(summary["title"])
    payload = json.dumps(summary, ensure_ascii=False).replace("</", "<\\/")
    nav = owntracks_nav_html("trips", date_text=str(summary.get("date") or ""))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; --border:#d1d5db; --ink:#111827; --muted:#4b5563; --panel:#fff; --soft:#f3f4f6; --accent:#0f766e; }}
    * {{ box-sizing: border-box; }}
    body {{ background:#f8fafc; color:var(--ink); font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; margin:0; }}
    header {{ background:var(--panel); border-bottom:1px solid var(--border); padding:14px 18px; position:sticky; top:0; z-index:10; }}
    h1 {{ font-size:20px; line-height:1.2; margin:0 0 8px; }}
{OWNTRACKS_NAV_CSS}
    .subtle {{ color:var(--muted); font-size:13px; line-height:1.4; }}
    .controls {{ align-items:end; display:grid; gap:10px; grid-template-columns:minmax(135px,160px) minmax(180px,1fr) minmax(180px,1fr) auto; margin-top:12px; }}
    label {{ color:#374151; display:grid; font-size:12px; font-weight:800; gap:4px; }}
    input, select {{ border:1px solid var(--border); border-radius:6px; color:var(--ink); font:inherit; min-height:36px; padding:7px 9px; width:100%; }}
    button, .button {{ align-items:center; background:var(--ink); border:0; border-radius:6px; color:white; cursor:pointer; display:inline-flex; font:inherit; font-size:13px; font-weight:800; justify-content:center; min-height:36px; padding:7px 11px; text-decoration:none; }}
    main {{ display:grid; gap:14px; padding:14px; }}
    .answer, .timeline {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; }}
    .answer {{ display:grid; gap:10px; grid-template-columns:repeat(4,minmax(0,1fr)); padding:14px; }}
    .metric {{ background:var(--soft); border:1px solid #e5e7eb; border-radius:7px; padding:10px; }}
    .metric span {{ color:var(--muted); display:block; font-size:11px; font-weight:850; text-transform:uppercase; }}
    .metric strong {{ display:block; font-size:18px; margin-top:3px; overflow-wrap:anywhere; }}
    .wide {{ grid-column:span 2; }}
    .places-panel {{
      background:var(--panel);
      border:1px solid var(--border);
      border-radius:8px;
      overflow:hidden;
    }}
    .places-panel-head {{
      border-bottom:1px solid var(--border);
      display:flex;
      justify-content:space-between;
      gap:12px;
      padding:12px 14px;
    }}
    .places-panel-head h2 {{
      font-size:16px;
      margin:0;
    }}
    .place-grid {{
      display:grid;
      gap:8px;
      grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
      padding:14px;
    }}
    .place-card {{
      background:var(--soft);
      border:1px solid #e5e7eb;
      border-radius:7px;
      padding:10px;
    }}
    .place-card strong {{
      display:block;
      font-size:13px;
      overflow-wrap:anywhere;
    }}
    .place-card .meta {{
      color:var(--muted);
      font-size:11px;
      line-height:1.35;
      margin-top:4px;
    }}
    .tag-row {{
      display:flex;
      flex-wrap:wrap;
      gap:4px;
      margin-top:6px;
    }}
    .tag {{
      background:#e0f2fe;
      border-radius:999px;
      color:#075985;
      font-size:11px;
      font-weight:800;
      padding:2px 6px;
    }}
    .timeline-head {{ align-items:center; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; padding:12px 14px; }}
    .timeline-head h2 {{ font-size:16px; margin:0; }}
    table {{ border-collapse:collapse; width:100%; }}
    th, td {{ border-bottom:1px solid #e5e7eb; font-size:13px; padding:8px 10px; text-align:left; vertical-align:top; }}
    th {{ background:#f9fafb; color:#374151; font-size:11px; font-weight:850; text-transform:uppercase; }}
    tr.hit {{ background:#ecfdf5; }}
    tr.depart {{ background:#eff6ff; }}
    tr.arrive {{ background:#fef3c7; }}
    code {{ background:#f3f4f6; border-radius:4px; padding:1px 4px; }}
    @media (max-width:800px) {{ header {{ max-height:52vh; overflow:auto; padding:10px 12px; }} .controls {{ grid-template-columns:1fr; }} .answer {{ grid-template-columns:1fr; }} .wide {{ grid-column:auto; }} .timeline {{ overflow-x:auto; }} table {{ min-width:780px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="subtle">Deterministic route timing from raw OwnTracks samples, waypoint radii, and saved occurrence reviews.</div>
    {nav}
    <form id="tripForm" class="controls">
      <label>Date <input id="tripDate" type="date"></label>
      <label>From <select id="origin"></select></label>
      <label>To <select id="destination"></select></label>
      <button type="submit">Run</button>
    </form>
  </header>
  <main>
    <section id="answer" class="answer"></section>
    <section class="places-panel">
      <div class="places-panel-head">
        <h2>Trip Places</h2>
        <span class="subtle" id="placeCount"></span>
      </div>
      <div id="placeGrid" class="place-grid"></div>
    </section>
    <section class="timeline">
      <div class="timeline-head">
        <h2>Raw Timeline</h2>
        <span id="timelineCount" class="subtle"></span>
      </div>
      <table>
        <thead><tr><th>Time</th><th>Line</th><th>Mode</th><th>Flag</th><th>Nearest place</th><th>Distance</th></tr></thead>
        <tbody id="timeline"></tbody>
      </table>
    </section>
  </main>
  <script>
    const data = {payload};
{OWNTRACKS_NAV_SCRIPT}
    const origin = document.getElementById("origin");
    const destination = document.getElementById("destination");
    const tripDate = document.getElementById("tripDate");
    const answer = document.getElementById("answer");
    const placeGrid = document.getElementById("placeGrid");
    const placeCount = document.getElementById("placeCount");
    const timeline = document.getElementById("timeline");
    const timelineCount = document.getElementById("timelineCount");
    const escapeHtml = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[char]));
    const placeSourceLabel = (place) => {{
      const sources = Array.isArray(place.sources) ? place.sources : [];
      if (!sources.length) return "place";
      if (sources.includes("review")) return "saved trip place";
      return sources.join(", ");
    }};
    const placeDisplayName = (place) => place.display_name || place.name || "";
    const optionHtml = () => (data.places || []).map((place) => `<option value="${{escapeHtml(place.key)}}">${{escapeHtml(placeDisplayName(place))}} · ${{escapeHtml(place.radius_m)}}m · ${{escapeHtml(placeSourceLabel(place))}}</option>`).join("");
    const renderPlaces = () => {{
      const places = data.places || [];
      placeCount.textContent = `${{places.length}} places`;
      if (!places.length) {{
        placeGrid.innerHTML = '<div class="place-card"><strong>No trip places</strong><div class="meta">Only waypoint, transition, and explicit saved trip places appear here.</div></div>';
        return;
      }}
      placeGrid.innerHTML = places.map((place) => `
        <div class="place-card">
          <strong>${{escapeHtml(placeDisplayName(place))}}</strong>
          <div class="meta">${{escapeHtml(place.radius_m)}} m radius · ${{escapeHtml(placeSourceLabel(place))}}</div>
          <div class="tag-row">${{(Array.isArray(place.sources) ? place.sources : []).map((source) => `<span class="tag">${{escapeHtml(source)}}</span>`).join("")}}</div>
        </div>
      `).join("");
    }};
    const tokenQuery = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return token ? `&token=${{encodeURIComponent(token)}}` : "";
    }};
    const mapLink = () => `/owntracks/map/${{encodeURIComponent(data.date)}}${{window.location.search.includes("token=") ? "?" + new URLSearchParams(window.location.search).toString() : ""}}`;
    const renderAnswer = () => {{
      const query = data.query || {{}};
      if (!query.ok) {{
        answer.innerHTML = `<div class="metric wide"><span>No result</span><strong>${{escapeHtml(query.reason || "Select places and run.")}}</strong></div>`;
        return;
      }}
      const last = query.last_origin;
      answer.innerHTML = `
        <div class="metric"><span>Preferred duration</span><strong>${{escapeHtml(query.duration)}}</strong></div>
        <div class="metric"><span>Departure${{query.departure.corrected ? " corrected" : query.departure.estimated ? " estimate" : ""}}</span><strong>${{escapeHtml(query.departure.time)}}<br><code>line ${{escapeHtml(query.departure.line)}}</code><br>${{escapeHtml(query.departure.source || "sample")}}</strong></div>
        <div class="metric"><span>Arrival${{query.arrival.corrected ? " corrected" : query.arrival.estimated ? " estimate" : ""}}</span><strong>${{escapeHtml(query.arrival.time)}}<br><code>line ${{escapeHtml(query.arrival.line)}}</code><br>${{escapeHtml(query.arrival.source || "sample")}}</strong></div>
        <div class="metric"><span>Arrival distance</span><strong>${{escapeHtml(query.arrival.distance_to_destination_m)}} m</strong></div>
        <div class="metric wide"><span>Rule</span><strong>${{escapeHtml(query.heuristic)}}</strong></div>
        <div class="metric"><span>Last origin sample</span><strong>${{last ? escapeHtml(last.time) + "<br>" + escapeHtml(last.duration) : "none"}}</strong></div>
        <div class="metric"><span>Map</span><strong><a href="${{escapeHtml(mapLink())}}">Open day map</a></strong></div>
      `;
    }};
    const renderTimeline = () => {{
      const departLine = data.query?.departure?.line;
      const arriveLine = data.query?.arrival?.line;
      timelineCount.textContent = `${{(data.timeline || []).length}} samples`;
      timeline.innerHTML = (data.timeline || []).map((row) => {{
        const cls = row.line === departLine ? "depart" : row.line === arriveLine ? "arrive" : "";
        return `<tr class="${{cls}}">
          <td>${{escapeHtml(row.time)}}</td>
          <td><code>${{escapeHtml(row.line)}}</code></td>
          <td>${{escapeHtml(row.motion_mode)}}</td>
          <td>${{escapeHtml(row.t || "")}}</td>
          <td>${{escapeHtml(row.nearest_place || "")}}</td>
          <td>${{row.nearest_distance_m == null ? "" : escapeHtml(row.nearest_distance_m + " m")}}</td>
        </tr>`;
      }}).join("");
    }};
    origin.innerHTML = optionHtml();
    destination.innerHTML = optionHtml();
    origin.value = data.selected?.origin_key || "";
    destination.value = data.selected?.destination_key || "";
    tripDate.value = data.date || "";
    document.getElementById("tripForm").addEventListener("submit", (event) => {{
      event.preventDefault();
      const params = new URLSearchParams();
      if (tripDate.value) params.set("date", tripDate.value);
      if (origin.value) params.set("from", origin.value);
      if (destination.value) params.set("to", destination.value);
      const token = new URLSearchParams(window.location.search).get("token");
      if (token) params.set("token", token);
      window.location.href = `/owntracks/trips?${{params.toString()}}`;
    }});
    renderAnswer();
    renderPlaces();
    renderTimeline();
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
            "start_line": stop.get("start_line"),
            "end_line": stop.get("end_line"),
            "lat": stop.get("lat"),
            "lon": stop.get("lon"),
            "start": stop.get("start", ""),
            "end": stop.get("end", ""),
            "start_timestamp": stop.get("start_timestamp"),
            "end_timestamp": stop.get("end_timestamp"),
            "visit_start_timestamp": stop.get("visit_start_timestamp", stop.get("start_timestamp")),
            "visit_end_timestamp": stop.get("visit_end_timestamp", stop.get("end_timestamp")),
            "entry_display": stop.get("entry_display") or stop.get("start", ""),
            "exit_display": stop.get("exit_display") or stop.get("end", ""),
            "entry_status": stop.get("entry_status"),
            "exit_status": stop.get("exit_status"),
            "entry_override": stop.get("entry_override", ""),
            "exit_override": stop.get("exit_override", ""),
            "entry_window": stop.get("entry_window"),
            "exit_window": stop.get("exit_window"),
            "raw_start": stop.get("raw_start", stop.get("start", "")),
            "raw_end": stop.get("raw_end", stop.get("end", "")),
            "raw_start_timestamp": stop.get("raw_start_timestamp"),
            "raw_end_timestamp": stop.get("raw_end_timestamp"),
            "visit_duration": stop.get("visit_duration") or stop.get("duration", ""),
            "visit_duration_minutes": stop.get("visit_duration_minutes", stop.get("duration_minutes")),
            "visit_source": stop.get("visit_source") or "detected-stop",
            "confidence": stop.get("confidence") or "unknown",
            "evidence": stop.get("evidence") or {},
            "radius_m": stop.get("radius_m") or DEFAULT_VISIT_RADIUS_M,
            "place": bool(stop.get("place")),
            "duration": stop.get("duration", ""),
            "points": stop.get("points", ""),
            "maps": stop.get("maps", ""),
            "tags": stop.get("user_tags", []),
            "note": stop.get("user_note", ""),
            "media": [item for item in stop.get("media", []) if isinstance(item, dict)],
            "previous_travel": stop.get("previous_travel"),
            "next_travel": stop.get("next_travel"),
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
            "poiEvents": plan.get("poi_events", []),
            "possibleMissedStops": plan.get("possible_missed_stops", []),
            "namedPlaces": named_places,
            "travelSegments": plan.get("travel_segments", []),
            "rideSegments": plan.get("ride_segments", []),
            "motionSummary": motion_summary,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    escaped_title = escape(title)
    nav = owntracks_nav_html("day", date_text=str(plan.get("date") or ""))
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
{OWNTRACKS_NAV_CSS}
    .tools .ot-nav {{
      margin: 8px 0;
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
    .selected-duration {{
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      color: #334155;
      font-size: 12px;
      line-height: 1.4;
      margin-top: 8px;
      padding: 8px;
    }}
    .selected-duration strong {{
      color: #111827;
      display: block;
      font-size: 13px;
      margin-bottom: 3px;
    }}
    .selected-duration button {{
      margin-top: 6px;
      min-height: 28px;
      padding: 4px 8px;
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
    .possible-stop-label {{
      background: #f59e0b;
      border: 1px solid #92400e;
      border-radius: 4px;
      color: #111827;
      font-size: 12px;
      font-weight: 800;
      padding: 3px 5px;
    }}
    .poi-label {{
      background: #0f766e;
      border: 1px solid #134e4a;
      border-radius: 4px;
      color: white;
      font-size: 12px;
      font-weight: 800;
      padding: 3px 5px;
    }}
    .poi-image {{
      border-radius: 7px;
      display: block;
      margin-top: 8px;
      max-height: 190px;
      object-fit: contain;
      width: 100%;
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
    .stop-popup .checkbox-row {{
      align-items: center;
      display: flex;
      gap: 6px;
    }}
    .stop-popup .checkbox-row input {{
      width: auto;
    }}
    .popup-meta {{
      color: #4b5563;
      font-size: 12px;
      line-height: 1.4;
    }}
    .media-grid {{
      display: grid;
      gap: 6px;
      grid-template-columns: repeat(auto-fill, minmax(78px, 1fr));
      margin-top: 8px;
    }}
    .media-card {{
      background: #f8fafc;
      border: 1px solid #e5e7eb;
      border-radius: 7px;
      overflow: hidden;
    }}
    .media-card img {{
      aspect-ratio: 4 / 3;
      display: block;
      object-fit: cover;
      width: 100%;
    }}
    .media-file {{
      align-items: center;
      aspect-ratio: 4 / 3;
      color: #4b5563;
      display: flex;
      font-size: 11px;
      font-weight: 800;
      justify-content: center;
      padding: 6px;
      text-align: center;
    }}
    .media-caption {{
      color: #374151;
      font-size: 11px;
      line-height: 1.25;
      padding: 5px;
      overflow-wrap: anywhere;
    }}
    .media-actions {{
      display: flex;
      gap: 5px;
      padding: 0 5px 5px;
    }}
    .media-actions button, .media-actions a {{
      align-items: center;
      background: #e5e7eb;
      border-radius: 5px;
      color: #111827;
      display: inline-flex;
      flex: 1;
      font-size: 10px;
      font-weight: 800;
      justify-content: center;
      min-height: 24px;
      padding: 3px 5px;
      text-decoration: none;
    }}
    .media-upload {{
      background: #f8fafc;
      border: 1px dashed #cbd5e1;
      border-radius: 7px;
      display: grid;
      gap: 6px;
      margin-top: 8px;
      padding: 7px;
    }}
    .media-upload input[type="file"] {{
      background: white;
      font-size: 11px;
      padding: 5px;
    }}
    .media-upload input[type="text"] {{
      font-size: 12px;
    }}
    .media-status {{
      color: #4b5563;
      font-size: 11px;
      font-weight: 750;
      min-height: 14px;
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
    .segment-popup label {{
      color: #374151;
      display: block;
      font-size: 11px;
      font-weight: 800;
      margin: 8px 0 3px;
    }}
    .segment-popup input,
    .segment-popup textarea {{
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      box-sizing: border-box;
      font: inherit;
      padding: 6px;
      width: 100%;
    }}
    .segment-popup textarea {{
      min-height: 62px;
      resize: vertical;
    }}
    .segment-popup .checkbox-row {{
      align-items: center;
      display: flex;
      gap: 6px;
    }}
    .segment-popup .checkbox-row input {{
      width: auto;
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
    {nav}
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
      <div id="selectedDuration" class="selected-duration">Open a point popup and use Set start / Set end to calculate elapsed time.</div>
      <div class="row" style="margin-top: 8px">
        <button id="toggleEdges" type="button" class="secondary">Hide edges</button>
        <button id="toggleArrows" type="button" class="secondary">Hide arrows</button>
      </div>
      <button id="toggleTravelTimes" type="button" class="secondary" style="margin-top: 8px; width: 100%">Show travel times</button>
      <div class="row" style="margin-top: 8px">
        <button id="toggleStopLabels" type="button" class="secondary">Hide stop labels</button>
        <button id="togglePlaceLabels" type="button" class="secondary">Show transition points</button>
      </div>
      <button id="toggleFilteredPoints" type="button" class="secondary" style="margin-top: 8px; width: 100%">Show filtered points</button>
      <button id="togglePois" type="button" class="secondary" style="margin-top: 8px; width: 100%">Show POIs</button>
      <button id="togglePossibleStops" type="button" class="secondary" style="margin-top: 8px; width: 100%">Show possible missed stops</button>
      <div id="possibleStopList" class="stop-list"></div>
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
      <label for="bulkName">Name for selected visits</label>
      <input id="bulkName" placeholder="Name selected visits">
      <div class="row">
        <button id="applyName" type="button">Apply</button>
      </div>
      <div class="profile-title" style="margin-top: 10px">Visits</div>
      <div id="stopList" class="stop-list"></div>
      <button id="saveChanges" type="button" style="margin-top: 8px; width: 100%">Save visit changes</button>
      <div id="saveFeedback" class="status"></div>
      <label id="commandsLabel" for="commands">Telegram fallback</label>
      <textarea id="commands" readonly></textarea>
      <button id="copyCommands" type="button" class="secondary" style="margin-top: 8px; width: 100%">Copy Telegram commands</button>
      <div id="status" class="status">loading</div>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const data = {payload};
{OWNTRACKS_NAV_SCRIPT}
    const selected = new Set();
    const selectedAnchors = new Map();
    const durationAnchors = new Map();
    let durationStartKey = null;
    let durationEndKey = null;
    const originalNames = new Map(data.stops.map((stop) => [stop.alias, stop.name]));
    const originalTags = new Map(data.stops.map((stop) => [stop.alias, (stop.tags || []).join(" ")]));
    const originalNotes = new Map(data.stops.map((stop) => [stop.alias, stop.note || ""]));
    const originalEntryTimes = new Map(data.stops.map((stop) => [stop.alias, stop.entry_override || ""]));
    const originalExitTimes = new Map(data.stops.map((stop) => [stop.alias, stop.exit_override || ""]));
    const originalRadii = new Map(data.stops.map((stop) => [stop.alias, String(stop.radius_m || {DEFAULT_VISIT_RADIUS_M})]));
    const originalPlaces = new Map(data.stops.map((stop) => [stop.alias, Boolean(stop.place)]));
    const tools = document.getElementById("tools");
    const toggleToolsButton = document.getElementById("toggleTools");
    const commands = document.getElementById("commands");
    const status = document.getElementById("status");
    let clipboardStatus = "";
    const map = L.map("map", {{ preferCanvas: true, zoomControl: false }});
    map.createPane("routePointsPane");
    map.getPane("routePointsPane").style.zIndex = "450";
    const osmTiles = L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }});
    const satelliteTiles = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
      maxZoom: 19,
      attribution: "Tiles &copy; Esri"
    }});
    osmTiles.addTo(map);
    L.control.layers({{ "OpenStreetMap": osmTiles, "Satellite": satelliteTiles }}, null, {{
      collapsed: true,
      position: "topright"
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
    // SVG only captures pointer events on circle paths; a canvas renderer would
    // block the segment layer across the entire map surface.
    const routePointRenderer = L.svg({{ pane: "routePointsPane", padding: 0.5 }});
    routePointRenderer.addTo(map);
    const edgeLayer = L.layerGroup().addTo(map);
    const arrowLayer = L.layerGroup().addTo(map);
    const travelTimeLayer = L.layerGroup();
    const placeLayer = L.layerGroup();
    const poiLayer = L.layerGroup();
    const routeLayer = L.layerGroup().addTo(map);
    const filteredPointLayer = L.layerGroup();
    const possibleStopLayer = L.layerGroup();
    const animationLayer = L.layerGroup().addTo(map);
    let activeMotionMode = "all";
    let edgesVisible = true;
    let arrowsVisible = true;
    let travelTimesVisible = false;
    let stopLabelsVisible = true;
    let placeLabelsVisible = false;
    let poisVisible = true;
    let filteredPointsVisible = false;
    let possibleStopsVisible = false;
    let routeColorMode = "speed";
    let profileAxis = "distance";
    let routeAnimationFrame = null;
    let routeAnimationRunning = false;
    let routeAnimationStartMs = null;
    let routeAnimationElapsedMs = 0;
    let routeAnimationStaticVisibility = null;
    const routeAnimationMaxZoom = 16;
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
      status.textContent = `leaflet z${{map.getZoom()}} · selected ${{selected.size}} · visits ${{data.stops.length}}${{clipboardStatus}}`;
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
    const changedStops = () => data.stops.filter((stop) =>
      stop.name !== originalNames.get(stop.alias)
      || (stop.tags || []).join(" ") !== originalTags.get(stop.alias)
      || (stop.note || "") !== originalNotes.get(stop.alias)
      || (stop.entry_override || "") !== originalEntryTimes.get(stop.alias)
      || (stop.exit_override || "") !== originalExitTimes.get(stop.alias)
      || String(stop.radius_m || {DEFAULT_VISIT_RADIUS_M}) !== originalRadii.get(stop.alias)
      || Boolean(stop.place) !== originalPlaces.get(stop.alias)
    );
    const owntracksStopsEndpoint = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return `/owntracks/stops${{token ? `?token=${{encodeURIComponent(token)}}` : ""}}`;
    }};
    const owntracksMediaEndpoint = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return `/owntracks/media${{token ? `?token=${{encodeURIComponent(token)}}` : ""}}`;
    }};
    const owntracksPlaceResolveEndpoint = () => {{
      const token = new URLSearchParams(window.location.search).get("token");
      return `/owntracks/resolve-place${{token ? `?token=${{encodeURIComponent(token)}}` : ""}}`;
    }};
    const mediaHref = (media) => {{
      if (!media || !media.filename) return "#";
      const token = new URLSearchParams(window.location.search).get("token");
      return `/owntracks/media/${{encodeURIComponent(data.date)}}/${{encodeURIComponent(media.filename)}}${{token ? `?token=${{encodeURIComponent(token)}}` : ""}}`;
    }};
    const mediaKey = (stop) => `${{stop.alias}}-${{stop.id}}`;
    const renderMediaGallery = (stop) => {{
      const media = stop.media || [];
      if (!media.length) return "";
      return `<div class="media-grid">${{media.map((item) => {{
        const href = mediaHref(item);
        const preview = item.kind === "image" || String(item.content_type || "").startsWith("image/")
          ? `<a href="${{escapeHtml(href)}}" target="_blank" rel="noreferrer"><img src="${{escapeHtml(href)}}" alt="${{escapeHtml(item.caption || item.original_name || "attachment")}}"></a>`
          : `<a class="media-file" href="${{escapeHtml(href)}}" target="_blank" rel="noreferrer">${{escapeHtml(item.original_name || "Attachment")}}</a>`;
        return `<div class="media-card">
          ${{preview}}
          ${{item.caption ? `<div class="media-caption">${{escapeHtml(item.caption)}}</div>` : ""}}
          <div class="media-actions">
            <a href="${{escapeHtml(href)}}" target="_blank" rel="noreferrer">Open</a>
            <button type="button" data-delete-media="${{escapeHtml(item.id || "")}}" data-media-stop="${{escapeHtml(stop.alias)}}">Delete</button>
          </div>
        </div>`;
      }}).join("")}}</div>`;
    }};
    const renderMediaUpload = (stop, scope) => {{
      const key = `${{scope}}-${{mediaKey(stop)}}`;
      return `<div class="media-upload">
        <input type="file" data-media-file="${{escapeHtml(key)}}" accept="image/*,.pdf">
        <input type="text" data-media-caption="${{escapeHtml(key)}}" placeholder="Caption">
        <button type="button" class="secondary" data-upload-media="${{escapeHtml(stop.alias)}}" data-media-key="${{escapeHtml(key)}}">Attach media</button>
        <div class="media-status" data-media-status="${{escapeHtml(key)}}"></div>
      </div>`;
    }};
    const uploadStopMedia = async (stop, key) => {{
      const fileInput = document.querySelector(`[data-media-file="${{CSS.escape(key)}}"]`);
      const captionInput = document.querySelector(`[data-media-caption="${{CSS.escape(key)}}"]`);
      const statusEl = document.querySelector(`[data-media-status="${{CSS.escape(key)}}"]`);
      const file = fileInput?.files?.[0];
      if (!file) {{
        if (statusEl) statusEl.textContent = "Choose a file first.";
        return false;
      }}
      const form = new FormData();
      form.append("date", data.date);
      form.append("id", stop.id);
      form.append("lat", stop.lat);
      form.append("lon", stop.lon);
      form.append("caption", captionInput?.value || "");
      form.append("file", file);
      if (statusEl) statusEl.textContent = "Uploading...";
      const response = await fetch(owntracksMediaEndpoint(), {{ method: "POST", body: form }});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
      stop.media = [...(stop.media || []), payload.media];
      if (fileInput) fileInput.value = "";
      if (captionInput) captionInput.value = "";
      if (statusEl) statusEl.textContent = "Uploaded.";
      refreshStop(stop);
      renderList();
      attachPopupHandlers(stop);
      return true;
    }};
    const deleteStopMedia = async (stop, mediaId) => {{
      const response = await fetch(owntracksMediaEndpoint(), {{
        method: "DELETE",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ date: data.date, id: stop.id, media_id: mediaId }}),
      }});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
      stop.media = (stop.media || []).filter((item) => item.id !== mediaId);
      refreshStop(stop);
      renderList();
      attachPopupHandlers(stop);
    }};
    const syncStopFromEditors = (stop, popupOnly = false) => {{
      const fields = [
        ["name", popupOnly ? `[data-popup-name="${{stop.alias}}"]` : `[data-name="${{stop.alias}}"]`],
        ["tags", popupOnly ? `[data-popup-tags="${{stop.alias}}"]` : `[data-tags="${{stop.alias}}"]`],
        ["note", popupOnly ? `[data-popup-note="${{stop.alias}}"]` : `[data-note="${{stop.alias}}"]`],
        ["entry_override", popupOnly ? `[data-popup-entry="${{stop.alias}}"]` : `[data-entry="${{stop.alias}}"]`],
        ["exit_override", popupOnly ? `[data-popup-exit="${{stop.alias}}"]` : `[data-exit="${{stop.alias}}"]`],
        ["radius_m", popupOnly ? `[data-popup-radius="${{stop.alias}}"]` : `[data-radius="${{stop.alias}}"]`],
      ];
      for (const [field, selector] of fields) {{
        const editors = document.querySelectorAll(selector);
        const editor = editors.length ? editors[editors.length - 1] : null;
        if (!editor) continue;
        if (field === "tags") stop.tags = parseTags(editor.value);
        else if (field === "radius_m") stop[field] = Number(editor.value) || {DEFAULT_VISIT_RADIUS_M};
        else stop[field] = editor.value.trim();
      }}
      const placeSelector = popupOnly ? `[data-popup-place="${{stop.alias}}"]` : `[data-place="${{stop.alias}}"]`;
      const placeEditors = document.querySelectorAll(placeSelector);
      const placeEditor = placeEditors.length ? placeEditors[placeEditors.length - 1] : null;
      if (placeEditor) stop.place = !Boolean(placeEditor.checked);
    }};
    const setSaveFeedback = (message, failed = false) => {{
      const feedback = document.getElementById("saveFeedback");
      if (!feedback) return;
      feedback.textContent = message;
      feedback.style.color = failed ? "#b91c1c" : "#166534";
    }};
    const saveStopReviews = async (requestedStops = null) => {{
      const editorsToSync = requestedStops || data.stops;
      editorsToSync.forEach((stop) => syncStopFromEditors(stop, Boolean(requestedStops)));
      const stops = requestedStops || changedStops();
      if (!canSaveManualStops()) {{
        clipboardStatus = " · hosted map required";
        setSaveFeedback("Open the hosted map to save directly.", true);
        updateStatus();
        return false;
      }}
      if (!stops.length) {{
        clipboardStatus = " · no changes";
        setSaveFeedback("No unsaved changes.");
        updateStatus();
        return true;
      }}
      const saveButton = document.getElementById("saveChanges");
      if (saveButton) {{
        saveButton.disabled = true;
        saveButton.textContent = "Saving...";
      }}
      setSaveFeedback(`Saving ${{stops.length}} visit${{stops.length === 1 ? "" : "s"}}...`);
      clipboardStatus = ` · saving ${{stops.length}} visit${{stops.length === 1 ? "" : "s"}}...`;
      updateStatus();
      try {{
        for (const stop of stops) {{
          const response = await fetch(owntracksStopsEndpoint(), {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              date: data.date,
              id: stop.id,
              lat: stop.lat,
              lon: stop.lon,
              name: stop.name,
              tags: stop.tags || [],
              note: stop.note || "",
              entry_time: stop.entry_override || "",
              exit_time: stop.exit_override || "",
              radius_m: stop.radius_m || {DEFAULT_VISIT_RADIUS_M},
              place: Boolean(stop.place),
            }}),
          }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
          originalNames.set(stop.alias, stop.name);
          originalTags.set(stop.alias, (stop.tags || []).join(" "));
          originalNotes.set(stop.alias, stop.note || "");
          originalEntryTimes.set(stop.alias, stop.entry_override || "");
          originalExitTimes.set(stop.alias, stop.exit_override || "");
          originalRadii.set(stop.alias, String(stop.radius_m || {DEFAULT_VISIT_RADIUS_M}));
          originalPlaces.set(stop.alias, Boolean(stop.place));
        }}
        clipboardStatus = ` · saved ${{stops.length}} visit${{stops.length === 1 ? "" : "s"}}`;
        setSaveFeedback(`Saved ${{stops.length}} visit${{stops.length === 1 ? "" : "s"}}.`);
        updateCommands(false);
        return true;
      }} catch (error) {{
        clipboardStatus = ` · save failed: ${{error.message || error}}`;
        setSaveFeedback(`Save failed: ${{error.message || error}}`, true);
        updateStatus();
        return false;
      }} finally {{
        if (saveButton) {{
          saveButton.disabled = false;
          saveButton.textContent = "Save visit changes";
        }}
      }}
    }};
    const renderPlaceCandidates = (stop, candidates) => {{
      if (!candidates.length) return '<div>No Overpass candidates found nearby.</div>';
      return candidates.map((candidate, index) => `
        <div style="border-top: 1px solid #e5e7eb; margin-top: 6px; padding-top: 6px">
          <strong>${{escapeHtml(candidate.name || "")}}</strong>
          <div>${{escapeHtml(candidate.category || "place")}} · ${{escapeHtml(candidate.distance_m || 0)}} m · score ${{escapeHtml(candidate.score || 0)}}</div>
          <button type="button" class="secondary" data-use-place-candidate="${{escapeHtml(stop.alias)}}" data-candidate-index="${{index}}">Use suggestion</button>
        </div>
      `).join("");
    }};
    const resolveStopPlace = async (stop, container, button) => {{
      if (!canSaveManualStops()) {{
        if (container) container.innerHTML = "Open the hosted map to resolve and save places.";
        return;
      }}
      if (button) {{
        button.disabled = true;
        button.textContent = "Resolving...";
      }}
      if (container) container.innerHTML = "Querying Overpass...";
      try {{
        const response = await fetch(owntracksPlaceResolveEndpoint(), {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ lat: stop.lat, lon: stop.lon, radius_m: stop.radius_m || {DEFAULT_VISIT_RADIUS_M} }}),
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
        stop.place_candidates = payload.candidates || [];
        if (container) container.innerHTML = renderPlaceCandidates(stop, stop.place_candidates);
      }} catch (error) {{
        if (container) container.innerHTML = `Resolve failed: ${{escapeHtml(error.message || error)}}`;
      }} finally {{
        if (button) {{
          button.disabled = false;
          button.textContent = "Resolve place";
        }}
      }}
    }};
    const usePlaceCandidate = async (stop, candidate, container) => {{
      if (!candidate) return;
      stop.name = candidate.name || stop.name;
      const candidateTags = Array.isArray(candidate.tags) ? candidate.tags : [];
      stop.tags = [...new Set([...(stop.tags || []), ...candidateTags])];
      stop.place = true;
      refreshStop(stop);
      attachPopupHandlers(stop);
      renderList();
      const saved = await saveStopReviews([stop]);
      if (container) container.innerHTML = saved
        ? `Saved place: ${{escapeHtml(candidate.name || "")}}`
        : "Filled suggestion but save failed.";
    }};
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
    const updateCommands = (autoCopy = false) => {{
      const changed = changedStops().filter((stop) =>
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
    const selectedDurationEl = document.getElementById("selectedDuration");
    const rawTrackPoints = data.sampledTrack || [];
    const rawUnfilteredTrackPoints = data.rawSampledTrack || rawTrackPoints;
    const rideSegments = data.rideSegments || [];
    const travelSegments = data.travelSegments || [];
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
        place_name: point.place_name || null,
        poi: point.poi || "",
        imagename: point.imagename || "",
        has_image: Boolean(point.has_image),
        line: point.line || null,
        time: point.time || "",
        maps: point.maps || "",
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
    const registerDurationAnchor = (anchor) => {{
      if (anchor && anchor.key) durationAnchors.set(anchor.key, anchor);
      return anchor;
    }};
    const anchorLabelWithTime = (anchor, field = "entry_timestamp") => {{
      if (!anchor) return "not set";
      const timestamp = Number(anchor[field]);
      return `${{anchor.label}}${{Number.isFinite(timestamp) ? " · " + formatTime(timestamp) : ""}}`;
    }};
    const clearDurationAnchors = () => {{
      durationStartKey = null;
      durationEndKey = null;
      refreshSelectedDuration();
    }};
    const durationAnchorButtons = (anchor) => {{
      if (!anchor || !anchor.key) return "";
      registerDurationAnchor(anchor);
      const anchorMeta = anchor.kind === "visit"
        ? `Visit entry: ${{formatTime(anchor.entry_timestamp)}} · visit exit: ${{formatTime(anchor.exit_timestamp)}}`
        : `Anchor: ${{anchorLabelWithTime(anchor)}}`;
      return `
        <div class="row" style="margin-top: 8px">
          <button type="button" class="secondary" data-duration-start="${{escapeHtml(anchor.key)}}">Set start</button>
          <button type="button" class="secondary" data-duration-end="${{escapeHtml(anchor.key)}}">Set end</button>
        </div>
        <div class="popup-meta">${{escapeHtml(anchorMeta)}}</div>
      `;
    }};
    const setDurationAnchor = (key, role) => {{
      if (!durationAnchors.has(key)) return;
      if (role === "start") durationStartKey = key;
      else durationEndKey = key;
      refreshSelectedDuration();
    }};
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
    const poiEvents = data.poiEvents || [];
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
        ${{durationAnchorButtons(anchorForRoutePoint(point))}}
      </div>
    `;
    const canSaveManualStops = () => window.location.protocol === "http:" || window.location.protocol === "https:";
    const routePointTitle = (point) => {{
      if (point.poi) return `POI: ${{point.poi}}`;
      if (point.place_name) return point.place_name;
      return "Route point";
    }};
    const routePointPopupHtml = (point) => `
      <div class="segment-popup">
        <strong>${{escapeHtml(routePointTitle(point))}}</strong>
        <div class="popup-meta">
          <div>${{formatTime(point.timestamp)}} · Line ${{escapeHtml(point.line || "")}}</div>
          <div>Motion: ${{escapeHtml(point.motion_mode || "unknown")}} · Speed: ${{formatSpeed(point.speed_kmh)}}</div>
          ${{point.imagename ? `<div>Image: ${{escapeHtml(point.imagename)}}</div>` : ""}}
          ${{point.has_image ? `<div>Embedded image available in POI marker</div>` : ""}}
          ${{point.maps ? `<div><a href="${{escapeHtml(point.maps)}}" target="_blank" rel="noreferrer">Google Maps</a></div>` : ""}}
        </div>
        ${{durationAnchorButtons(anchorForRoutePoint(point))}}
        ${{canSaveManualStops() && point.line ? `
          <label>Name</label>
          <input data-manual-stop-name value="${{escapeHtml(point.place_name || "")}}">
          <label>Tags</label>
          <input data-manual-stop-tags placeholder="tags">
          <label>Note</label>
          <textarea data-manual-stop-note placeholder="note"></textarea>
          <label class="checkbox-row">
            <input type="checkbox" data-manual-stop-single-use>
            <span>Single-use visit only</span>
          </label>
          <button type="button" data-mark-manual-stop>Save visit</button>
        ` : '<div class="popup-meta">Open the hosted map to mark this point as a stop.</div>'}}
        <div class="popup-meta" data-manual-stop-status></div>
      </div>
    `;
    const saveManualStop = async (point, popupElement) => {{
      const button = popupElement.querySelector("[data-mark-manual-stop]");
      const result = popupElement.querySelector("[data-manual-stop-status]");
      const name = popupElement.querySelector("[data-manual-stop-name]")?.value.trim() || "";
      const tags = parseTags(popupElement.querySelector("[data-manual-stop-tags]")?.value || "");
      const note = popupElement.querySelector("[data-manual-stop-note]")?.value.trim() || "";
      const place = !Boolean(popupElement.querySelector("[data-manual-stop-single-use]")?.checked);
      if (button) button.disabled = true;
      if (result) result.textContent = "Saving...";
      try {{
        const query = new URLSearchParams(window.location.search);
        const token = query.get("token");
        const endpoint = `/owntracks/stops${{token ? `?token=${{encodeURIComponent(token)}}` : ""}}`;
        const response = await fetch(endpoint, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            date: data.date,
            lat: point.lat,
            lon: point.lon,
            line: point.line,
            timestamp: point.timestamp,
            time: point.time || formatTime(point.timestamp),
            motion_mode: point.motion_mode || "unknown",
            name,
            tags,
            note,
            place,
          }}),
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
        if (result) result.textContent = "Saved. Reloading map...";
        window.location.reload();
      }} catch (error) {{
        if (result) result.textContent = `Could not save: ${{error.message || error}}`;
        if (button) button.disabled = false;
      }}
    }};
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
    const travelSegmentPopupHtml = (segment) => `
      <div class="segment-popup">
        <strong>${{escapeHtml(segment.label || "Travel time")}}</strong>
        <div class="popup-meta">
          <div>Travel time: ${{escapeHtml(segment.duration || formatDuration(segment.duration_seconds))}}</div>
          <div>${{escapeHtml(segment.start_time || "")}} to ${{escapeHtml(segment.end_time || "")}}</div>
          <div>Distance: ${{Number(segment.distance_km || 0).toFixed(2)}} km · Points: ${{escapeHtml(segment.point_count || 0)}}</div>
          <div>${{escapeHtml(segment.start_kind || "")}} to ${{escapeHtml(segment.end_kind || "")}}</div>
        </div>
      </div>
    `;
    const travelLineEndpoints = (segment) => {{
      const startLat = Number(segment.start_lat);
      const startLon = Number(segment.start_lon);
      const endLat = Number(segment.end_lat);
      const endLon = Number(segment.end_lon);
      if (![startLat, startLon, endLat, endLon].every(Number.isFinite)) return null;
      return {{ startLat, startLon, endLat, endLon }};
    }};
    const renderTravelTimes = () => {{
      travelTimeLayer.clearLayers();
      if (!travelTimesVisible) {{
        travelTimeLayer.remove();
        syncTravelTimesButton();
        return;
      }}
      for (const segment of travelSegments) {{
        const endpoints = travelLineEndpoints(segment);
        if (!endpoints) continue;
        const midLat = (endpoints.startLat + endpoints.endLat) / 2;
        const midLon = (endpoints.startLon + endpoints.endLon) / 2;
        L.polyline(
          [[endpoints.startLat, endpoints.startLon], [endpoints.endLat, endpoints.endLon]],
          {{ color: "#111827", weight: 2, opacity: 0.5, dashArray: "4 6", interactive: true }}
        ).bindPopup(travelSegmentPopupHtml(segment)).addTo(travelTimeLayer);
        L.marker([midLat, midLon], {{
          interactive: true,
          icon: L.divIcon({{
            className: "travel-time-marker",
            html: `<div class="travel-time-label">${{escapeHtml(segment.duration || formatDuration(segment.duration_seconds))}}</div>`,
          }}),
        }}).bindPopup(travelSegmentPopupHtml(segment)).addTo(travelTimeLayer);
      }}
      travelTimeLayer.addTo(map);
      syncTravelTimesButton();
    }};
    const syncTravelTimesButton = () => {{
      const button = document.getElementById("toggleTravelTimes");
      if (!button) return;
      button.textContent = travelTimesVisible ? `Hide travel times (${{travelSegments.length}})` : `Show travel times (${{travelSegments.length}})`;
      button.classList.toggle("active", travelTimesVisible);
      button.disabled = !travelSegments.length;
    }};
    const setTravelTimesVisible = (visible) => {{
      travelTimesVisible = visible;
      renderTravelTimes();
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
        const marker = L.circleMarker([point.lat, point.lon], {{
          renderer: routePointRenderer,
          radius: pointRadius,
          color: routeColorFor(point, previous),
          fillColor: routeColorFor(point, previous),
          fillOpacity: 0.9,
          // A transparent stroke provides a reliable touch target without
          // visually enlarging dense GPS samples.
          opacity: 0,
          weight: 10,
        }});
        if (point.place_name) {{
          marker.bindTooltip(escapeHtml(point.place_name), {{
            permanent: placeLabelsVisible && placeLabelsAllowedByZoom(),
            direction: "right",
            className: "place-label",
          }});
        }}
        marker.bindPopup(routePointPopupHtml(point), {{ maxWidth: 320 }});
        marker.on("popupopen", () => {{
          const element = marker.getPopup() && marker.getPopup().getElement();
          if (!element) return;
          L.DomEvent.disableClickPropagation(element);
          const button = element.querySelector("[data-mark-manual-stop]");
          if (button) button.addEventListener("click", () => saveManualStop(point, element), {{ once: true }});
        }});
        marker.addTo(routeLayer);
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
      const scaledProgress = progress * segments.length;
      const currentIndex = progress >= 1
        ? segments.length - 1
        : Math.min(segments.length - 1, Math.floor(scaledProgress));
      const current = segments[currentIndex];
      const segmentProgress = progress >= 1 ? 1 : Math.max(0, Math.min(1, scaledProgress - Math.floor(scaledProgress)));
      const currentPosition = current
        ? [
            interpolateNumber(current.start[0], current.end[0], segmentProgress),
            interpolateNumber(current.start[1], current.end[1], segmentProgress),
          ]
        : null;
      if (currentPosition) {{
        L.circleMarker(currentPosition, {{
          radius: 7,
          color: "#ffffff",
          fillColor: current.color,
          fillOpacity: 1,
          weight: 3,
          interactive: false,
        }}).addTo(animationLayer);
      }}
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
      const segments = routeAnimationSegments();
      if (segments.length) {{
        const routeBounds = L.latLngBounds([
          segments[0].start,
          ...segments.map((segment) => segment.end),
        ]);
        map.fitBounds(routeBounds, {{ padding: [60, 60], maxZoom: routeAnimationMaxZoom, animate: true }});
      }}
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
        placeButton.textContent = placeLabelsVisible ? "Hide transition points" : "Show transition points";
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
    const possibleStops = data.possibleMissedStops || [];
    const syncPossibleStopsButton = () => {{
      const button = document.getElementById("togglePossibleStops");
      if (!button) return;
      button.textContent = possibleStopsVisible ? `Hide possible missed stops (${{possibleStops.length}})` : `Show possible missed stops (${{possibleStops.length}})`;
      button.classList.toggle("active", possibleStopsVisible);
      button.disabled = possibleStops.length === 0;
    }};
    const syncPoisButton = () => {{
      const button = document.getElementById("togglePois");
      if (!button) return;
      button.textContent = poisVisible ? `Hide POIs (${{poiEvents.length}})` : `Show POIs (${{poiEvents.length}})`;
      button.classList.toggle("active", poisVisible);
      button.disabled = poiEvents.length === 0;
    }};
    const poiPoint = (item) => ({{
      line: item.line,
      lat: item.lat,
      lon: item.lon,
      timestamp: item.timestamp,
      time: item.time,
      motion_mode: item.motion_mode || "unknown",
      place_name: item.name || item.poi || "",
      poi: item.name || item.poi || "",
      imagename: item.imagename || "",
      has_image: Boolean(item.image_data_url || item.has_image),
      maps: item.maps || "",
    }});
    const poiPopupHtml = (item) => `
      <div class="segment-popup">
        <strong>POI: ${{escapeHtml(item.name || item.poi || "untitled")}}</strong>
        <div class="popup-meta">
          <div>${{escapeHtml(item.time || formatTime(item.timestamp))}} · Line ${{escapeHtml(item.line || "")}}</div>
          <div>Motion: ${{escapeHtml(item.motion_mode || "unknown")}} · Accuracy: ${{Number.isFinite(Number(item.accuracy_m)) ? Math.round(Number(item.accuracy_m)) + " m" : "unknown"}}</div>
          ${{item.imagename ? `<div>Image: ${{escapeHtml(item.imagename)}}</div>` : ""}}
          ${{item.maps ? `<div><a href="${{escapeHtml(item.maps)}}" target="_blank" rel="noreferrer">Google Maps</a></div>` : ""}}
        </div>
        ${{item.image_data_url ? `<a href="${{escapeHtml(item.image_data_url)}}" target="_blank" rel="noreferrer"><img class="poi-image" src="${{escapeHtml(item.image_data_url)}}" alt="${{escapeHtml(item.imagename || item.name || "POI image")}}"></a>` : ""}}
        ${{routePointPopupHtml(poiPoint(item))}}
      </div>
    `;
    const renderPois = () => {{
      poiLayer.clearLayers();
      if (!poisVisible || !poiEvents.length) {{
        poiLayer.remove();
        syncPoisButton();
        return;
      }}
      poiEvents.forEach((item, index) => {{
        const lat = Number(item.lat);
        const lon = Number(item.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
        const marker = L.marker([lat, lon], {{
          icon: L.divIcon({{
            className: "poi-marker",
            html: `<div class="poi-label">POI${{item.image_data_url ? " + image" : ""}}</div>`,
            iconSize: [74, 24],
            iconAnchor: [10, 20],
          }}),
        }}).bindTooltip(escapeHtml(item.name || item.poi || `POI ${{index + 1}}`), {{
          permanent: true,
          direction: "right",
          className: "poi-label",
        }}).bindPopup(poiPopupHtml(item), {{ maxWidth: 360 }});
        marker.on("popupopen", () => {{
          const element = marker.getPopup() && marker.getPopup().getElement();
          if (!element) return;
          L.DomEvent.disableClickPropagation(element);
          const button = element.querySelector("[data-mark-manual-stop]");
          if (button) button.addEventListener("click", () => saveManualStop(poiPoint(item), element), {{ once: true }});
        }});
        marker.addTo(poiLayer);
      }});
      poiLayer.addTo(map);
      syncPoisButton();
    }};
    const setPoisVisible = (visible) => {{
      poisVisible = visible;
      renderPois();
    }};
    const possibleStopPoint = (item) => ({{
      line: item.line,
      lat: item.lat,
      lon: item.lon,
      timestamp: item.timestamp,
      time: item.time,
      motion_mode: item.motion_mode || "unknown",
      place_name: item.name || "",
      possible_stop: true,
    }});
    const possibleStopPopupHtml = (item) => `
      <div class="segment-popup">
        <strong>Possible missed stop</strong>
        <div class="popup-meta">
          <div>${{escapeHtml(item.time || "unknown")}} · Line ${{escapeHtml(item.line || "")}}</div>
          <div>${{escapeHtml(item.reason || "Sparse buffered track point")}}</div>
          <div>Motion: ${{escapeHtml(item.motion_mode || "unknown")}} · Accuracy: ${{Number.isFinite(Number(item.accuracy_m)) ? Math.round(Number(item.accuracy_m)) + " m" : "unknown"}}</div>
          <div>Previous gap: ${{Number.isFinite(Number(item.previous_gap_minutes)) ? Math.round(Number(item.previous_gap_minutes)) + " min" : "unknown"}} · Next gap: ${{Number.isFinite(Number(item.next_gap_minutes)) ? Math.round(Number(item.next_gap_minutes)) + " min" : "unknown"}}</div>
          ${{item.maps ? `<div><a href="${{escapeHtml(item.maps)}}" target="_blank" rel="noreferrer">Google Maps</a></div>` : ""}}
        </div>
        ${{routePointPopupHtml(possibleStopPoint(item))}}
      </div>
    `;
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
    const renderPossibleStops = () => {{
      possibleStopLayer.clearLayers();
      const list = document.getElementById("possibleStopList");
      if (list) {{
        list.style.display = possibleStopsVisible && possibleStops.length ? "block" : "none";
        list.innerHTML = possibleStopsVisible ? possibleStops.map((item, index) => `
          <div class="stop-row">
            <div class="stop-head">
              <span class="alias">p${{index + 1}}</span>
              <button type="button" class="secondary" data-possible-stop="${{index}}">Open</button>
              ${{item.maps ? `<a href="${{escapeHtml(item.maps)}}" target="_blank" rel="noreferrer">Google Maps</a>` : ""}}
            </div>
            <div class="meta">${{escapeHtml(item.time || "unknown")}} · line ${{escapeHtml(item.line || "")}} · ${{escapeHtml(item.confidence || "low")}} confidence</div>
            <div class="meta">${{escapeHtml(item.reason || "Sparse buffered track point")}}</div>
          </div>
        `).join("") : "";
        list.querySelectorAll("[data-possible-stop]").forEach((button) => {{
          button.addEventListener("click", () => {{
            const item = possibleStops[Number(button.dataset.possibleStop)];
            if (!item) return;
            possibleStopsVisible = true;
            renderPossibleStops();
            map.setView([item.lat, item.lon], zoomAtLeast(16), {{ animate: true }});
          }});
        }});
      }}
      if (!possibleStopsVisible || !possibleStops.length) {{
        possibleStopLayer.remove();
        syncPossibleStopsButton();
        return;
      }}
      possibleStops.forEach((item, index) => {{
        const lat = Number(item.lat);
        const lon = Number(item.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
        const marker = L.circleMarker([lat, lon], {{
          radius: 8,
          color: "#92400e",
          fillColor: "#f59e0b",
          fillOpacity: 0.9,
          opacity: 1,
          weight: 2,
          dashArray: "4 3",
        }}).bindTooltip(`p${{index + 1}}`, {{
          permanent: true,
          direction: "top",
          className: "possible-stop-label",
        }}).bindPopup(possibleStopPopupHtml(item), {{ maxWidth: 340 }});
        marker.on("popupopen", () => {{
          const element = marker.getPopup() && marker.getPopup().getElement();
          if (!element) return;
          L.DomEvent.disableClickPropagation(element);
          const button = element.querySelector("[data-mark-manual-stop]");
          if (button) button.addEventListener("click", () => saveManualStop(possibleStopPoint(item), element), {{ once: true }});
        }});
        marker.addTo(possibleStopLayer);
      }});
      possibleStopLayer.addTo(map);
      syncPossibleStopsButton();
    }};
    const setPossibleStopsVisible = (visible) => {{
      possibleStopsVisible = visible;
      renderPossibleStops();
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
      if (placeLabelsVisible) placeLayer.addTo(map);
      else placeLayer.remove();
      drawRoute();
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
    const stopReference = (stop) => {{
      const lines = stop.start_line && stop.end_line ? `lines ${{stop.start_line}}-${{stop.end_line}}` : "lines unknown";
      return `${{data.date}} ${{stop.alias}} ${{stop.id || "unknown-id"}} ${{lines}}`;
    }};
    const stopAnchorKey = (stop) => `stop:${{stop.alias}}`;
    const placeAnchorKey = (place, index) => `place:${{place.id || index}}`;
    const anchorForStop = (stop) => ({{
      key: stopAnchorKey(stop),
      label: labelFor(stop),
      kind: "visit",
      entry_timestamp: Number(stop.visit_start_timestamp || stop.start_timestamp),
      exit_timestamp: Number(stop.visit_end_timestamp || stop.end_timestamp),
      lat: Number(stop.lat),
      lon: Number(stop.lon),
    }});
    const routePointAnchorKey = (point) => `route:${{point.line || point.timestamp || `${{point.lat}},${{point.lon}}`}}`;
    const anchorForRoutePoint = (point) => ({{
      key: routePointAnchorKey(point),
      label: point.place_name || `Route point${{point.line ? " line " + point.line : ""}}`,
      kind: "route",
      entry_timestamp: Number(point.timestamp),
      exit_timestamp: Number(point.timestamp),
      lat: Number(point.lat),
      lon: Number(point.lon),
    }});
    const anchorForPlace = (place, index) => {{
      const label = `${{place.action || ""}} ${{place.name || "place"}}`.trim();
      return {{
        key: placeAnchorKey(place, index),
        label,
        kind: "place",
        entry_timestamp: Number(place.timestamp),
        exit_timestamp: Number(place.timestamp),
        lat: Number(place.lat),
        lon: Number(place.lon),
      }};
    }};
    const selectedAnchorList = () => [...selectedAnchors.values()].sort((left, right) => Number(left.entry_timestamp) - Number(right.entry_timestamp));
    const refreshSelectedDuration = () => {{
      if (!selectedDurationEl) return;
      if (durationStartKey || durationEndKey) {{
        const first = durationAnchors.get(durationStartKey);
        const second = durationAnchors.get(durationEndKey);
        if (!first || !second) {{
          selectedDurationEl.innerHTML = `
            <strong>Selected duration</strong>
            <span>Start: ${{escapeHtml(anchorLabelWithTime(first, "entry_timestamp"))}}</span><br>
            <span>End: ${{escapeHtml(anchorLabelWithTime(second, "entry_timestamp"))}}</span><br>
            <button type="button" class="secondary" data-duration-clear>Clear</button>
          `;
          return;
        }}
        const selectedStart = Number(first.entry_timestamp);
        const selectedEnd = Number(second.entry_timestamp);
        const start = Math.min(selectedStart, selectedEnd);
        const end = Math.max(selectedStart, selectedEnd);
        if (!Number.isFinite(selectedStart) || !Number.isFinite(selectedEnd)) {{
          selectedDurationEl.innerHTML = `
            <strong>${{escapeHtml(anchorLabelWithTime(first, "entry_timestamp"))}} to ${{escapeHtml(anchorLabelWithTime(second, "entry_timestamp"))}}</strong>
            <span>Duration unavailable for one of these selected points.</span><br>
            <button type="button" class="secondary" data-duration-clear>Clear</button>
          `;
          return;
        }}
        const seconds = end - start;
        const pointsBetween = rawUnfilteredTrackPoints.filter((point) => {{
          const timestamp = Number(point.timestamp);
          return Number.isFinite(timestamp) && timestamp >= start && timestamp <= end;
        }});
        let distanceKm = 0;
        for (let index = 1; index < pointsBetween.length; index += 1) {{
          distanceKm += distanceMeters(pointsBetween[index - 1], pointsBetween[index]) / 1000;
        }}
        const reversed = selectedEnd < selectedStart;
        selectedDurationEl.innerHTML = `
          <strong>${{escapeHtml(anchorLabelWithTime(first, "entry_timestamp"))}} to ${{escapeHtml(anchorLabelWithTime(second, "entry_timestamp"))}}</strong>
          <span>${{formatDuration(seconds)}} elapsed · ${{distanceKm.toFixed(2)}} km raw track · ${{pointsBetween.length}} point${{pointsBetween.length === 1 ? "" : "s"}}</span><br>
          ${{reversed ? '<span>Selected order was later-to-earlier; showing chronological span.</span><br>' : ""}}
          <button type="button" class="secondary" data-duration-clear>Clear</button>
        `;
        return;
      }}
      selectedDurationEl.innerHTML = `Open a point popup and use Set start / Set end to calculate elapsed time.`;
    }};
    const stopTravelLine = (label, segment, placeField) => {{
      if (!segment) return "";
      return `<div>${{label}}: ${{escapeHtml(segment[placeField] || "")}}, ${{escapeHtml(segment.duration || formatDuration(segment.duration_seconds))}}</div>`;
    }};
    const stopTravelHtml = (stop) => {{
      const lines = [
        stopTravelLine("From previous", stop.previous_travel, "start_name"),
        stopTravelLine("To next", stop.next_travel, "end_name"),
      ].filter(Boolean);
      return lines.length ? `<div class="popup-meta" style="margin-top: 6px">${{lines.join("")}}</div>` : "";
    }};
    const visitTimingHtml = (stop) => {{
      const entry = stop.entry_display || stop.start || "";
      const exit = stop.exit_display || stop.end || "";
      return `
        <div class="popup-meta">
          <div>${{escapeHtml(entry)}} -> ${{escapeHtml(exit)}}</div>
          <div>${{escapeHtml(stop.visit_duration || stop.duration || "")}} · ${{escapeHtml(stop.points || 0)}} sample${{Number(stop.points) === 1 ? "" : "s"}}</div>
        </div>
      `;
    }};
    const popupFor = (stop) => `
      <div class="stop-popup">
        <strong>${{escapeHtml(labelFor(stop))}}</strong>
        ${{visitTimingHtml(stop)}}
        <div class="popup-meta"><a href="${{escapeHtml(stop.maps)}}" target="_blank" rel="noreferrer">Google Maps</a></div>
        <label for="popup-name-${{escapeHtml(stop.alias)}}">Name</label>
        <input id="popup-name-${{escapeHtml(stop.alias)}}" data-popup-name="${{escapeHtml(stop.alias)}}" value="${{escapeHtml(stop.name)}}">
        <label for="popup-tags-${{escapeHtml(stop.alias)}}">Tags</label>
        <input id="popup-tags-${{escapeHtml(stop.alias)}}" data-popup-tags="${{escapeHtml(stop.alias)}}" value="${{escapeHtml((stop.tags || []).join(" "))}}" placeholder="tags">
        <label for="popup-note-${{escapeHtml(stop.alias)}}">Note</label>
        <textarea id="popup-note-${{escapeHtml(stop.alias)}}" data-popup-note="${{escapeHtml(stop.alias)}}" placeholder="note">${{escapeHtml(stop.note || "")}}</textarea>
        <label for="popup-entry-${{escapeHtml(stop.alias)}}">Entry override</label>
        <input id="popup-entry-${{escapeHtml(stop.alias)}}" data-popup-entry="${{escapeHtml(stop.alias)}}" placeholder="HH:MM or YYYY-MM-DDTHH:MM" value="${{escapeHtml(stop.entry_override || "")}}">
        <label for="popup-exit-${{escapeHtml(stop.alias)}}">Exit override</label>
        <input id="popup-exit-${{escapeHtml(stop.alias)}}" data-popup-exit="${{escapeHtml(stop.alias)}}" placeholder="HH:MM or YYYY-MM-DDTHH:MM" value="${{escapeHtml(stop.exit_override || "")}}">
        <label for="popup-radius-${{escapeHtml(stop.alias)}}">Grouping radius, meters</label>
        <input id="popup-radius-${{escapeHtml(stop.alias)}}" data-popup-radius="${{escapeHtml(stop.alias)}}" type="number" min="10" max="5000" step="10" value="${{escapeHtml(stop.radius_m || {DEFAULT_VISIT_RADIUS_M})}}">
        <label class="checkbox-row">
          <input type="checkbox" data-popup-place="${{escapeHtml(stop.alias)}}" ${{stop.place ? "" : "checked"}}>
          <span>Single-use visit only</span>
        </label>
        ${{renderMediaGallery(stop)}}
        ${{canSaveManualStops() ? renderMediaUpload(stop, "popup") : ""}}
        ${{canSaveManualStops() ? `
          <div class="row" style="margin-top: 8px">
            <button type="button" class="secondary" data-resolve-stop="${{escapeHtml(stop.alias)}}">Resolve place</button>
          </div>
          <div class="popup-meta" data-place-resolve-results="${{escapeHtml(stop.alias)}}"></div>
          <div class="row" style="margin-top: 8px">
            <button type="button" data-save-stop="${{escapeHtml(stop.alias)}}">Save changes</button>
            <button type="button" class="secondary" data-dismiss-stop="${{escapeHtml(stop.alias)}}">Dismiss visit</button>
          </div>
        ` : ""}}
        <div class="popup-meta" data-save-stop-feedback></div>
      </div>
    `;
    const dismissStop = async (stop, element) => {{
      if (!window.confirm(`Dismiss ${{stop.alias}} as a visit? The route samples stay on the map.`)) return;
      const feedback = element.querySelector("[data-save-stop-feedback]");
      const button = element.querySelector("[data-dismiss-stop]");
      if (button) {{
        button.disabled = true;
        button.textContent = "Dismissing...";
      }}
      if (feedback) feedback.textContent = "Dismissing visit...";
      try {{
        const response = await fetch(owntracksStopsEndpoint(), {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            date: data.date,
            id: stop.id,
            lat: stop.lat,
            lon: stop.lon,
            ignored: true,
          }}),
        }});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${{response.status}}`);
        if (feedback) feedback.textContent = "Dismissed. Reloading map...";
        window.location.reload();
      }} catch (error) {{
        if (feedback) feedback.textContent = `Dismiss failed: ${{error.message || error}}`;
        if (button) {{
          button.disabled = false;
          button.textContent = "Dismiss visit";
        }}
      }}
    }};
    const attachPopupHandlers = (stop) => {{
      const marker = markers.get(stop.alias);
      const popup = marker && marker.getPopup ? marker.getPopup() : null;
      const element = popup && popup.getElement ? popup.getElement() : null;
      if (!element) return;
      L.DomEvent.disableClickPropagation(element);
      const saveButton = element.querySelector("[data-save-stop]");
      if (saveButton) saveButton.addEventListener("click", async () => {{
        const feedback = element.querySelector("[data-save-stop-feedback]");
        saveButton.disabled = true;
        saveButton.textContent = "Saving...";
        if (feedback) feedback.textContent = "Saving...";
        const saved = await saveStopReviews([stop]);
        if (feedback) feedback.textContent = saved ? "Saved." : "Save failed; see Tools status.";
        saveButton.disabled = false;
        saveButton.textContent = "Save changes";
      }});
      const dismissButton = element.querySelector("[data-dismiss-stop]");
      if (dismissButton) dismissButton.addEventListener("click", () => dismissStop(stop, element));
      const resolveButton = element.querySelector("[data-resolve-stop]");
      if (resolveButton) resolveButton.addEventListener("click", () => {{
        const container = element.querySelector(`[data-place-resolve-results="${{CSS.escape(stop.alias)}}"]`);
        resolveStopPlace(stop, container, resolveButton);
      }});
      element.querySelectorAll("[data-use-place-candidate]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const targetStop = data.stops.find((item) => item.alias === button.dataset.usePlaceCandidate);
          if (!targetStop) return;
          const candidate = (targetStop.place_candidates || [])[Number(button.dataset.candidateIndex)];
          const container = element.querySelector(`[data-place-resolve-results="${{CSS.escape(targetStop.alias)}}"]`);
          usePlaceCandidate(targetStop, candidate, container);
        }});
      }});
      element.querySelectorAll("[data-upload-media]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const targetStop = data.stops.find((item) => item.alias === button.dataset.uploadMedia);
          if (!targetStop) return;
          button.disabled = true;
          button.textContent = "Uploading...";
          const feedback = element.querySelector(`[data-media-status="${{CSS.escape(button.dataset.mediaKey || "")}}"]`);
          try {{
            await uploadStopMedia(targetStop, button.dataset.mediaKey || "");
          }} catch (error) {{
            if (feedback) feedback.textContent = `Upload failed: ${{error.message || error}}`;
          }} finally {{
            button.disabled = false;
            button.textContent = "Attach media";
          }}
        }});
      }});
      element.querySelectorAll("[data-delete-media]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const targetStop = data.stops.find((item) => item.alias === button.dataset.mediaStop);
          if (!targetStop || !window.confirm("Delete this attachment?")) return;
          button.disabled = true;
          try {{
            await deleteStopMedia(targetStop, button.dataset.deleteMedia || "");
          }} catch (error) {{
            button.textContent = "Failed";
            button.title = String(error.message || error);
            button.disabled = false;
          }}
        }});
      }});
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
      const alias = target.dataset.popupName || target.dataset.popupTags || target.dataset.popupNote || target.dataset.popupEntry || target.dataset.popupExit || target.dataset.popupRadius || target.dataset.popupPlace;
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
      }} else if (target.dataset.popupEntry) {{
        stop.entry_override = target.value.trim();
      }} else if (target.dataset.popupExit) {{
        stop.exit_override = target.value.trim();
      }} else if (target.dataset.popupRadius) {{
        stop.radius_m = Number(target.value) || {DEFAULT_VISIT_RADIUS_M};
      }} else if (target.dataset.popupPlace) {{
        stop.place = !Boolean(target.checked);
      }}
      updateCommands();
    }};
    const handlePopupEditEvent = (event) => {{
      const target = event.target;
      if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) return;
      if (!target.matches("[data-popup-name], [data-popup-tags], [data-popup-note], [data-popup-entry], [data-popup-exit], [data-popup-radius], [data-popup-place]")) return;
      applyPopupEdit(target);
    }};
    document.addEventListener("click", (event) => {{
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const startKey = target.dataset.durationStart;
      const endKey = target.dataset.durationEnd;
      if (target.dataset.durationClear != null) clearDurationAnchors();
      if (startKey) setDurationAnchor(startKey, "start");
      if (endKey) setDurationAnchor(endKey, "end");
      const candidateAlias = target.dataset.usePlaceCandidate;
      if (candidateAlias) {{
        const stop = data.stops.find((item) => item.alias === candidateAlias);
        if (!stop) return;
        const candidate = (stop.place_candidates || [])[Number(target.dataset.candidateIndex)];
        const popup = target.closest(".stop-popup");
        const container = popup ? popup.querySelector(`[data-place-resolve-results="${{CSS.escape(stop.alias)}}"]`) : null;
        usePlaceCandidate(stop, candidate, container);
      }}
    }});
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
          <div class="meta">${{escapeHtml(stop.entry_display || stop.start)}} -> ${{escapeHtml(stop.exit_display || stop.end)}} · ${{escapeHtml(stop.visit_duration || stop.duration)}} · ${{escapeHtml(stop.visit_source || "detected-stop")}} · ${{escapeHtml(stop.confidence || "unknown")}}</div>
          <div class="meta">${{escapeHtml(stop.points || 0)}} raw sample${{Number(stop.points) === 1 ? "" : "s"}} · entry ${{escapeHtml(stop.entry_status || "sample")}} · exit ${{escapeHtml(stop.exit_status || "sample")}}</div>
          <input data-name="${{escapeHtml(stop.alias)}}" value="${{escapeHtml(stop.name)}}">
          <input data-tags="${{escapeHtml(stop.alias)}}" value="${{escapeHtml((stop.tags || []).join(" "))}}" placeholder="tags">
          <textarea data-note="${{escapeHtml(stop.alias)}}" placeholder="note">${{escapeHtml(stop.note || "")}}</textarea>
          <div class="row">
            <input data-entry="${{escapeHtml(stop.alias)}}" placeholder="Entry HH:MM" value="${{escapeHtml(stop.entry_override || "")}}">
            <input data-exit="${{escapeHtml(stop.alias)}}" placeholder="Exit HH:MM" value="${{escapeHtml(stop.exit_override || "")}}">
          </div>
          <div class="row">
            <input data-radius="${{escapeHtml(stop.alias)}}" type="number" min="10" max="5000" step="10" value="${{escapeHtml(stop.radius_m || {DEFAULT_VISIT_RADIUS_M})}}">
            <label class="inline-check"><input data-place="${{escapeHtml(stop.alias)}}" type="checkbox" ${{stop.place ? "" : "checked"}}> Single-use only</label>
          </div>
          ${{renderMediaGallery(stop)}}
          ${{canSaveManualStops() ? renderMediaUpload(stop, "list") : ""}}
          <div class="meta">${{escapeHtml(stopReference(stop))}}</div>
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
      stopList.querySelectorAll("[data-entry]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.entry);
          if (!stop) return;
          stop.entry_override = input.value.trim();
          updateCommands();
        }});
      }});
      stopList.querySelectorAll("[data-exit]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.exit);
          if (!stop) return;
          stop.exit_override = input.value.trim();
          updateCommands();
        }});
      }});
      stopList.querySelectorAll("[data-radius]").forEach((input) => {{
        input.addEventListener("input", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.radius);
          if (!stop) return;
          stop.radius_m = Number(input.value) || {DEFAULT_VISIT_RADIUS_M};
          updateCommands();
        }});
      }});
      stopList.querySelectorAll("[data-place]").forEach((input) => {{
        input.addEventListener("change", () => {{
          const stop = data.stops.find((item) => item.alias === input.dataset.place);
          if (!stop) return;
          stop.place = !Boolean(input.checked);
          updateCommands();
        }});
      }});
      stopList.querySelectorAll("[data-upload-media]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const stop = data.stops.find((item) => item.alias === button.dataset.uploadMedia);
          if (!stop) return;
          button.disabled = true;
          button.textContent = "Uploading...";
          try {{
            await uploadStopMedia(stop, button.dataset.mediaKey || "");
          }} catch (error) {{
            const statusEl = document.querySelector(`[data-media-status="${{CSS.escape(button.dataset.mediaKey || "")}}"]`);
            if (statusEl) statusEl.textContent = `Upload failed: ${{error.message || error}}`;
          }} finally {{
            button.disabled = false;
            button.textContent = "Attach media";
          }}
        }});
      }});
      stopList.querySelectorAll("[data-delete-media]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const stop = data.stops.find((item) => item.alias === button.dataset.mediaStop);
          if (!stop || !window.confirm("Delete this attachment?")) return;
          button.disabled = true;
          try {{
            await deleteStopMedia(stop, button.dataset.deleteMedia || "");
          }} catch (error) {{
            button.textContent = "Failed";
            button.title = String(error.message || error);
            button.disabled = false;
          }}
        }});
      }});
    }};
    const refreshSelectedStops = () => {{
      for (const stop of data.stops) {{
        if (selected.has(stop.alias)) selectedAnchors.set(stopAnchorKey(stop), anchorForStop(stop));
        else selectedAnchors.delete(stopAnchorKey(stop));
      }}
      for (const stop of data.stops) refreshStop(stop);
      renderList();
      updateCommands();
      refreshSelectedDuration();
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
      if (isSelected) {{
        selected.add(alias);
        selectedAnchors.set(stopAnchorKey(stop), anchorForStop(stop));
      }} else {{
        selected.delete(alias);
        selectedAnchors.delete(stopAnchorKey(stop));
      }}
      refreshStop(stop);
      renderList();
      updateCommands();
      refreshSelectedDuration();
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
    poiEvents.forEach((item) => addFitPoint(item.lat, item.lon));
    data.namedPlaces.forEach((place, index) => {{
      const label = `${{place.action || ""}} ${{place.name}}`.trim();
      const placeAnchor = anchorForPlace(place, index);
      const marker = L.circleMarker([place.lat, place.lon], {{ radius: placeMarkerRadius(), color: "#2563eb", fillColor: "#2563eb", fillOpacity: 1, weight: 2 }}).addTo(placeLayer);
      placeMarkers.push(marker);
      marker.bindTooltip(escapeHtml(label), {{ permanent: true, direction: "right", className: "place-label" }});
      marker.bindPopup(`
        <div class="segment-popup">
          <strong>${{escapeHtml(label)}}</strong>
          <div class="popup-meta">${{escapeHtml(place.time)}}</div>
          ${{durationAnchorButtons(placeAnchor)}}
        </div>
      `);
      marker.on("click", () => {{
        const anchor = placeAnchor;
        if (selectedAnchors.has(anchor.key)) selectedAnchors.delete(anchor.key);
        else selectedAnchors.set(anchor.key, anchor);
        refreshSelectedDuration();
      }});
    }});
    for (const stop of data.stops) {{
      const marker = L.marker([stop.lat, stop.lon], {{ icon: iconFor(stop) }}).addTo(map);
      markers.set(stop.alias, marker);
      marker.bindTooltip(escapeHtml(shortLabelFor(stop)), {{ permanent: true, direction: "top", offset: [0, -12], className: "stop-label" }});
      marker.bindPopup(popupFor(stop), {{ className: "stop-popup-shell", maxWidth: 320 }});
      marker.on("click", () => {{
        toggleStop(stop);
        marker.openPopup();
        // Selection refresh replaces the popup DOM, so attach to the final button.
        attachPopupHandlers(stop);
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
      selectedAnchors.clear();
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
    document.getElementById("toggleTravelTimes").addEventListener("click", () => {{
      setTravelTimesVisible(!travelTimesVisible);
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
    document.getElementById("togglePois").addEventListener("click", () => {{
      setPoisVisible(!poisVisible);
    }});
    document.getElementById("togglePossibleStops").addEventListener("click", () => {{
      setPossibleStopsVisible(!possibleStopsVisible);
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
      renderPossibleStops();
      refreshZoomSensitiveMarkers();
      if (!routeAnimationRunning && routeAnimationElapsedMs > 0) renderRouteAnimation(routeAnimationElapsedMs);
    }});
    syncEdgeButton();
    syncArrowButton();
    syncTravelTimesButton();
    syncDayNavigationButtons();
    syncLabelButtons();
    syncPoisButton();
    syncFilteredPointsButton();
    syncPossibleStopsButton();
    applyLabelVisibility();
    syncRouteColorButtons();
    syncProfileAxisButtons();
    syncRouteAnimationButton();
    routeAnimationStatus("ready");
    renderRouteLegend();
    setActiveMotionMode("all");
    setEdgesVisible(true);
    setArrowsVisible(true);
    setTravelTimesVisible(false);
    setPoisVisible(true);
    setFilteredPointsVisible(false);
    setPossibleStopsVisible(false);
    renderElevationProfile();
    document.getElementById("selectNearby").addEventListener("click", () => {{
      const picked = selectedStops()[0] || data.stops[0];
      const threshold = Number(document.getElementById("groupDistance").value) || 150;
      if (!picked) return;
      selected.clear();
      selectedAnchors.clear();
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
    document.getElementById("saveChanges").addEventListener("click", () => saveStopReviews());
    document.getElementById("saveChanges").disabled = !canSaveManualStops();
    document.getElementById("commandsLabel").textContent = canSaveManualStops()
      ? "Telegram fallback"
      : "Paste this in Telegram";
    document.getElementById("fitAll").addEventListener("click", () => {{
      if (fitPoints.length === 1) {{
        map.setView(fitPoints[0], zoomAtLeast(14));
      }} else if (fitPoints.length) {{
        map.fitBounds(L.latLngBounds(fitPoints).pad(0.18));
      }}
    }});
    map.on("zoomend moveend", updateStatus);
    [osmTiles, satelliteTiles].forEach((tileLayer) => {{
      tileLayer.on("tileloadstart tileload tileerror", updateStatus);
    }});
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
    covered_place_names = {str(place.get("name") or "").casefold() for place in places}
    places.extend(proximity_named_place_events(events, target_date, user_tags or {}, covered_place_names))
    stops = candidate_stops(window_events)
    home_anchor_points = home_anchors(events, home_filter)
    stops.extend(boundary_home_stops(window_events, stops, home_filter, home_anchor_points))
    detected_stop_ids = {stop["id"] for stop in stops}
    stops.extend(
        stop
        for stop in manual_stops_for_date(user_tags or {}, target_date.isoformat())
        if stop["id"] not in detected_stop_ids
    )
    stop_overrides = ((user_tags or {}).get(target_date.isoformat(), {}) or {}).get("stops", {})
    stops = [stop for stop in stops if not stop_is_ignored(stop, stop_overrides)]
    stops.sort(key=lambda stop: (int(stop.get("start_line") or 0), int(stop.get("end_line") or 0)))
    annotate_visit_boundaries(stops, track_points)
    missed_stop_suggestions = possible_missed_stops(window_events, stops)
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
        "possible_missed_stops": missed_stop_suggestions,
        "poi_events": poi_event_dicts(window_events),
        "raw_sampled_track": point_dicts(track_points, events),
        "sampled_track": point_dicts(visual_track_points, events),
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
    plan = apply_user_tags(plan, user_tags or {}, events)
    for index, stop in enumerate(plan["candidate_stops"], start=1):
        stop["alias"] = f"s{index}"
    apply_stop_labels_to_point_dicts(plan)
    plan["travel_segments"] = build_stop_travel_segments(track_points, plan["candidate_stops"], places)
    attach_stop_travel_context(plan["candidate_stops"], plan["travel_segments"])
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
