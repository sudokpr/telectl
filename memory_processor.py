from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from image_summary import IST, ImageSummaryConfig, log, ollama_chat


@dataclass(frozen=True)
class SavedMemory:
    path: Path
    content: str


def slugify(value: str, default: str = "memory") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or default


def fallback_memory(raw_text: str) -> dict[str, Any]:
    first_line = next((line.strip() for line in raw_text.splitlines() if line.strip()), "Untitled note")
    return {
        "title": first_line[:80],
        "category": "note",
        "summary": raw_text.strip(),
        "key_fields": {},
        "tags": [],
    }


def parse_memory_json(raw: str) -> dict[str, Any]:
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
        raise ValueError("memory response was not a JSON object")
    return data


def extract_memory(raw_text: str, cfg: ImageSummaryConfig) -> dict[str, Any]:
    prompt = f"""
Convert this iOS-extracted text into a durable memory record.

Return only JSON with this exact shape:
{{
  "title": "short descriptive title",
  "category": "fuel|receipt|bill|note|task|event|contact|other",
  "summary": "concise useful summary",
  "key_fields": {{
    "amount": null,
    "currency": null,
    "date": null,
    "merchant": null,
    "fuel_volume": null,
    "fuel_unit": null,
    "fuel_rate": null,
    "vehicle": null,
    "odometer": null
  }},
  "tags": ["short", "lowercase", "tags"]
}}

Use null for unknown fields. Preserve important numbers exactly as written.
For fuel bills, extract amount, volume, rate, date, merchant, vehicle, and odometer when present.
For other text, use key_fields for any important structured facts.

TEXT:
{raw_text}
""".strip()
    try:
        response = ollama_chat(cfg, cfg.memory_llm_model, prompt)
        data = parse_memory_json(response)
    except Exception as exc:
        log(cfg, f"memory_llm_failed error={exc}")
        data = fallback_memory(raw_text)

    data.setdefault("title", "Untitled memory")
    data.setdefault("category", "other")
    data.setdefault("summary", raw_text.strip())
    data.setdefault("key_fields", {})
    data.setdefault("tags", [])
    return data


def memory_to_markdown(data: dict[str, Any], raw_text: str, source: dict[str, Any]) -> str:
    title = str(data.get("title") or "Untitled memory").strip()
    category = str(data.get("category") or "other").strip()
    summary = str(data.get("summary") or "").strip()
    key_fields = data.get("key_fields") if isinstance(data.get("key_fields"), dict) else {}
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []

    frontmatter = {
        "category": category,
        "tags": [str(tag) for tag in tags],
        "source": source,
        "created_at": dt.datetime.now(IST).isoformat(timespec="seconds"),
    }

    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", "", f"# {title}", "", "## Summary", "", summary or "No summary generated."])

    if key_fields:
        lines.extend(["", "## Key Fields", ""])
        for key in sorted(key_fields):
            value = key_fields[key]
            if value is not None and value != "":
                lines.append(f"- {key}: {value}")

    lines.extend(["", "## Raw Text", "", "```text", raw_text.strip(), "```", ""])
    return "\n".join(lines)


def save_memory(raw_text: str, cfg: ImageSummaryConfig, source: dict[str, Any]) -> SavedMemory:
    data = extract_memory(raw_text, cfg)
    stamp = dt.datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    title = str(data.get("title") or "memory")
    path = cfg.memory_dir / f"{stamp}-{slugify(title)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = memory_to_markdown(data, raw_text, source)
    path.write_text(content, encoding="utf-8")
    log(cfg, f"memory_saved path={path} title={title!r} category={data.get('category')!r}")
    return SavedMemory(path=path, content=content)
