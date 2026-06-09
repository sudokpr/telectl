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


@dataclass(frozen=True)
class OwnTracksScope:
    kind: str
    value: str
    start_date: date
    end_date: date


def as_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def point_dict(event: Event) -> dict:
    return {
        "line": event.line_no,
        "time": fmt_dt(event_time(event)),
        "lat": event.lat,
        "lon": event.lon,
        "motion": event.motion,
        "speed_kmh": event.speed_kmh,
        "accuracy_m": event.payload.get("acc"),
        "battery": event.payload.get("batt"),
        "regions": event.payload.get("inregions") or [],
        "maps": maps_url(event.lat, event.lon) if event.lat is not None and event.lon is not None else None,
    }


def is_moving_ride_point(event: Event) -> bool:
    if not event.is_location:
        return False
    motion = set(event.motion)
    speed = event.speed_kmh or 0
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
                "lat": lat,
                "lon": lon,
                "line": event.line_no,
                "tags": [f"place:{slug(desc)}", f"geofence:{event.payload.get('event')}"],
                "maps": maps_url(lat, lon) if lat is not None and lon is not None else None,
            }
        )
    return places


def candidate_stops(events: list[Event], min_minutes: int = 10, radius_m: int = 180) -> list[dict]:
    low_motion = [
        event
        for event in events
        if event.is_location
        and "Home" not in (event.payload.get("inregions") or [])
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
        if dist_m <= radius_m and dt_gap <= 45 * 60:
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
        name = regions.most_common(1)[0][0] if regions else f"unnamed-stop-{index}"
        stop_id = f"{slug(name)}-{cluster[0].line_no}"
        stops.append(
            {
                "id": stop_id,
                "name": name,
                "start": fmt_dt(start),
                "end": fmt_dt(end),
                "duration_minutes": duration_minutes,
                "duration": fmt_duration(duration_minutes),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "points": len(cluster),
                "motion": ", ".join(f"{name}:{count}" for name, count in motions.most_common()) or "unknown",
                "tags": [f"stop:{stop_id}", "candidate:stop"],
                "maps": maps_url(lat, lon),
            }
        )
    return stops


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
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def render_map_html(plan: dict, tile_cache_dir: Path | None = None) -> str:
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
    for index, point in enumerate(track):
        xy = project_point(point[0], point[1])
        if xy is None:
            continue
        route_parts.append(f"{'L' if index else 'M'} {xy[0]:.1f} {xy[1]:.1f}")
    fallback_route = " ".join(route_parts)
    tile_images = []
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
                if href is None:
                    href = f"https://tile.openstreetmap.org/{tile_zoom}/{tile_x}/{tile_y}.png"
                tile_images.append(
                    f'<image class="tile" href="{href}" x="{min(xy1[0], xy2[0]):.1f}" y="{min(xy1[1], xy2[1]):.1f}" '
                    f'width="{abs(xy2[0] - xy1[0]):.1f}" height="{abs(xy2[1] - xy1[1]):.1f}" preserveAspectRatio="none"></image>'
                )
    embedded_tile_images = bool(tile_images) and all("data:image/png;base64," in image for image in tile_images)
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
        {"date": plan["date"], "track": track, "stops": stops, "namedPlaces": named_places},
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
    .route {{
      fill: none;
      stroke: #2563eb;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 4;
    }}
    .tile {{
      image-rendering: auto;
    }}
    .place {{
      fill: #2563eb;
      stroke: white;
      stroke-width: 3;
    }}
    .stop {{
      cursor: pointer;
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
          <path id="route" class="route" d="{fallback_route}"/>
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
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({{
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
    const centerStop = (alias) => {{
      const stop = data.stops.find((item) => item.alias === alias);
      if (!stop) return;
      const [x, y] = project(stop.lat, stop.lon);
      viewBox[0] = x - viewBox[2] / 2;
      viewBox[1] = y - viewBox[3] / 2;
      setViewBox();
    }};
    const updateCommands = () => {{
      const changed = data.stops.filter((stop) => stop.name !== originalNames.get(stop.alias));
      commands.value = changed.length
        ? ["/otb " + data.date, ...changed.map((stop) => `${{stop.alias}} ${{stop.name}}`)].join("\\n")
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
        stopsLayer.insertAdjacentHTML("beforeend", `
          <g class="stop ${{selected.has(stop.alias) ? "selected" : ""}}" data-alias="${{escapeHtml(stop.alias)}}" transform="translate(${{drawX}}, ${{drawY}})">
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
      }}
      stopTapStart = null;
    }});
    stopsLayer.addEventListener("pointercancel", () => {{
      stopTapStart = null;
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


def build_heatmap_summary(events: list[Event], scope: OwnTracksScope, user_tags: dict | None = None) -> dict:
    scope_events = [event for event in events if (event_date(event) is not None and scope.start_date <= event_date(event) <= scope.end_date)]
    location_points = [event for event in scope_events if event.is_location]
    day_points: dict[date, list[Event]] = {}
    buckets: Counter[tuple[float, float]] = Counter()
    for event in location_points:
        if event.lat is None or event.lon is None:
            continue
        day = event_date(event)
        if day is not None:
            day_points.setdefault(day, []).append(event)
        bucket = (round(event.lat, 4), round(event.lon, 4))
        buckets[bucket] += 1

    heat_points: list[dict] = []
    hotspots: list[dict] = []
    for (lat, lon), count in sorted(buckets.items(), key=lambda item: (-item[1], item[0])):
        label = location_override_for({"lat": lat, "lon": lon}, user_tags or {}, scope.end_date.isoformat()).get("name")
        display_label = label or f"{lat:.4f}, {lon:.4f}"
        heat_points.append({"lat": round(lat, 6), "lon": round(lon, 6), "weight": count})
        hotspots.append({"lat": round(lat, 6), "lon": round(lon, 6), "count": count, "label": display_label})

    least_visited = sorted(hotspots, key=lambda item: (item["count"], item["label"]))[:10]
    most_visited = hotspots[:10]
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
            "unique_locations": len(buckets),
            "max_visits": max(buckets.values()) if buckets else 0,
            "min_visits": min(buckets.values()) if buckets else 0,
            "sampled_distance_km": total_distance_km,
        },
        "heat_points": heat_points,
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
      <div class="stat-grid">
        <div class="stat"><span class="label">Days</span><span class="value">{stats["days_with_points"]}</span></div>
        <div class="stat"><span class="label">Points</span><span class="value">{stats["location_points"]}</span></div>
        <div class="stat"><span class="label">Locations</span><span class="value">{stats["unique_locations"]}</span></div>
        <div class="stat"><span class="label">Max visits</span><span class="value">{stats["max_visits"]}</span></div>
        <div class="stat"><span class="label">Min visits</span><span class="value">{stats["min_visits"]}</span></div>
        <div class="stat"><span class="label">Distance</span><span class="value">{stats["sampled_distance_km"]} km</span></div>
      </div>
      <div class="list">
        <h2>Most visited</h2>
        <div id="mostVisited"></div>
      </div>
      <div class="list">
        <h2>Least visited</h2>
        <div id="leastVisited"></div>
      </div>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
  <script>
    const data = {payload};
    const heatPoints = data.heat_points.map((item) => [item.lat, item.lon, item.weight]);
    const map = L.map("map", {{ preferCanvas: true, zoomControl: false }});
    const panel = document.getElementById("heatmapPanel");
    const togglePanelButton = document.getElementById("toggleHeatmapPanel");
    L.control.zoom({{ position: "bottomright" }}).addTo(map);
    L.control.scale({{ position: "bottomright", metric: true, imperial: false, maxWidth: 160 }}).addTo(map);
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
      opacity: 0.95,
    }}).addTo(map);
    const heat = heatPoints.length ? L.heatLayer(heatPoints, {{
      radius: 28,
      blur: 20,
      maxZoom: 17,
      minOpacity: 0.25,
    }}).addTo(map) : null;
    const markers = [];
    const centerAndZoom = (spot) => {{
      map.setView([spot.lat, spot.lon], Math.max(map.getZoom(), 14), {{ animate: true }});
    }};
    const makeSpot = (spot, index) => {{
      const marker = L.circleMarker([spot.lat, spot.lon], {{
        radius: Math.min(16, 5 + Math.log2(spot.count + 1) * 2.5),
        color: index === 0 ? "#b91c1c" : "#1e3a8a",
        weight: 2,
        fillColor: index === 0 ? "#ef4444" : "#3b82f6",
        fillOpacity: 0.78,
      }}).addTo(map);
      marker.bindTooltip(`${{spot.label}} · ${{spot.count}}`, {{ permanent: index < 3, direction: "right", className: "place-label" }});
      marker.bindPopup(`<strong>${{spot.label}}</strong><br>${{spot.count}} visits`);
      marker.on("click", () => centerAndZoom(spot));
      markers.push(marker);
    }};
    data.most_visited.forEach((spot, index) => makeSpot(spot, index));
    const syncPanelButton = () => {{
      togglePanelButton.textContent = panel.classList.contains("collapsed") ? "Show" : "Hide";
    }};
    togglePanelButton.addEventListener("click", () => {{
      panel.classList.toggle("collapsed");
      syncPanelButton();
    }});
    if (window.matchMedia("(max-width: 800px)").matches) {{
      panel.classList.add("collapsed");
    }}
    syncPanelButton();
    const listFor = (items, target) => {{
      const root = document.getElementById(target);
      root.innerHTML = items.map((spot) => `
        <div class="spot" data-lat="${{spot.lat}}" data-lon="${{spot.lon}}">
          <div class="name">${{spot.label}}</div>
          <div class="count">${{spot.count}}</div>
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
    listFor(data.most_visited, "mostVisited");
    listFor(data.least_visited, "leastVisited");
    const allPoints = data.heat_points.map((item) => [item.lat, item.lon]);
    if (allPoints.length) {{
      const bounds = L.latLngBounds(allPoints);
      map.fitBounds(bounds.pad(0.2));
    }} else {{
      map.setView([0, 0], 2);
      document.body.insertAdjacentHTML("beforeend", `<div class="empty"><strong>No OwnTracks points for ${{data.scope.value}}</strong><br>Try a different month or year.</div>`);
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
    payload = json.dumps(
        {"date": plan["date"], "track": track, "stops": stops, "namedPlaces": named_places},
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
      <button id="centerSelected" type="button" class="secondary" style="margin-top: 8px; width: 100%">Center selected</button>
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
        <button id="copyCommands" type="button" class="secondary">Copy</button>
      </div>
      <div id="stopList" class="stop-list"></div>
      <label for="commands">Paste this in Telegram</label>
      <textarea id="commands" readonly></textarea>
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
    const bounds = [];
    const markers = new Map();
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({{
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }}[char]));
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
    const parseTags = (value) => value.split(/[\\s,]+/).map((item) => item.trim()).filter(Boolean);
    const copyCommandsToClipboard = async (automatic = false) => {{
      if (!commands.value) return false;
      try {{
        if (navigator.clipboard?.writeText) {{
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
    const iconFor = (stop) => L.divIcon({{
      className: "",
      html: `<div style="background:${{selected.has(stop.alias) ? "#f59e0b" : "#dc2626"}};border:3px solid white;border-radius:999px;box-shadow:0 1px 7px rgb(0 0 0 / .35);height:18px;width:18px"></div>`,
      iconSize: [24, 24],
      iconAnchor: [12, 12]
    }});
    const shorten = (value, maxLength = 18) => {{
      const text = String(value ?? "");
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
      const element = marker?.getPopup()?.getElement();
      if (!element) return;
      L.DomEvent.disableClickPropagation(element);
    }};
    const refreshStop = (stop, refreshPopup = true) => {{
      const marker = markers.get(stop.alias);
      if (!marker) return;
      marker.setIcon(iconFor(stop));
      marker.setTooltipContent(escapeHtml(shortLabelFor(stop)));
      marker.getElement()?.setAttribute("title", labelFor(stop));
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
      const route = L.polyline(data.track, {{ color: "#2563eb", weight: 4, opacity: 0.8 }}).addTo(map);
      bounds.push(route.getBounds());
    }}
    for (const place of data.namedPlaces) {{
      const label = `${{place.action || ""}} ${{place.name}}`.trim();
      const marker = L.circleMarker([place.lat, place.lon], {{ radius: 6, color: "#2563eb", fillColor: "#2563eb", fillOpacity: 1, weight: 2 }}).addTo(map);
      marker.bindTooltip(escapeHtml(label), {{ permanent: true, direction: "right", className: "place-label" }});
      marker.bindPopup(`<strong>${{escapeHtml(label)}}</strong><br>${{escapeHtml(place.time)}}`);
      bounds.push(marker.getLatLng());
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
      marker.on("mouseover", () => marker.setTooltipContent(escapeHtml(labelFor(stop))));
      marker.on("mouseout", () => marker.setTooltipContent(escapeHtml(shortLabelFor(stop))));
      marker.on("popupopen", () => attachPopupHandlers(stop));
      bounds.push(marker.getLatLng());
      refreshStop(stop);
    }}
    if (bounds.length) {{
      const group = L.featureGroup(bounds.map((item) => item instanceof L.LatLngBounds ? L.rectangle(item, {{ opacity: 0, fillOpacity: 0 }}) : L.marker(item, {{ opacity: 0 }})));
      map.fitBounds(group.getBounds().pad(0.18));
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
    document.getElementById("centerSelected").addEventListener("click", () => {{
      const stop = selectedStops()[0];
      if (stop) centerStop(stop);
    }});
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
      if (bounds.length) {{
        const group = L.featureGroup(bounds.map((item) => item instanceof L.LatLngBounds ? L.rectangle(item, {{ opacity: 0, fillOpacity: 0 }}) : L.marker(item, {{ opacity: 0 }})));
        map.fitBounds(group.getBounds().pad(0.18));
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


def build_plan(events: list[Event], target_date: date, user_tags: dict | None = None) -> tuple[dict, list[Event]]:
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
    speeds = [event.speed_kmh for event in track_points if event.speed_kmh is not None]
    batteries = [event.payload.get("batt") for event in track_points if event.payload.get("batt") is not None]
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
            "ride_points": len(ride_points),
            "approx_distance_km": round(summarize_distance(track_points), 2),
            "max_speed_kmh": max(speeds) if speeds else None,
            "battery_start": batteries[0] if batteries else None,
            "battery_end": batteries[-1] if batteries else None,
        },
        "recommended_tags": sorted(set(recommended_tags)),
        "named_places": places,
        "candidate_stops": stops,
        "sampled_track": [point_dict(event) for event in track_points],
    }
    plan = apply_user_tags(plan, user_tags or {})
    for index, stop in enumerate(plan["candidate_stops"], start=1):
        stop["alias"] = f"s{index}"
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
    if not plan["named_places"]:
        lines.append("- None")
    for place in plan["named_places"]:
        lines.append(f"- {place['time']}: {place['action']} {place['name']}")
        if place.get("maps"):
            lines.append(f"  {place['maps']}")

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
            "/otb DD|MM-DD|YYYY-MM-DD",
            "/ott s1 tag1 tag2",
            "/otn s1 place name",
            "/oto s1 what happened there",
        ]
    )
    return "\n".join(lines)
