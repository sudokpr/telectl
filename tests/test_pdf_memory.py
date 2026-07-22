from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

import pdf_memory
from pdf_memory import PdfExtraction, extract_pdf_text, save_pdf_memory


def pdf_cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        work_dir=tmp_path / "work",
        memory_dir=tmp_path / "memories",
        log_file=tmp_path / "worker.log",
        ocr_command="tesseract",
        ocr_lang="eng",
        ocr_psm="6",
        memory_pdf_max_pages=5,
        memory_pdf_render_scale=2.5,
    )


def test_scanned_pdf_uses_tesseract_ocr(tmp_path: Path) -> None:
    image = Image.new("RGB", (1400, 500), "white")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 54)
    draw = ImageDraw.Draw(image)
    draw.text((70, 90), "MARLIN YETI MTB PURCHASE RECEIPT", fill="black", font=font)
    draw.text((70, 190), "TOTAL INR 48000", fill="black", font=font)
    pdf_path = tmp_path / "receipt.pdf"
    image.save(pdf_path, "PDF", resolution=150)

    extraction = extract_pdf_text(pdf_path, pdf_cfg(tmp_path))

    assert extraction.method == "tesseract_ocr"
    assert extraction.quality in {"medium", "high"}
    assert "MARLIN YETI" in extraction.text.upper()
    assert "48000" in extraction.text


def test_pdf_hash_deduplicates_and_new_caption_updates_same_memory(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "receipt.pdf"
    pdf_path.write_bytes(b"stable PDF bytes")
    cfg = pdf_cfg(tmp_path)
    extraction = PdfExtraction("Receipt total 100", "embedded_text", "high", 1, 1, 17)
    monkeypatch.setattr(pdf_memory, "extract_pdf_text", lambda *_args: extraction)

    def fake_save(raw_text, _cfg, source, target_path=None, **_kwargs):
        path = target_path or cfg.memory_dir / "receipt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"source: {json.dumps(source)}\n{raw_text}"
        path.write_text(content, encoding="utf-8")
        return SimpleNamespace(path=path, content=content)

    monkeypatch.setattr(pdf_memory, "save_memory", fake_save)

    first, _ = save_pdf_memory(pdf_path, cfg, {"user_comment": "Bought preowned"})
    duplicate, _ = save_pdf_memory(pdf_path, cfg, {})
    updated, _ = save_pdf_memory(pdf_path, cfg, {"user_comment": "Bought Marlin Yeti MTB preowned"})

    assert first is not None
    assert duplicate is None
    assert updated is not None
    assert updated.path == first.path
    assert "Bought Marlin Yeti MTB preowned" in updated.content
    assert len(list(cfg.memory_dir.glob("*.md"))) == 1


def test_pdf_reupload_upgrades_matching_legacy_caption_memory(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "receipt.pdf"
    pdf_path.write_bytes(b"newly available PDF bytes")
    cfg = pdf_cfg(tmp_path)
    cfg.memory_dir.mkdir()
    legacy = cfg.memory_dir / "marlin-yeti.md"
    legacy.write_text(
        'source: {"pdf_extraction_status": "attachment_not_downloaded_by_legacy_flow"}\n'
        "Bought Marlin Yeti MTB cycle preowned",
        encoding="utf-8",
    )
    extraction = PdfExtraction("Receipt total 48000", "tesseract_ocr", "high", 1, 1, 19)
    monkeypatch.setattr(pdf_memory, "extract_pdf_text", lambda *_args: extraction)

    def fake_save(raw_text, _cfg, source, target_path=None, **_kwargs):
        assert target_path == legacy
        content = f"source: {json.dumps(source)}\n{raw_text}"
        target_path.write_text(content, encoding="utf-8")
        return SimpleNamespace(path=target_path, content=content)

    monkeypatch.setattr(pdf_memory, "save_memory", fake_save)

    saved, _ = save_pdf_memory(pdf_path, cfg, {"user_comment": "Bought Marlin Yeti MTB cycle preowned"})

    assert saved is not None
    assert saved.path == legacy
    assert "pdf_sha256" in saved.content
    assert len(list(cfg.memory_dir.glob("*.md"))) == 1


def test_pdf_reply_message_id_upgrades_legacy_memory_with_different_caption(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "receipt.pdf"
    pdf_path.write_bytes(b"recovered legacy PDF")
    cfg = pdf_cfg(tmp_path)
    cfg.memory_dir.mkdir()
    legacy = cfg.memory_dir / "marlin-yeti.md"
    legacy.write_text(
        'source: {"telegram_message_ids": [1239, 1242], '
        '"pdf_extraction_status": "attachment_not_downloaded_by_legacy_flow"}\n'
        "Bought Marlin Yeti MTB cycle preowned",
        encoding="utf-8",
    )
    extraction = PdfExtraction("Seller receipt total 48000", "embedded_text", "high", 1, 1, 26)
    monkeypatch.setattr(pdf_memory, "extract_pdf_text", lambda *_args: extraction)
    captured = {}

    def fake_save(raw_text, _cfg, source, target_path=None, **_kwargs):
        captured.update(raw_text=raw_text, source=source, target_path=target_path)
        content = f"source: {json.dumps(source)}\n{raw_text}"
        target_path.write_text(content, encoding="utf-8")
        return SimpleNamespace(path=target_path, content=content)

    monkeypatch.setattr(pdf_memory, "save_memory", fake_save)

    saved, _ = save_pdf_memory(
        pdf_path,
        cfg,
        {
            "user_comment": "The seller included the rear light",
            "caption_reply_to_message_id": 1242,
            "caption_update_source": "telegram_reply",
        },
    )

    assert saved is not None
    assert captured["target_path"] == legacy
    assert captured["source"]["telegram_message_ids"] == [1239, 1242]
    assert "pdf_extraction_status" not in captured["source"]
    assert captured["source"]["user_comment_history"][-1]["comment"] == "The seller included the rear light"
