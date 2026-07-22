from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"

AREA_TAGS = {"boundary", "landuse", "route"}
ROAD_TAGS = {"highway"}
POI_KEYS = ("amenity", "shop", "craft", "tourism", "office", "leisure", "healthcare")
LOW_VALUE_AMENITIES = {"shelter", "parking", "bench"}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371008.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def slug_tag(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return text[:80]


def element_lat_lon(element: dict[str, Any]) -> tuple[float, float] | None:
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if isinstance(center, dict) and center.get("lat") is not None and center.get("lon") is not None:
        return float(center["lat"]), float(center["lon"])
    return None


def category_for(tags: dict[str, Any]) -> str:
    for key in POI_KEYS:
        value = str(tags.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    if tags.get("public_transport") or tags.get("bus") or tags.get("railway"):
        return "transport"
    if tags.get("highway"):
        return f"highway:{tags.get('highway')}"
    if tags.get("boundary"):
        return f"boundary:{tags.get('boundary')}"
    if tags.get("landuse"):
        return f"landuse:{tags.get('landuse')}"
    return "named-feature"


def score_element(tags: dict[str, Any], distance_m: float) -> int:
    score = max(0, 1000 - int(distance_m * 5))
    if any(tags.get(key) for key in POI_KEYS):
        score += 450
    if tags.get("shop") or tags.get("craft") or tags.get("amenity"):
        score += 250
    if str(tags.get("amenity") or "") in LOW_VALUE_AMENITIES:
        score -= 400
    if tags.get("public_transport") or tags.get("bus"):
        score -= 180
    if any(tags.get(key) for key in ROAD_TAGS):
        score -= 350
    if any(tags.get(key) for key in AREA_TAGS):
        score -= 300
    if tags.get("boundary") == "administrative":
        score -= 350
    return score


def parse_overpass_candidates(payload: dict[str, Any], lat: float, lon: float, *, limit: int = 8) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for element in payload.get("elements") or []:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        name = str(tags.get("name:en") or tags.get("name") or "").strip()
        if not name:
            continue
        lat_lon = element_lat_lon(element)
        if lat_lon is None:
            continue
        item_lat, item_lon = lat_lon
        distance_m = haversine_m(lat, lon, item_lat, item_lon)
        category = category_for(tags)
        key = (name.casefold(), category)
        if key in seen:
            continue
        seen.add(key)
        score = score_element(tags, distance_m)
        tags_out = []
        tag_slug = slug_tag(category.replace(":", "-"))
        if tag_slug:
            tags_out.append(tag_slug)
        candidates.append(
            {
                "provider": "overpass",
                "osm_type": element.get("type"),
                "osm_id": element.get("id"),
                "name": name,
                "lat": round(item_lat, 7),
                "lon": round(item_lon, 7),
                "distance_m": round(distance_m),
                "category": category,
                "score": score,
                "tags": tags_out,
            }
        )
    candidates.sort(key=lambda item: (-int(item["score"]), int(item["distance_m"]), str(item["name"]).casefold()))
    return candidates[:limit]


def overpass_query(lat: float, lon: float, radius_m: int) -> str:
    return (
        "[out:json][timeout:25];"
        "("
        f"node(around:{radius_m},{lat:.7f},{lon:.7f})[name];"
        f"way(around:{radius_m},{lat:.7f},{lon:.7f})[name];"
        f"relation(around:{radius_m},{lat:.7f},{lon:.7f})[name];"
        ");"
        "out center tags 50;"
    )


def resolve_overpass(
    lat: float,
    lon: float,
    *,
    radius_m: int = 120,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    timeout_seconds: int = 25,
) -> list[dict[str, Any]]:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid coordinates")
    radius_m = max(20, min(int(radius_m), 1000))
    query = overpass_query(lat, lon, radius_m)
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "telegram-control-owntracks-place-resolver/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid Overpass response")
    return parse_overpass_candidates(payload, lat, lon)
