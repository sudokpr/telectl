from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from image_summary import ImageSummaryConfig, log, run_ocr
from owntracks.env import project_path
from owntracks.tagger import Event, build_plan, event_time, haversine_km, load_user_tags, parse_log


IST = ZoneInfo("Asia/Kolkata")
INR_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", re.IGNORECASE)
AMOUNT_RE = re.compile(r"\b([0-9][0-9,]*(?:\.\d{1,2})?)\b")
DATE_RE = re.compile(r"\b(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")


@dataclass(frozen=True)
class SpendingConfig:
    enabled: bool
    db_path: Path
    evidence_dir: Path
    owntracks_log_path: Path
    user_tags_path: Path
    poll_seconds: int
    index_images: bool
    max_image_bytes: int
    nearest_stop_radius_m: int
    nearest_stop_time_window_minutes: int


@dataclass(frozen=True)
class IndexResult:
    scanned: int
    indexed: int
    skipped: int
    errors: int


def env_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_spending_config(env: dict[str, str]) -> SpendingConfig:
    work_dir = project_path(env.get("SPENDING_WORK_DIR"), "./data/spending")
    return SpendingConfig(
        enabled=env_bool(env.get("SPENDING_INDEX_ENABLED"), True),
        db_path=project_path(env.get("SPENDING_DB_PATH"), str(work_dir / "spending.sqlite")),
        evidence_dir=project_path(env.get("SPENDING_EVIDENCE_DIR"), str(work_dir / "evidence")),
        owntracks_log_path=project_path(env.get("OWNTRACKS_LOG_PATH"), "./data/owntracks/mqtt.log"),
        user_tags_path=project_path(env.get("OWNTRACKS_USER_TAGS_PATH"), "./data/owntracks/user_tags.json"),
        poll_seconds=int(env.get("SPENDING_INDEX_POLL_SECONDS") or "60"),
        index_images=env_bool(env.get("SPENDING_INDEX_IMAGES"), True),
        max_image_bytes=int(env.get("SPENDING_MAX_IMAGE_BYTES") or str(4 * 1024 * 1024)),
        nearest_stop_radius_m=int(env.get("SPENDING_NEAREST_STOP_RADIUS_METERS") or "300"),
        nearest_stop_time_window_minutes=int(env.get("SPENDING_NEAREST_STOP_TIME_WINDOW_MINUTES") or "180"),
    )


def connect(cfg: SpendingConfig) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            owntracks_line INTEGER NOT NULL UNIQUE,
            recorded_at TEXT,
            received_at TEXT,
            lat REAL,
            lon REAL,
            poi_text TEXT,
            image_path TEXT,
            image_sha256 TEXT,
            extracted_text TEXT,
            confidence REAL NOT NULL DEFAULT 0,
            reviewed INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            merchant TEXT,
            transaction_date TEXT,
            raw_text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS receipt_items (
            id INTEGER PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            item_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            quantity REAL,
            unit TEXT,
            unit_price REAL,
            line_total REAL,
            raw_line TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS location_matches (
            event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
            label TEXT,
            distance_m REAL,
            time_delta_seconds INTEGER,
            map_date TEXT,
            maps_url TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_amount_date ON transactions(amount, transaction_date);
        CREATE INDEX IF NOT EXISTS idx_items_name ON receipt_items(normalized_name);
        """
    )
    conn.commit()


def normalize_name(value: str) -> str:
    clean = re.sub(r"[^a-z0-9 ]+", " ", value.lower())
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def parse_date(text: str, fallback: dt.datetime | None) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return fallback.date().isoformat() if fallback else None
    raw = match.group(1)
    if "-" in raw and raw[:4].isdigit():
        parts = raw.split("-")
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    sep = "/" if "/" in raw else "-"
    day, month, year = raw.split(sep)
    year_int = int(year)
    if year_int < 100:
        year_int += 2000
    return f"{year_int:04d}-{int(month):02d}-{int(day):02d}"


def amount_from_text(text: str) -> float | None:
    match = INR_RE.search(text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def likely_merchant(text: str) -> str | None:
    for pattern in (
        r"\bat\s+([A-Za-z0-9 &.'-]{3,60})",
        r"\bto\s+([A-Za-z0-9 &.'-]{3,60})",
        r"\bmerchant[:\s]+([A-Za-z0-9 &.'-]{3,60})",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip(" .,-")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:80] or None


def parse_receipt_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    skip_words = {
        "total",
        "subtotal",
        "tax",
        "gst",
        "cgst",
        "sgst",
        "amount",
        "balance",
        "cash",
        "upi",
        "paid",
        "debited",
        "credited",
        "spent",
        "transaction",
    }
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if len(line) < 4 or not re.search(r"[A-Za-z]", line):
            continue
        if any(line.lower().startswith(word) for word in skip_words):
            continue
        numbers = [float(item.replace(",", "")) for item in AMOUNT_RE.findall(line)]
        if not numbers:
            continue
        name_part = re.sub(r"(?:rs\.?|inr|₹)?\s*[0-9][0-9,]*(?:\.\d{1,2})?", " ", line, flags=re.IGNORECASE)
        name = re.sub(r"\b(?:kg|g|pcs?|nos?|x)\b", " ", name_part, flags=re.IGNORECASE)
        name = re.sub(r"[^A-Za-z0-9 &.'-]+", " ", name)
        name = re.sub(r"\s+", " ", name).strip(" -")
        if len(name) < 2:
            continue
        unit_match = re.search(r"\b(kg|g|pcs?|nos?)\b", line, re.IGNORECASE)
        qty = numbers[0] if unit_match and len(numbers) >= 2 else None
        unit = unit_match.group(1).lower() if unit_match else None
        line_total = numbers[-1]
        unit_price = None
        if unit_match and len(numbers) >= 2:
            unit_price = numbers[-2] if len(numbers) >= 3 else line_total / qty if qty else None
        elif len(numbers) >= 2:
            unit_price = numbers[-2]
        items.append(
            {
                "item_name": name[:120],
                "normalized_name": normalize_name(name),
                "quantity": qty,
                "unit": unit,
                "unit_price": unit_price,
                "line_total": line_total,
                "raw_line": raw_line.strip(),
            }
        )
    return items[:80]


def event_image_path(event: Event, cfg: SpendingConfig) -> tuple[Path | None, str | None]:
    image = str(event.payload.get("image") or "").strip()
    if not image or not cfg.index_images:
        return None, None
    try:
        body = base64.b64decode(image, validate=False)
    except Exception:
        return None, None
    if not body or len(body) > cfg.max_image_bytes:
        return None, None
    digest = hashlib.sha256(body).hexdigest()
    day = event_time(event).date().isoformat()
    path = cfg.evidence_dir / day / f"{event.line_no}-{digest[:16]}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(body)
    return path, digest


def extracted_text_for_event(event: Event, cfg: SpendingConfig, image_cfg: ImageSummaryConfig) -> tuple[str, Path | None, str | None, str | None]:
    text = str(event.payload.get("poi") or "").strip()
    image_path, image_sha = event_image_path(event, cfg)
    error = None
    if image_path and image_cfg.ocr_enabled:
        try:
            ocr_text = run_ocr(image_path, image_cfg)
            if ocr_text:
                text = "\n".join(part for part in [text, ocr_text] if part)
        except Exception as exc:
            error = str(exc)
    return text, image_path, image_sha, error


def stop_label(stop: dict[str, Any]) -> str:
    return str(stop.get("reviewed_name") or stop.get("name") or stop.get("alias") or stop.get("id") or "").strip()


def plan_location_context(
    event: Event,
    events: list[Event],
    cfg: SpendingConfig,
    user_tags: dict,
    plan_cache: dict[str, dict],
) -> dict[str, Any] | None:
    if event.lat is None or event.lon is None:
        return None
    current_time = event_time(event)
    map_date = current_time.date().isoformat()
    timestamp = int(current_time.timestamp())
    plan = plan_cache.get(map_date)
    if plan is None:
        plan, _track_points = build_plan(events, current_time.date(), user_tags)
        plan_cache[map_date] = plan

    for stop in plan.get("candidate_stops", []):
        start_ts = stop.get("visit_start_timestamp", stop.get("start_timestamp"))
        end_ts = stop.get("visit_end_timestamp", stop.get("end_timestamp"))
        if not isinstance(start_ts, int) or not isinstance(end_ts, int) or not (start_ts <= timestamp <= end_ts):
            continue
        label = stop_label(stop)
        if not label:
            continue
        distance_m = None
        if stop.get("lat") is not None and stop.get("lon") is not None:
            distance_m = round(haversine_km(event.lat, event.lon, float(stop["lat"]), float(stop["lon"])) * 1000, 1)
        return {
            "label": label,
            "distance_m": distance_m,
            "time_delta_seconds": 0,
            "map_date": map_date,
            "maps_url": f"https://www.google.com/maps?q={event.lat:.6f},{event.lon:.6f}",
        }

    for segment in plan.get("travel_segments", []):
        start_ts = segment.get("start_timestamp")
        end_ts = segment.get("end_timestamp")
        if not isinstance(start_ts, int) or not isinstance(end_ts, int) or not (start_ts <= timestamp <= end_ts):
            continue
        label = str(segment.get("label") or "").strip()
        if not label:
            continue
        return {
            "label": f"en route: {label}",
            "distance_m": None,
            "time_delta_seconds": 0,
            "map_date": map_date,
            "maps_url": f"https://www.google.com/maps?q={event.lat:.6f},{event.lon:.6f}",
        }
    return None


def nearby_location(
    event: Event,
    events: list[Event],
    cfg: SpendingConfig,
    user_tags: dict | None = None,
    plan_cache: dict[str, dict] | None = None,
) -> dict[str, Any] | None:
    if event.lat is None or event.lon is None:
        return None
    planned = plan_location_context(event, events, cfg, user_tags or {}, plan_cache or {})
    if planned is not None:
        return planned
    current_time = event_time(event)
    best: tuple[float, float, Event, str] | None = None
    for candidate in events:
        if not candidate.is_location or candidate.line_no == event.line_no:
            continue
        label = str(candidate.payload.get("desc") or "").strip()
        regions = [str(item) for item in candidate.payload.get("inregions") or [] if str(item).strip()]
        if not label and regions:
            label = regions[0]
        if not label:
            continue
        delta = abs((event_time(candidate) - current_time).total_seconds())
        if delta > cfg.nearest_stop_time_window_minutes * 60:
            continue
        distance_m = haversine_km(event.lat, event.lon, candidate.lat, candidate.lon) * 1000
        if distance_m > cfg.nearest_stop_radius_m:
            continue
        score = distance_m + delta / 60
        if best is None or score < best[0]:
            best = (score, distance_m, candidate, label)
    if best is None:
        return {
            "label": "near recorded POI coordinates",
            "distance_m": 0,
            "time_delta_seconds": 0,
            "map_date": current_time.date().isoformat(),
            "maps_url": f"https://www.google.com/maps?q={event.lat:.6f},{event.lon:.6f}",
        }
    _score, distance_m, candidate, label = best
    return {
        "label": label,
        "distance_m": round(distance_m, 1),
        "time_delta_seconds": int((event_time(candidate) - current_time).total_seconds()),
        "map_date": current_time.date().isoformat(),
        "maps_url": f"https://www.google.com/maps?q={event.lat:.6f},{event.lon:.6f}",
    }


def refresh_location_match(
    conn: sqlite3.Connection,
    event_id: int,
    event: Event,
    events: list[Event],
    cfg: SpendingConfig,
    user_tags: dict,
    plan_cache: dict[str, dict],
) -> None:
    location = nearby_location(event, events, cfg, user_tags, plan_cache)
    if not location:
        return
    conn.execute("DELETE FROM location_matches WHERE event_id = ?", (event_id,))
    conn.execute(
        """
        INSERT INTO location_matches (event_id, label, distance_m, time_delta_seconds, map_date, maps_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            location["label"],
            location["distance_m"],
            location["time_delta_seconds"],
            location["map_date"],
            location["maps_url"],
        ),
    )


def upsert_event(
    conn: sqlite3.Connection,
    event: Event,
    events: list[Event],
    cfg: SpendingConfig,
    image_cfg: ImageSummaryConfig,
    user_tags: dict,
    plan_cache: dict[str, dict],
) -> bool:
    existing = conn.execute("SELECT id FROM events WHERE owntracks_line = ?", (event.line_no,)).fetchone()
    if existing:
        refresh_location_match(conn, int(existing["id"]), event, events, cfg, user_tags, plan_cache)
        return False
    text, image_path, image_sha, error = extracted_text_for_event(event, cfg, image_cfg)
    if not text and not image_path:
        return False
    amount = amount_from_text(text)
    items = parse_receipt_items(text)
    if amount is None and not items:
        return False
    recorded_at = event_time(event)
    confidence = 0.75 if amount is not None else 0.55
    now = dt.datetime.now(IST).isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        INSERT INTO events (
            owntracks_line, recorded_at, received_at, lat, lon, poi_text, image_path,
            image_sha256, extracted_text, confidence, reviewed, error, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            event.line_no,
            recorded_at.isoformat(timespec="seconds"),
            event.received_at.isoformat(timespec="seconds") if event.received_at else None,
            event.lat,
            event.lon,
            str(event.payload.get("poi") or ""),
            str(image_path) if image_path else None,
            image_sha,
            text,
            confidence,
            error,
            now,
        ),
    )
    event_id = int(cursor.lastrowid)
    if amount is not None:
        conn.execute(
            """
            INSERT INTO transactions (event_id, amount, currency, merchant, transaction_date, raw_text)
            VALUES (?, ?, 'INR', ?, ?, ?)
            """,
            (event_id, amount, likely_merchant(text), parse_date(text, recorded_at), text[:4000]),
        )
    for item in items:
        conn.execute(
            """
            INSERT INTO receipt_items (
                event_id, item_name, normalized_name, quantity, unit, unit_price, line_total, raw_line
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                item["item_name"],
                item["normalized_name"],
                item["quantity"],
                item["unit"],
                item["unit_price"],
                item["line_total"],
                item["raw_line"],
            ),
        )
    refresh_location_match(conn, event_id, event, events, cfg, user_tags, plan_cache)
    return True


def index_scope(
    cfg: SpendingConfig,
    image_cfg: ImageSummaryConfig,
    scope: str | None = None,
) -> IndexResult:
    conn = connect(cfg)
    all_events = parse_log(cfg.owntracks_log_path, IST)
    events = all_events
    if scope:
        events = [event for event in all_events if event_matches_scope(event, scope)]
    user_tags = load_user_tags(cfg.user_tags_path)
    plan_cache: dict[str, dict] = {}
    scanned = indexed = skipped = errors = 0
    for event in events:
        if not event.is_location or not str(event.payload.get("poi") or "").strip():
            continue
        scanned += 1
        try:
            if upsert_event(conn, event, all_events, cfg, image_cfg, user_tags, plan_cache):
                indexed += 1
            else:
                skipped += 1
        except Exception as exc:
            errors += 1
            log(image_cfg, f"spending_index_event_failed line={event.line_no} error={exc}")
    conn.commit()
    conn.close()
    return IndexResult(scanned=scanned, indexed=indexed, skipped=skipped, errors=errors)


def event_matches_scope(event: Event, scope: str) -> bool:
    day = event_time(event).date().isoformat()
    clean = scope.strip().lower()
    if clean == "today":
        return day == dt.datetime.now(IST).date().isoformat()
    if clean == "yesterday":
        return day == (dt.datetime.now(IST).date() - dt.timedelta(days=1)).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean):
        return day == clean
    if re.fullmatch(r"\d{4}-\d{2}", clean):
        return day.startswith(clean + "-")
    if re.fullmatch(r"\d{4}", clean):
        return day.startswith(clean + "-")
    return True


def parse_query_date(question: str) -> str | None:
    lower = question.lower()
    if "today" in lower:
        return dt.datetime.now(IST).date().isoformat()
    if "yesterday" in lower:
        return (dt.datetime.now(IST).date() - dt.timedelta(days=1)).isoformat()
    month_names = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\b", lower)
    if match and match.group(2) in month_names:
        return f"{int(match.group(3)):04d}-{month_names[match.group(2)]:02d}-{int(match.group(1)):02d}"
    return parse_date(question, None)


def parse_query_year(question: str) -> str | None:
    match = re.search(r"\b(20\d{2})\b", question)
    return match.group(1) if match else None


def query_spending(cfg: SpendingConfig, question: str) -> str:
    conn = connect(cfg)
    try:
        amount = amount_from_text(question)
        if amount is not None or re.search(r"\bwhere\b", question, re.IGNORECASE):
            return query_amount_location(conn, question, amount)
        if re.search(r"\b(avg|average|last|latest|price)\b", question, re.IGNORECASE):
            return query_item_price(conn, question)
        return "Ask about an amount/date/location or an item price, for example: `Where was Rs.450 spent on 7th July 2026?`"
    finally:
        conn.close()


def query_amount_location(conn: sqlite3.Connection, question: str, amount: float | None) -> str:
    date_text = parse_query_date(question)
    params: list[Any] = []
    where = []
    if amount is not None:
        where.append("ABS(t.amount - ?) < 0.01")
        params.append(amount)
    if date_text:
        where.append("t.transaction_date = ?")
        params.append(date_text)
    if not where:
        return "I need at least an amount or a date for a spend-location lookup."
    rows = conn.execute(
        f"""
        SELECT t.amount, t.currency, t.merchant, t.transaction_date, e.recorded_at, e.confidence,
               e.reviewed, e.owntracks_line, l.label, l.distance_m, l.maps_url
        FROM transactions t
        JOIN events e ON e.id = t.event_id
        LEFT JOIN location_matches l ON l.event_id = e.id
        WHERE {' AND '.join(where)}
        ORDER BY e.recorded_at DESC
        LIMIT 8
        """,
        params,
    ).fetchall()
    if not rows:
        return "No matching spending POI is indexed yet. Run `/spi YYYY-MM-DD` to backfill that date."
    lines = []
    for row in rows:
        status = "reviewed" if row["reviewed"] else "unreviewed"
        location = row["label"] or "unknown location"
        distance = f", {row['distance_m']:.0f} m away" if row["distance_m"] is not None else ""
        maps = f"; map: {row['maps_url']}" if row["maps_url"] else ""
        lines.append(
            f"- {row['currency']} {row['amount']:.2f} on {row['transaction_date'] or row['recorded_at']}: "
            f"{location}{distance}{maps}; merchant: {row['merchant'] or 'unknown'}; {status}; line {row['owntracks_line']}"
        )
    return "Matching spend records:\n" + "\n".join(lines)


def query_item_price(conn: sqlite3.Connection, question: str) -> str:
    year = parse_query_year(question)
    item = item_term_from_question(question)
    if not item:
        return "I could not identify the item name in that price question."
    like = f"%{normalize_name(item)}%"
    params: list[Any] = [like]
    date_filter = ""
    if year:
        date_filter = "AND substr(t.transaction_date, 1, 4) = ?"
        params.append(year)
    if re.search(r"\b(last|latest)\b", question, re.IGNORECASE):
        row = conn.execute(
            f"""
            SELECT i.item_name, i.unit_price, i.line_total, i.unit, t.transaction_date, t.merchant,
                   l.label, e.owntracks_line
            FROM receipt_items i
            JOIN events e ON e.id = i.event_id
            LEFT JOIN transactions t ON t.event_id = e.id
            LEFT JOIN location_matches l ON l.event_id = e.id
            WHERE i.normalized_name LIKE ? {date_filter}
            ORDER BY COALESCE(t.transaction_date, substr(e.recorded_at, 1, 10)) DESC, e.recorded_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            return f"No indexed price found for `{item}`."
        price = row["unit_price"] if row["unit_price"] is not None else row["line_total"]
        unit = f" per {row['unit']}" if row["unit"] else ""
        return (
            f"Last indexed price for {row['item_name']}: INR {price:.2f}{unit} "
            f"on {row['transaction_date'] or 'unknown date'} at {row['merchant'] or row['label'] or 'unknown place'} "
            f"(line {row['owntracks_line']})."
        )
    rows = conn.execute(
        f"""
        SELECT i.unit_price, i.line_total
        FROM receipt_items i
        JOIN events e ON e.id = i.event_id
        LEFT JOIN transactions t ON t.event_id = e.id
        WHERE i.normalized_name LIKE ? {date_filter}
        """,
        params,
    ).fetchall()
    values = [float(row["unit_price"] if row["unit_price"] is not None else row["line_total"]) for row in rows]
    if not values:
        return f"No indexed price found for `{item}`."
    return (
        f"Average indexed price for {item}"
        + (f" in {year}" if year else "")
        + f": INR {sum(values) / len(values):.2f} across {len(values)} item(s). "
        f"Min INR {min(values):.2f}, max INR {max(values):.2f}."
    )


def item_term_from_question(question: str) -> str:
    lower = question.lower()
    lower = re.sub(r"\b(what|is|the|last|latest|avg|average|price|of|i|have|paid|in|per|kg|for)\b", " ", lower)
    lower = re.sub(r"\b20\d{2}\b", " ", lower)
    lower = re.sub(r"[^a-z0-9 ]+", " ", lower)
    return re.sub(r"\s+", " ", lower).strip()


def recent_events(cfg: SpendingConfig, limit: int = 50) -> list[dict[str, Any]]:
    conn = connect(cfg)
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.owntracks_line, e.recorded_at, e.confidence, e.reviewed, e.error,
                   t.amount, t.currency, t.merchant, l.label
            FROM events e
            LEFT JOIN transactions t ON t.event_id = e.id
            LEFT JOIN location_matches l ON l.event_id = e.id
            ORDER BY e.recorded_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


async def spending_index_loop(cfg: SpendingConfig, image_cfg: ImageSummaryConfig) -> None:
    import asyncio

    while True:
        try:
            if cfg.enabled:
                result = await asyncio.to_thread(index_scope, cfg, image_cfg, None)
                if result.indexed or result.errors:
                    log(
                        image_cfg,
                        "spending_index_auto "
                        f"scanned={result.scanned} indexed={result.indexed} skipped={result.skipped} errors={result.errors}",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(image_cfg, f"spending_index_auto_failed error={exc}")
        await asyncio.sleep(max(10, cfg.poll_seconds))


def index_result_dict(result: IndexResult) -> dict[str, int]:
    return {
        "scanned": result.scanned,
        "indexed": result.indexed,
        "skipped": result.skipped,
        "errors": result.errors,
    }
