from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from codex_llm import ask_codex_text, build_codex_llm_config

from .env import project_path


DEFAULT_SEARCH_ALIASES: dict[str, list[str]] = {
    "medical": ["doctor", "clinic", "hospital", "physician", "checkup", "health", "healthcare"],
    "dental": ["dentist", "dental", "orthodontist"],
    "cycling": ["cycle", "cycling", "bike", "bicycle", "biking"],
    "work": ["office", "work", "meeting", "client"],
    "food": ["restaurant", "cafe", "lunch", "dinner", "snack"],
    "shopping": ["shop", "shopping", "store", "mall", "market"],
    "travel": ["airport", "station", "hotel", "trip", "travel"],
}


def alias_paths(env: dict[str, str]) -> tuple[Path, Path]:
    generated_path = project_path(
        env.get("OWNTRACKS_SEARCH_ALIASES_GENERATED_PATH"),
        "./data/owntracks/search_aliases.generated.json",
    )
    local_path = project_path(
        env.get("OWNTRACKS_SEARCH_ALIASES_LOCAL_PATH"),
        "./data/owntracks/search_aliases.local.json",
    )
    return generated_path, local_path


def normalize_aliases(data: Any) -> dict[str, list[str]]:
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for raw_category, raw_value in data.items():
        category = normalize_term(raw_category)
        if not category:
            continue
        if isinstance(raw_value, dict):
            raw_terms = raw_value.get("terms") or []
        else:
            raw_terms = raw_value or []
        if isinstance(raw_terms, str):
            raw_terms = [raw_terms]
        if not isinstance(raw_terms, list):
            continue
        terms = []
        for raw_term in raw_terms:
            term = normalize_term(raw_term)
            if term and term not in terms:
                terms.append(term)
        if terms:
            normalized[category] = terms
    return normalized


def normalize_term(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    return text[:80]


def load_alias_file(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    try:
        return normalize_aliases(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return {}


def merge_aliases(*sources: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for source in sources:
        for category, terms in normalize_aliases(source).items():
            values = merged.setdefault(category, [])
            for term in terms:
                if term not in values:
                    values.append(term)
    return dict(sorted(merged.items()))


def load_search_aliases(env: dict[str, str]) -> dict[str, list[str]]:
    generated_path, local_path = alias_paths(env)
    return merge_aliases(
        DEFAULT_SEARCH_ALIASES,
        load_alias_file(generated_path),
        load_alias_file(local_path),
    )


def search_alias_metadata(env: dict[str, str]) -> dict[str, Any]:
    generated_path, local_path = alias_paths(env)
    aliases = load_search_aliases(env)

    def file_meta(path: Path) -> dict[str, Any]:
        exists = path.exists()
        mtime = path.stat().st_mtime if exists else None
        return {
            "path": str(path),
            "exists": exists,
            "updated_at": datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds") if mtime else None,
        }

    return {
        "generated": file_meta(generated_path),
        "local": file_meta(local_path),
        "categories": len(aliases),
        "terms": sum(len(terms) for terms in aliases.values()),
    }


def stop_index_evidence(summary: dict, *, max_places: int = 120, max_terms: int = 400) -> dict:
    terms: dict[str, dict[str, Any]] = {}

    def add(value: Any, source: str) -> None:
        text = normalize_term(value)
        if not text or len(text) < 2:
            return
        item = terms.setdefault(text, {"term": text, "sources": [], "count": 0})
        item["count"] += 1
        if source not in item["sources"] and len(item["sources"]) < 5:
            item["sources"].append(source)

    places = sorted(
        summary.get("places") or [],
        key=lambda item: (int(item.get("visit_count") or 0), int(item.get("total_minutes") or 0)),
        reverse=True,
    )[:max_places]
    for place in places:
        add(place.get("name"), "place name")
        for tag in place.get("tags") or []:
            add(tag, "place tag")
        for visit in (place.get("visits") or [])[:8]:
            add(visit.get("raw_name"), "raw stop name")
            add(visit.get("motion_mode"), "motion mode")
            for tag in visit.get("tags") or []:
                add(tag, "visit tag")
            note = str(visit.get("note") or "")
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", note):
                add(token, "note token")

    ranked = sorted(terms.values(), key=lambda item: (-int(item["count"]), str(item["term"])))[:max_terms]
    return {
        "scope": summary.get("scope") or {},
        "stats": summary.get("stats") or {},
        "terms": ranked,
    }


def prompt_for_alias_generation(evidence: dict, existing_aliases: dict[str, list[str]]) -> str:
    evidence_json = json.dumps(evidence, ensure_ascii=False, indent=2)
    existing_json = json.dumps(existing_aliases, ensure_ascii=False, indent=2)
    return (
        "Create deterministic search alias categories for a personal OwnTracks stop index.\n"
        "Return only compact JSON, no markdown. The JSON shape must be:\n"
        "{\n"
        '  "category_name": ["term one", "term two"]\n'
        "}\n\n"
        "Rules:\n"
        "- Keep categories short, lowercase, and human-readable.\n"
        "- Include only terms that are useful for search expansion.\n"
        "- Merge synonyms, variants, singular/plural forms, and related personal place terms.\n"
        "- Do not include exact coordinates, dates, IDs, or long notes.\n"
        "- Prefer 5 to 30 categories total and 2 to 20 terms per category.\n"
        "- Preserve useful existing categories unless the evidence clearly suggests better terms.\n"
        "- Do not invent sensitive facts beyond the evidence.\n\n"
        f"Existing aliases:\n{existing_json}\n\n"
        f"OwnTracks evidence:\n{evidence_json}\n"
    )


def parse_alias_response(text: str) -> dict[str, list[str]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Codex alias response did not contain a JSON object")
    parsed = json.loads(stripped[start : end + 1])
    aliases = normalize_aliases(parsed)
    if not aliases:
        raise ValueError("Codex alias response did not contain any usable aliases")
    return aliases


def generate_aliases_with_codex(summary: dict, env: dict[str, str]) -> dict[str, list[str]]:
    generated_path, local_path = alias_paths(env)
    existing = merge_aliases(DEFAULT_SEARCH_ALIASES, load_alias_file(generated_path), load_alias_file(local_path))
    evidence = stop_index_evidence(summary)
    prompt = prompt_for_alias_generation(evidence, existing)
    response = ask_codex_text(prompt, build_codex_llm_config(env))
    return parse_alias_response(response)


def save_generated_aliases(path: Path, aliases: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalize_aliases(aliases), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
