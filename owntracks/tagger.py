from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


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
        stop_id = f"{slug(name)}-{cluster[0].line_no}-{cluster[-1].line_no}"
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


def apply_user_tags(plan: dict, user_tags: dict) -> dict:
    day_tags = user_tags.get(plan["date"], {})
    global_tags = day_tags.get("activity", day_tags.get("ride", {})).get("tags", [])
    plan["recommended_tags"] = sorted(set(plan["recommended_tags"] + global_tags))
    stop_overrides = day_tags.get("stops", {})
    for stop in plan["candidate_stops"]:
        override = stop_overrides.get(stop["id"], {})
        if override.get("name"):
            stop["reviewed_name"] = override["name"]
        if override.get("tags"):
            stop["user_tags"] = override["tags"]
            plan["recommended_tags"] = sorted(set(plan["recommended_tags"] + override["tags"]))
        if override.get("note"):
            stop["user_note"] = override["note"]
    return plan


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
    if not value or value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    return date.fromisoformat(value)


def render_digest(plan: dict) -> str:
    window = plan.get("activity_window") or plan["ride_window"]
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

    lines.extend(["", "Stops to review"])
    if not plan["candidate_stops"]:
        lines.append("- None")
    for stop in plan["candidate_stops"]:
        display_name = stop.get("reviewed_name") or stop["name"]
        lines.append(f"- {stop['alias']} ({stop['id']}): {display_name}")
        lines.append(f"  Time: {stop['start']} to {stop['end']} ({stop['duration']})")
        lines.append(f"  Motion: {stop['motion']} | Points: {stop['points']}")
        lines.append(f"  Map: {stop['maps']}")
        if stop.get("user_tags"):
            lines.append(f"  Saved tags: {', '.join(stop['user_tags'])}")
        if stop.get("user_note"):
            lines.append(f"  Note: {stop['user_note']}")

    lines.extend(["", "Tags"])
    lines.append(", ".join(plan["recommended_tags"]))
    lines.extend(
        [
            "",
            "Commands:",
            "/ot [today|yesterday|YYYY-MM-DD]",
            "/tag s1 tag1 tag2",
            "/name s1 place name",
            "/note s1 what happened there",
        ]
    )
    return "\n".join(lines)
