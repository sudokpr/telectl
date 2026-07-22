from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pypdfium2
from pypdf import PdfReader

from image_summary import ImageSummaryConfig, log, run_ocr
from memory_processor import SavedMemory, save_memory


@dataclass(frozen=True)
class PdfExtraction:
    text: str
    method: str
    quality: str
    page_count: int
    pages_processed: int
    character_count: int


def pdf_digest(pdf_path: Path) -> str:
    digest = hashlib.sha256()
    with pdf_path.open("rb") as pdf_file:
        for chunk in iter(lambda: pdf_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extraction_quality(text: str) -> str:
    clean = text.strip()
    if not clean:
        return "none"
    printable = sum(character.isprintable() for character in clean)
    alphanumeric = sum(character.isalnum() for character in clean)
    ratio = alphanumeric / max(1, len(clean))
    if len(clean) >= 200 and printable / len(clean) >= 0.95 and ratio >= 0.45:
        return "high"
    if len(clean) >= 50 and printable / len(clean) >= 0.85 and ratio >= 0.30:
        return "medium"
    return "low"


def extract_embedded_pdf_text(pdf_path: Path, max_pages: int) -> PdfExtraction:
    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("encrypted PDF requires a password")
    page_count = len(reader.pages)
    pages = min(page_count, max(1, max_pages))
    parts: list[str] = []
    for index in range(pages):
        text = str(reader.pages[index].extract_text() or "").strip()
        if text:
            parts.append(f"--- Page {index + 1} ---\n{text}")
    combined = "\n\n".join(parts).strip()
    return PdfExtraction(combined, "embedded_text", extraction_quality(combined), page_count, pages, len(combined))


def extract_pdf_ocr(pdf_path: Path, cfg: ImageSummaryConfig, max_pages: int, render_scale: float) -> PdfExtraction:
    document = pypdfium2.PdfDocument(str(pdf_path))
    page_count = len(document)
    pages = min(page_count, max(1, max_pages))
    parts: list[str] = []
    try:
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pdf-ocr-", dir=str(cfg.work_dir)) as temp_dir:
            for index in range(pages):
                page = document[index]
                bitmap = page.render(scale=max(1.0, render_scale))
                image_path = Path(temp_dir) / f"page-{index + 1}.png"
                try:
                    bitmap.to_pil().save(image_path, format="PNG")
                    text = run_ocr(image_path, cfg).strip()
                    if text:
                        parts.append(f"--- Page {index + 1} ---\n{text}")
                finally:
                    bitmap.close()
                    page.close()
    finally:
        document.close()
    combined = "\n\n".join(parts).strip()
    return PdfExtraction(combined, "tesseract_ocr", extraction_quality(combined), page_count, pages, len(combined))


def extract_pdf_text(pdf_path: Path, cfg: ImageSummaryConfig) -> PdfExtraction:
    max_pages = int(getattr(cfg, "memory_pdf_max_pages", 10))
    embedded = extract_embedded_pdf_text(pdf_path, max_pages)
    if embedded.quality in {"high", "medium"}:
        return embedded
    try:
        ocr = extract_pdf_ocr(
            pdf_path,
            cfg,
            max_pages,
            float(getattr(cfg, "memory_pdf_render_scale", 2.5)),
        )
    except Exception as exc:
        log(cfg, f"pdf_ocr_failed file={pdf_path.name} error={exc}")
        return embedded
    return ocr if ocr.character_count > embedded.character_count else embedded


def pdf_memory_path(pdf_path: Path, cfg: ImageSummaryConfig) -> Path | None:
    marker = pdf_digest(pdf_path)
    if not cfg.memory_dir.exists():
        return None
    for memory_path in cfg.memory_dir.glob("*.md"):
        try:
            if marker in memory_path.read_text(encoding="utf-8"):
                return memory_path
        except UnicodeDecodeError:
            continue
    return None


def extraction_metadata_from_memory(path: Path) -> PdfExtraction:
    content = path.read_text(encoding="utf-8")
    match = re.search(r"^source:\s*(\{.*\})\s*$", content, re.MULTILINE)
    source: dict[str, Any] = {}
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                source = parsed
        except json.JSONDecodeError:
            pass
    return PdfExtraction(
        text="",
        method=str(source.get("pdf_extraction_method") or "previously_extracted"),
        quality=str(source.get("pdf_extraction_quality") or "unknown"),
        page_count=int(source.get("pdf_page_count") or 0),
        pages_processed=int(source.get("pdf_pages_processed") or 0),
        character_count=int(source.get("pdf_character_count") or 0),
    )


def source_metadata_from_memory(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {}
    match = re.search(r"^source:\s*(\{.*\})\s*$", content, re.MULTILINE)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def legacy_caption_memory_path(cfg: ImageSummaryConfig, user_comment: str) -> Path | None:
    if not user_comment or not cfg.memory_dir.exists():
        return None
    for path in cfg.memory_dir.glob("*.md"):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if '"pdf_extraction_status": "attachment_not_downloaded_by_legacy_flow"' not in content:
            continue
        if likely_pdf_caption_duplicate(path, (user_comment,)):
            return path
    return None


def legacy_reply_memory_path(cfg: ImageSummaryConfig, replied_message_id: object) -> Path | None:
    try:
        wanted = int(replied_message_id)
    except (TypeError, ValueError):
        return None
    if not cfg.memory_dir.exists():
        return None
    for path in cfg.memory_dir.glob("*.md"):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = re.search(r"^source:\s*(\{.*\})\s*$", content, re.MULTILINE)
        if not match:
            continue
        try:
            source = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(source, dict) or source.get("pdf_extraction_status") != "attachment_not_downloaded_by_legacy_flow":
            continue
        message_ids = source.get("telegram_message_ids")
        if not isinstance(message_ids, list):
            message_ids = [source.get("telegram_message_id")]
        if wanted in {int(value) for value in message_ids if str(value or "").isdigit()}:
            return path
    return None


def save_pdf_memory(
    pdf_path: Path,
    cfg: ImageSummaryConfig,
    source: dict[str, Any],
) -> tuple[SavedMemory | None, PdfExtraction]:
    user_comment = str(source.get("user_comment") or "").strip()
    existing_path = pdf_memory_path(pdf_path, cfg)
    if existing_path and not user_comment:
        return None, extraction_metadata_from_memory(existing_path)
    if existing_path and user_comment:
        existing_content = existing_path.read_text(encoding="utf-8")
        if user_comment in existing_content:
            return None, extraction_metadata_from_memory(existing_path)

    target_path = (
        existing_path
        or legacy_reply_memory_path(cfg, source.get("caption_reply_to_message_id"))
        or legacy_caption_memory_path(cfg, user_comment)
    )
    extraction = extract_pdf_text(pdf_path, cfg)
    raw_text = extraction.text.strip()
    if user_comment:
        raw_text = "\n\n".join(
            part for part in (raw_text, f"User-supplied PDF comment:\n{user_comment}") if part
        )
    if not raw_text:
        raise ValueError("PDF contained no extractable text and no caption")
    previous_source = source_metadata_from_memory(target_path) if target_path else {}
    history = [dict(item) for item in previous_source.get("user_comment_history", []) if isinstance(item, dict)]
    previous_comment = str(previous_source.get("user_comment") or "").strip()
    known_comments = {str(item.get("comment") or "").strip() for item in history}
    if previous_comment and previous_comment not in known_comments:
        history.append({"comment": previous_comment, "source": "previous_active_comment"})
        known_comments.add(previous_comment)
    if user_comment and user_comment not in known_comments:
        history.append({"comment": user_comment, "source": str(source.get("caption_update_source") or "pdf_upload")})
    pdf_source = {
        **previous_source,
        **source,
        "source": "pdf_extraction",
        "pdf_file": pdf_path.name,
        "pdf_sha256": pdf_digest(pdf_path),
        "pdf_extraction_method": extraction.method,
        "pdf_extraction_quality": extraction.quality,
        "pdf_page_count": extraction.page_count,
        "pdf_pages_processed": extraction.pages_processed,
        "pdf_character_count": extraction.character_count,
        "user_comment_history": history,
    }
    pdf_source.pop("pdf_extraction_status", None)
    saved = save_memory(
        raw_text,
        cfg,
        pdf_source,
        target_path=target_path,
        enrich_urls=False,
    )
    return saved, extraction


def likely_pdf_caption_duplicate(path: Path, captions: tuple[str, ...]) -> bool:
    try:
        content = path.read_text(encoding="utf-8").lower()
    except UnicodeDecodeError:
        return False
    normalized = lambda value: set(re.findall(r"[a-z0-9]+", value.lower()))
    words = normalized(content)
    return all(len(normalized(caption) & words) >= max(3, len(normalized(caption)) - 2) for caption in captions)
