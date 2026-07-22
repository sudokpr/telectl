from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from image_summary import build_config, image_result_jobs, processing_description
from memory_processor import has_image_memory, save_image_memory, select_image_extraction, update_image_memory_comment


def test_select_image_extraction_prefers_codex_then_raw_ocr() -> None:
    results = [
        {"label": "Direct vision LLM (local)", "ok": True, "value": "local summary"},
        {"label": "OCR + LLM (model)", "ok": True, "value": ("raw OCR rows", "summary")},
        {"label": "Codex benchmark text (model)", "ok": True, "value": "all Codex rows"},
    ]
    assert select_image_extraction(results) == ("Codex benchmark text (model)", "all Codex rows")

    results[-1]["ok"] = False
    assert select_image_extraction(results) == ("OCR + LLM (model)", "raw OCR rows")


def test_save_image_memory_records_digest_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    image_path = tmp_path / "receipt.jpg"
    image_path.write_bytes(b"receipt image")
    cfg = SimpleNamespace(memory_dir=tmp_path / "memories")

    def fake_save_memory(raw_text, _cfg, source, **_kwargs):
        path = cfg.memory_dir / "receipt.md"
        path.parent.mkdir()
        path.write_text(f"source: {source!r}\n{raw_text}", encoding="utf-8")
        return SimpleNamespace(path=path, content=path.read_text(encoding="utf-8"))

    monkeypatch.setattr("memory_processor.save_memory", fake_save_memory)
    results = [{"label": "Codex benchmark text (model)", "ok": True, "value": "Sugar 1kg"}]

    saved = save_image_memory(image_path, results, cfg, {"telegram_message_id": 42})
    assert saved is not None
    assert "Sugar 1kg" in saved.content
    assert "image_sha256" in saved.content
    assert has_image_memory(image_path, cfg)
    assert save_image_memory(image_path, results, cfg, {}) is None


def test_save_image_memory_falls_back_to_tesseract(tmp_path: Path, monkeypatch) -> None:
    image_path = tmp_path / "receipt.jpg"
    image_path.write_bytes(b"receipt image")
    cfg = SimpleNamespace(memory_dir=tmp_path / "memories")
    captured = {}

    monkeypatch.setattr("memory_processor.run_ocr", lambda *_args: "Milk 1 20.00")

    def fake_save_memory(raw_text, _cfg, source, **_kwargs):
        captured.update(raw_text=raw_text, source=source)
        return SimpleNamespace(path=tmp_path / "memory.md", content=raw_text)

    monkeypatch.setattr("memory_processor.save_memory", fake_save_memory)
    saved = save_image_memory(image_path, [], cfg, {})
    assert saved is not None
    assert captured["raw_text"] == "Milk 1 20.00"
    assert captured["source"]["extraction_label"] == "Tesseract OCR fallback"


def test_ollama_can_be_disabled_for_image_jobs(tmp_path: Path) -> None:
    cfg = build_config(
        {
            "IMAGE_SUMMARY_MODE": "compare",
            "IMAGE_SUMMARY_WORK_DIR": str(tmp_path),
            "CODEX_LLM_ENABLED": "true",
            "CODEX_LLM_MODEL": "test-model",
            "OLLAMA_ENABLED": "false",
        },
        0,
    )
    labels = [label for label, _job in image_result_jobs(tmp_path / "image.jpg", cfg)]
    assert labels == ["Codex benchmark text (test-model)"]
    assert processing_description(cfg) == "Codex vision"


def test_memory_query_feedback_flags_are_opt_in(tmp_path: Path) -> None:
    default_cfg = build_config({"IMAGE_SUMMARY_WORK_DIR": str(tmp_path)}, 0)
    assert default_cfg.image_summary_stream is False
    assert default_cfg.memory_query_show_retrieval is False
    assert default_cfg.memory_query_stream is False

    enabled_cfg = build_config(
        {
            "IMAGE_SUMMARY_WORK_DIR": str(tmp_path),
            "IMAGE_SUMMARY_STREAM": "true",
            "MEMORY_QUERY_SHOW_RETRIEVAL": "true",
            "MEMORY_QUERY_STREAM": "true",
        },
        0,
    )
    assert enabled_cfg.image_summary_stream is True
    assert enabled_cfg.memory_query_show_retrieval is True
    assert enabled_cfg.memory_query_stream is True


def test_image_caption_is_passed_to_codex_vision(tmp_path: Path, monkeypatch) -> None:
    cfg = build_config(
        {
            "IMAGE_SUMMARY_MODE": "vision",
            "IMAGE_SUMMARY_WORK_DIR": str(tmp_path),
            "CODEX_LLM_ENABLED": "true",
            "OLLAMA_ENABLED": "false",
        },
        0,
    )
    captured = {}
    streamed: list[str] = []

    def fake_vision(_path, _cfg, user_comment=None, on_text_delta=None):
        captured["comment"] = user_comment
        if on_text_delta:
            on_text_delta("BROOKLY ")
        return "BROOKLY means broccoli"

    monkeypatch.setattr("image_summary.summarize_codex_vision", fake_vision)
    _label, job = image_result_jobs(
        tmp_path / "receipt.jpg",
        cfg,
        "Broccoli is misspelt as brookly in the receipt.",
        on_text_delta=streamed.append,
    )[0]

    result = job()

    assert result["ok"] is True
    assert captured["comment"] == "Broccoli is misspelt as brookly in the receipt."
    assert streamed == ["BROOKLY "]


def test_new_caption_updates_existing_image_memory(tmp_path: Path, monkeypatch) -> None:
    image_path = tmp_path / "receipt.jpg"
    image_path.write_bytes(b"same image")
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    existing_path = memory_dir / "receipt.md"

    from memory_processor import image_digest

    existing_path.write_text(f'image_sha256: "{image_digest(image_path)}"\nBROOKLY', encoding="utf-8")
    cfg = SimpleNamespace(memory_dir=memory_dir)
    captured = {}

    def fake_save_memory(raw_text, _cfg, source, target_path=None, **_kwargs):
        captured.update(raw_text=raw_text, source=source, target_path=target_path)
        return SimpleNamespace(path=target_path, content=raw_text)

    monkeypatch.setattr("memory_processor.save_memory", fake_save_memory)
    results = [{"label": "Codex benchmark text (model)", "ok": True, "value": "BROOKLY 0.370"}]

    saved = save_image_memory(
        image_path,
        results,
        cfg,
        {"user_comment": "BROOKLY means broccoli."},
    )

    assert saved is not None
    assert captured["target_path"] == existing_path
    assert "User-supplied image comment:\nBROOKLY means broccoli." in captured["raw_text"]
    assert captured["source"]["user_comment"] == "BROOKLY means broccoli."


def test_caption_capture_id_is_saved_as_source_metadata(tmp_path: Path, monkeypatch) -> None:
    image_path = tmp_path / "receipt.jpg"
    image_path.write_bytes(b"receipt image")
    cfg = SimpleNamespace(memory_dir=tmp_path / "memories")
    captured = {}

    def fake_save_memory(raw_text, _cfg, source, **_kwargs):
        captured.update(raw_text=raw_text, source=source)
        return SimpleNamespace(path=tmp_path / "memory.md", content=raw_text)

    monkeypatch.setattr("memory_processor.save_memory", fake_save_memory)
    results = [{"label": "Codex benchmark text (model)", "ok": True, "value": "Receipt 100"}]

    save_image_memory(
        image_path,
        results,
        cfg,
        {"user_comment": "capture_id: B3ADCA44-2F72-4E42-8729-D262FC55DF77"},
    )

    assert captured["source"]["capture_id"] == "b3adca44-2f72-4e42-8729-d262fc55df77"


def test_reply_caption_updates_existing_image_memory_without_rerunning_image_extraction(tmp_path: Path, monkeypatch) -> None:
    image_path = tmp_path / "receipt.jpg"
    image_path.write_bytes(b"same receipt")
    from memory_processor import image_digest

    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    existing = memory_dir / "receipt.md"
    existing.write_text(
        "---\n"
        f'source: {{"image_sha256": "{image_digest(image_path)}", "extraction_label": "Codex benchmark", '
        '"user_comment": "Old broccoli comment"}\n'
        "---\n\n# Receipt\n\n## Raw Text\n\n```text\nCORINDER 25.00\n\n"
        "User-supplied image comment:\nOld broccoli comment\n```\n",
        encoding="utf-8",
    )
    cfg = SimpleNamespace(memory_dir=memory_dir)
    captured = {}

    def fake_save(raw_text, _cfg, source, target_path=None, **_kwargs):
        captured.update(raw_text=raw_text, source=source, target_path=target_path)
        return SimpleNamespace(path=target_path, content=raw_text)

    monkeypatch.setattr("memory_processor.save_memory", fake_save)

    saved = update_image_memory_comment(
        image_path,
        cfg,
        "Coriander is misspelt in the receipt",
        {"telegram_message_id": 1270, "caption_reply_to_message_id": 1247},
    )

    assert saved is not None
    assert captured["target_path"] == existing
    assert "CORINDER 25.00" in captured["raw_text"]
    assert "Old broccoli comment" not in captured["raw_text"]
    assert "Coriander is misspelt" in captured["raw_text"]
    assert captured["source"]["caption_update_source"] == "telegram_reply"
    assert captured["source"]["extraction_label"] == "Codex benchmark"
    assert [item["comment"] for item in captured["source"]["user_comment_history"]] == [
        "Old broccoli comment",
        "Coriander is misspelt in the receipt",
    ]
