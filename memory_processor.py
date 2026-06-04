from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from image_summary import IST, ImageSummaryConfig, log, ollama_chat, text_llm_chat


@dataclass(frozen=True)
class SavedMemory:
    path: Path
    content: str


@dataclass(frozen=True)
class MemoryAnswer:
    answer: str
    context_paths: tuple[Path, ...]


QUERY_STOP_WORDS = {
    "about",
    "after",
    "all",
    "and",
    "any",
    "are",
    "ask",
    "but",
    "can",
    "did",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "into",
    "more",
    "much",
    "not",
    "our",
    "the",
    "this",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


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


def query_terms(question: str) -> set[str]:
    terms = {
        term
        for term in re.findall(r"[a-z0-9]+", question.lower())
        if len(term) > 2 and term not in QUERY_STOP_WORDS
    }
    if "labour" in terms:
        terms.add("labor")
    if "labor" in terms:
        terms.add("labour")
    for term in list(terms):
        if len(term) == 4 and term.startswith("20") and term[2:].isdigit():
            terms.add(term[2:])
    return terms


def memory_score(path: Path, content: str, terms: set[str]) -> int:
    haystack = f"{path.name}\n{content}".lower()
    tokens = re.findall(r"[a-z0-9]+", haystack)
    score = 0
    for term in terms:
        occurrences = tokens.count(term)
        if occurrences:
            score += occurrences
            if term in path.name.lower():
                score += 5
    return score


def relevant_memories(question: str, cfg: ImageSummaryConfig) -> list[tuple[Path, str, int]]:
    terms = query_terms(question)
    memories: list[tuple[Path, str, int]] = []
    if not cfg.memory_dir.exists():
        return memories
    for path in sorted(cfg.memory_dir.glob("*.md"), reverse=True):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        score = memory_score(path, content, terms)
        if score > 0:
            memories.append((path, content, score))
    memories.sort(key=lambda item: (item[2], item[0].stat().st_mtime), reverse=True)
    return memories[: cfg.memory_query_top_k]


def build_memory_context(memories: list[tuple[Path, str, int]], max_chars: int) -> str:
    chunks: list[str] = []
    used = 0
    for path, content, _score in memories:
        header = f"\n\n--- MEMORY FILE: {path.name} ---\n"
        remaining = max_chars - used - len(header)
        if remaining <= 0:
            break
        body = content[:remaining]
        chunks.append(header + body)
        used += len(header) + len(body)
    return "".join(chunks).strip()


def answer_memory_question(question: str, cfg: ImageSummaryConfig) -> MemoryAnswer:
    memories = relevant_memories(question, cfg)
    if not memories:
        return MemoryAnswer(
            answer="I could not find a relevant memory for that question.",
            context_paths=(),
        )

    context = build_memory_context(memories, cfg.memory_query_max_context_chars)
    prompt = f"""
You answer questions using only the provided local memory files.

Rules:
- Answer the user's question directly.
- Use only facts present in the memory context.
- If a value is unclear, say what is unclear.
- Include the memory file name you used.
- Do not invent missing totals or dates.
- Do not summarize the memory files generally.
- Prefer a short answer with the exact amount, date, vehicle, and any useful subtotal/tax detail.
- For receipt tables, distinguish subtotal, tax, and tax-inclusive amounts when those labels are visible.

QUESTION:
{question}

MEMORY CONTEXT:
{context}
""".strip()
    answer = text_llm_chat(cfg, cfg.memory_query_model, prompt, "memory_query")
    return MemoryAnswer(
        answer=answer,
        context_paths=tuple(path for path, _content, _score in memories),
    )
