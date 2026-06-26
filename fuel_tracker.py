from __future__ import annotations

import base64
import csv
import datetime as dt
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from codex_llm import CodexLlmConfig, ask_codex_image, build_codex_llm_config
from image_summary import IST, env_bool, env_int, env_topic_id, log


DEFAULT_FIELDS = [
    "Date",
    "Odo (km)",
    "Fuel (l)",
    "Full",
    "Price",
    "km/l",
    "latitude",
    "longitude",
    "City",
    "Notes",
    "Missed",
    "TankNumber",
    "FuelType",
    "VolumePrice",
    "StationID",
    "ExcludeDistance",
    "UniqueId",
    "TankCalc",
    "Weather",
]


@dataclass(frozen=True)
class FuelConfig:
    enabled: bool
    chat_id: int
    topic_id: int
    work_dir: Path
    csv_path: Path
    llm_provider: str
    model: str
    ollama_url: str
    ollama_timeout_seconds: int
    codex_llm_config: CodexLlmConfig
    pending_window_seconds: int
    correction_window_seconds: int
    csv_fields: tuple[str, ...]


@dataclass
class FuelPending:
    key: str
    created_at: float
    updated_at: float
    message_ids: list[int] = field(default_factory=list)
    image_paths: list[Path] = field(default_factory=list)
    task: Any = None


@dataclass(frozen=True)
class FuelApproval:
    approval_id: str
    row: dict[str, str]
    image_paths: tuple[str, ...]
    message_ids: tuple[int, ...]


def build_fuel_config(env: dict[str, str], fallback_chat_id: int) -> FuelConfig:
    work_dir = Path(env.get("FUEL_WORK_DIR", "./data/fuel")).expanduser()
    llm_provider = env.get("FUEL_LLM_PROVIDER", "codex").strip().lower()
    if llm_provider not in {"codex", "ollama"}:
        raise ValueError("FUEL_LLM_PROVIDER must be one of: codex, ollama")
    fields = tuple(
        part.strip()
        for part in env.get("FUEL_CSV_FIELDS", ",".join(DEFAULT_FIELDS)).split(",")
        if part.strip()
    )
    return FuelConfig(
        enabled=env_bool(env.get("FUEL_ENABLED"), True),
        chat_id=env_int(env.get("FUEL_CHAT_ID"), fallback_chat_id),
        topic_id=env_topic_id(env.get("FUEL_TOPIC_ID"), 349),
        work_dir=work_dir,
        csv_path=Path(env.get("FUEL_CSV_PATH", str(work_dir / "fuel.csv"))).expanduser(),
        llm_provider=llm_provider,
        model=env.get("FUEL_MODEL", "minicpm-v:latest"),
        ollama_url=env.get("IMAGE_SUMMARY_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        ollama_timeout_seconds=env_int(env.get("IMAGE_SUMMARY_OLLAMA_TIMEOUT_SECONDS"), 600),
        codex_llm_config=build_codex_llm_config(env),
        pending_window_seconds=env_int(env.get("FUEL_PENDING_WINDOW_SECONDS"), 90),
        correction_window_seconds=env_int(env.get("FUEL_CORRECTION_WINDOW_SECONDS"), 300),
        csv_fields=fields or tuple(DEFAULT_FIELDS),
    )


def parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def normalize_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d.%m.%y",
    ):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return value


def clean_number(value: str) -> str:
    value = value.strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return ""
    number = match.group(0)
    try:
        parsed = float(number)
    except ValueError:
        return number
    return f"{parsed:g}"


def parse_float(value: str) -> float | None:
    cleaned = clean_number(value)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_decimal(value: float, places: int) -> str:
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def reconcile_receipt_math(row: dict[str, str], locked_fields: set[str] | None = None) -> dict[str, str]:
    updated = dict(row)
    locked = locked_fields or set()
    amount = parse_float(updated.get("Price", ""))
    volume = parse_float(updated.get("Fuel (l)", ""))
    rate = parse_float(updated.get("VolumePrice", ""))

    if {"Price", "VolumePrice"}.issubset(locked) and "Fuel (l)" not in locked and amount is not None and rate:
        updated["Fuel (l)"] = format_decimal(amount / rate, 3)
        return updated

    if {"Price", "Fuel (l)"}.issubset(locked) and "VolumePrice" not in locked and amount is not None and volume:
        updated["VolumePrice"] = format_decimal(amount / volume, 2)
        return updated

    if {"VolumePrice", "Fuel (l)"}.issubset(locked) and "Price" not in locked and rate is not None and volume is not None:
        updated["Price"] = format_decimal(rate * volume, 2)
        return updated

    # Receipt amount and unit rate are usually the most reliable OCR values.
    if "Fuel (l)" not in locked and amount is not None and rate and rate > 0:
        calculated_volume = amount / rate
        if volume is None or abs(calculated_volume - volume) > 0.02:
            updated["Fuel (l)"] = format_decimal(calculated_volume, 3)
        return updated

    if "VolumePrice" not in locked and amount is not None and volume and volume > 0:
        updated["VolumePrice"] = format_decimal(amount / volume, 2)
        return updated

    if "Price" not in locked and rate is not None and volume is not None:
        updated["Price"] = format_decimal(rate * volume, 2)
    return updated


def parse_corrections(text: str) -> dict[str, str]:
    aliases = {
        "odo": "Odo (km)",
        "odometer": "Odo (km)",
        "odometer_km": "Odo (km)",
        "km": "Odo (km)",
        "volume": "Fuel (l)",
        "vol": "Fuel (l)",
        "litre": "Fuel (l)",
        "litres": "Fuel (l)",
        "liter": "Fuel (l)",
        "liters": "Fuel (l)",
        "fuel_l": "Fuel (l)",
        "rate": "VolumePrice",
        "unit_rate": "VolumePrice",
        "price_per_litre": "VolumePrice",
        "price_per_liter": "VolumePrice",
        "amount": "Price",
        "amt": "Price",
        "total": "Price",
        "price": "Price",
        "date": "Date",
        "station": "City",
        "city": "City",
        "notes": "Notes",
    }
    key_pattern = "|".join(sorted((re.escape(key) for key in aliases), key=len, reverse=True))
    pattern = re.compile(
        rf"\b({key_pattern})\b\s*[:=]\s*(.*?)(?=\s+\b(?:{key_pattern})\b\s*[:=]|\n|,|;|$)",
        re.IGNORECASE,
    )
    corrections: dict[str, str] = {}
    for match in pattern.finditer(text):
        key = match.group(1).lower()
        field = aliases[key]
        raw_value = match.group(2).strip()
        if not raw_value:
            continue
        if field in {"Odo (km)", "Fuel (l)", "VolumePrice", "Price"}:
            value = clean_number(raw_value)
        elif field == "Date":
            value = normalize_date(raw_value)
        else:
            value = raw_value
        if value:
            corrections[field] = value
    return corrections


def apply_corrections(row: dict[str, str], corrections: dict[str, str], cfg: FuelConfig) -> dict[str, str]:
    updated = dict(row)
    updated.update(corrections)
    updated = reconcile_receipt_math(updated, set(corrections))
    updated["km/l"] = ""
    return updated


def extract_fuel_fields(image_paths: list[Path], cfg: FuelConfig) -> dict[str, str]:
    prompt = """
You are extracting fields from fuel receipt and vehicle odometer photos.

Return only JSON. Use this exact object shape:
{
  "date": "",
  "time": "",
  "odometer_km": "",
  "fuel_volume_l": "",
  "fuel_rate": "",
  "total_amount": "",
  "station": "",
  "fuel_type": "",
  "receipt_no": "",
  "notes": ""
}

Rules:
- Extract odometer reading from the odometer/dashboard image.
- Extract fuel volume, rate, amount, station, receipt number, date, and time from the receipt image.
- Use empty string for unknown fields.
- Preserve numbers as printed, but remove obvious leading zero padding where safe.
- Do not invent values. If uncertain, mention uncertainty in notes.
""".strip()
    if cfg.llm_provider == "codex":
        content = ask_codex_image(prompt, image_paths, cfg.codex_llm_config)
    else:
        content = extract_fuel_fields_ollama(prompt, image_paths, cfg)
    data = parse_json_object(content)
    extracted = {
        "date": normalize_date(normalize_value(data.get("date", ""))),
        "time": normalize_value(data.get("time", "")),
        "odometer_km": clean_number(normalize_value(data.get("odometer_km", ""))),
        "fuel_volume_l": clean_number(normalize_value(data.get("fuel_volume_l", ""))),
        "fuel_rate": clean_number(normalize_value(data.get("fuel_rate", ""))),
        "total_amount": clean_number(normalize_value(data.get("total_amount", ""))),
        "station": normalize_value(data.get("station", "")),
        "fuel_type": normalize_value(data.get("fuel_type", "")),
        "receipt_no": normalize_value(data.get("receipt_no", "")),
        "notes": normalize_value(data.get("notes", "")),
    }
    return extracted


def extract_fuel_fields_ollama(prompt: str, image_paths: list[Path], cfg: FuelConfig) -> str:
    images = [base64.b64encode(path.read_bytes()).decode("ascii") for path in image_paths]
    resp = requests.post(
        f"{cfg.ollama_url}/api/chat",
        json={
            "model": cfg.model,
            "messages": [{"role": "user", "content": prompt, "images": images}],
            "stream": False,
        },
        timeout=cfg.ollama_timeout_seconds,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {cfg.model} failed: HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json().get("message", {}).get("content", "")


def read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def find_section(rows: list[list[str]], section: str) -> tuple[int, int, int]:
    start = next(i for i, row in enumerate(rows) if row and row[0] == section)
    header = start + 1
    end = next((i for i in range(header + 1, len(rows)) if rows[i] and rows[i][0].startswith("##")), len(rows))
    return start, header, end


def next_unique_id(rows: list[list[str]], header_index: int, end_index: int) -> str:
    header = rows[header_index]
    try:
        unique_index = header.index("UniqueId")
    except ValueError:
        return "1"
    values = []
    for row in rows[header_index + 1 : end_index]:
        if len(row) > unique_index and row[unique_index].strip().isdigit():
            values.append(int(row[unique_index]))
    return str((max(values) if values else 0) + 1)


def previous_odometer(rows: list[list[str]], header_index: int, end_index: int, current_odo: str) -> float | None:
    header = rows[header_index]
    try:
        date_index = header.index("Date")
        odo_index = header.index("Odo (km)")
    except ValueError:
        return None
    try:
        current = float(current_odo)
    except ValueError:
        return None
    candidates = []
    for row in rows[header_index + 1 : end_index]:
        if len(row) <= max(date_index, odo_index):
            continue
        try:
            odo = float(row[odo_index])
        except ValueError:
            continue
        if odo < current:
            candidates.append(odo)
    return max(candidates) if candidates else None


def compute_consumption(rows: list[list[str]], header_index: int, end_index: int, odo: str, volume: str) -> str:
    try:
        current_odo = float(odo)
        fuel = float(volume)
    except ValueError:
        return "0.0"
    prev = previous_odometer(rows, header_index, end_index, odo)
    if prev is None or fuel <= 0:
        return "0.0"
    return str((current_odo - prev) / fuel)


def apply_fill_type(row: dict[str, str], is_full: bool) -> dict[str, str]:
    updated = dict(row)
    updated["Full"] = "1" if is_full else "0"
    updated["km/l"] = ""
    return updated


def build_row(
    extracted: dict[str, str],
    cfg: FuelConfig,
    image_paths: list[Path],
    message_ids: list[int],
) -> dict[str, str]:
    rows = read_csv_rows(cfg.csv_path) if cfg.csv_path.exists() else []
    header_index = end_index = 0
    if rows:
        _, header_index, end_index = find_section(rows, "## Log")

    odo = extracted.get("odometer_km", "")
    notes = extracted.get("notes", "")
    meta_notes = [
        part
        for part in [
            f"time={extracted.get('time')}" if extracted.get("time") else "",
            f"receipt_no={extracted.get('receipt_no')}" if extracted.get("receipt_no") else "",
            f"images={' '.join(str(path) for path in image_paths)}",
            f"messages={' '.join(str(message_id) for message_id in message_ids)}",
        ]
        if part
    ]
    if notes:
        meta_notes.insert(0, notes)

    row = {field: "" for field in cfg.csv_fields}
    row.update(
        {
            "Date": normalize_date(extracted.get("date", "")),
            "Odo (km)": odo,
            "Fuel (l)": extracted.get("fuel_volume_l", ""),
            "Full": "1",
            "Price": extracted.get("total_amount", ""),
            "km/l": "",
            "latitude": "",
            "longitude": "",
            "City": extracted.get("station", ""),
            "Notes": " | ".join(meta_notes),
            "Missed": "0",
            "TankNumber": "1",
            "FuelType": "0",
            "VolumePrice": extracted.get("fuel_rate", ""),
            "StationID": "0",
            "ExcludeDistance": "0.0",
            "UniqueId": next_unique_id(rows, header_index, end_index) if rows else "1",
            "TankCalc": "0.0",
            "Weather": "",
        }
    )
    row = reconcile_receipt_math(row)
    row["km/l"] = ""
    return row


def append_fuel_row(row: dict[str, str], cfg: FuelConfig) -> None:
    cfg.csv_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg.csv_path.exists():
        with cfg.csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["## Log"])
            writer.writerow(list(cfg.csv_fields))
            writer.writerow([row.get(field, "") for field in cfg.csv_fields])
        return

    rows = read_csv_rows(cfg.csv_path)
    _, header_index, end_index = find_section(rows, "## Log")
    header = rows[header_index]
    rows.insert(end_index, [row.get(field, "") for field in header])
    with cfg.csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def format_approval(row: dict[str, str], cfg: FuelConfig, approval_id: str) -> str:
    lines = [
        "Fuel entry extracted. Choose full tank, partial fill, correction, or reject.",
        "",
        f"Approval ID: `{approval_id}`",
        "",
    ]
    for field in cfg.csv_fields:
        value = row.get(field, "")
        if value:
            lines.append(f"- {field}: {value}")
    lines.extend(["", f"CSV: `{cfg.csv_path}`"])
    return "\n".join(lines)


def make_approval(row: dict[str, str], image_paths: list[Path], message_ids: list[int]) -> FuelApproval:
    return FuelApproval(
        approval_id=uuid.uuid4().hex[:12],
        row=row,
        image_paths=tuple(str(path) for path in image_paths),
        message_ids=tuple(message_ids),
    )
