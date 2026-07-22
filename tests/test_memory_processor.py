from pathlib import Path
from types import SimpleNamespace

from memory_processor import (
    MemoryQueryTurn,
    answer_memory_question,
    memories_with_history,
    query_terms,
    relevant_memories,
)


def test_multiword_coverage_beats_repetition_of_one_term(tmp_path: Path) -> None:
    (tmp_path / "service.md").write_text("oil " * 20, encoding="utf-8")
    grocery = tmp_path / "grocery.md"
    grocery.write_text("FORTUNE OIL 1L", encoding="utf-8")
    cfg = SimpleNamespace(memory_dir=tmp_path, memory_query_top_k=2)

    results = relevant_memories("fortune oil", cfg)

    assert results[0][0] == grocery


def test_query_terms_preserve_amounts_and_normalize_natural_dates() -> None:
    terms = query_terms("Where was Rs.30 spent on 18th July 2026?")

    assert "30" in terms
    assert "18" in terms
    assert "20260718" in terms


def test_relevant_memories_match_compact_query_amount_to_formatted_amount(tmp_path: Path) -> None:
    expected = tmp_path / "owntracks-poi-20260709-bank.md"
    expected.write_text("Debited for Rs.9,999.00 on a route", encoding="utf-8")
    (tmp_path / "service.md").write_text("9th service in July 2026", encoding="utf-8")
    cfg = SimpleNamespace(memory_dir=tmp_path, memory_query_top_k=1)

    matches = relevant_memories("Where was Rs.9999 spent on 9th July 2026?", cfg)

    assert matches[0][0] == expected


def test_followup_query_reuses_previous_turn_sources(tmp_path: Path, monkeypatch) -> None:
    receipt = tmp_path / "broccoli-receipt.md"
    receipt.write_text(
        "Date: 20/07/2026\nBROOKLY means broccoli\nCARROT 44.10\nBANANA 56.38",
        encoding="utf-8",
    )
    unrelated = tmp_path / "day-note.md"
    unrelated.write_text("A note about another day", encoding="utf-8")
    cfg = SimpleNamespace(
        memory_dir=tmp_path,
        memory_query_top_k=1,
        memory_query_max_context_chars=14000,
        memory_query_model="test-model",
    )
    history = (
        MemoryQueryTurn(
            question="What was the broccoli price?",
            answer="Broccoli cost 66.60 on 20/07/2026.",
            context_paths=(receipt,),
        ),
    )
    captured = {}

    def fake_text_llm(_cfg, _model, prompt, _purpose):
        captured["prompt"] = prompt
        return "You also bought carrot and banana."

    monkeypatch.setattr("memory_processor.text_llm_chat", fake_text_llm)

    result = answer_memory_question("What all did I buy on the same day?", cfg, history)

    assert result.context_paths[0] == receipt
    assert "What was the broccoli price?" in captured["prompt"]
    assert "Broccoli cost 66.60 on 20/07/2026." in captured["prompt"]
    assert "BROOKLY means broccoli" in captured["prompt"]


def test_history_source_keeps_current_keyword_score(tmp_path: Path) -> None:
    receipt = tmp_path / "broccoli-receipt.md"
    receipt.write_text("Broccoli 66.60", encoding="utf-8")
    cfg = SimpleNamespace(memory_dir=tmp_path, memory_query_top_k=1)
    history = (
        MemoryQueryTurn(question="Earlier?", answer="Earlier answer", context_paths=(receipt,)),
    )

    selected = memories_with_history("broccoli price", cfg, history)

    assert selected[0][0] == receipt
    assert selected[0][2] > 0


def test_memory_answer_includes_correlated_poi_context(tmp_path: Path, monkeypatch) -> None:
    receipt = tmp_path / "receipt.md"
    receipt.write_text("Broccoli 66.60", encoding="utf-8")
    cfg = SimpleNamespace(
        memory_dir=tmp_path,
        memory_query_top_k=1,
        memory_query_max_context_chars=14000,
        memory_query_model="test-model",
    )
    selected = [(receipt, receipt.read_text(encoding="utf-8"), 10)]
    captured = {}

    def fake_text_llm(_cfg, _model, prompt, _purpose):
        captured["prompt"] = prompt
        return "It was bought at Fruit Market."

    monkeypatch.setattr("memory_processor.text_llm_chat", fake_text_llm)

    answer_memory_question(
        "Where did I buy broccoli?",
        cfg,
        selected_memories=selected,
        correlated_poi_context="Place: Fruit Market\nPOI recorded at: 2026-07-20T15:03:00+05:30",
    )

    assert "CORRELATED POI CONTEXT" in captured["prompt"]
    assert "Place: Fruit Market" in captured["prompt"]


def test_memory_answer_validates_declared_sources_and_strips_protocol_line(tmp_path: Path, monkeypatch) -> None:
    relevant = tmp_path / "receipt.md"
    unrelated = tmp_path / "old-receipt.md"
    relevant.write_text("Broccoli receipt address: Teachers Colony", encoding="utf-8")
    unrelated.write_text("Older grocery receipt", encoding="utf-8")
    cfg = SimpleNamespace(memory_query_max_context_chars=14000, memory_query_model="test-model")
    selected = [
        (relevant, relevant.read_text(encoding="utf-8"), 10),
        (unrelated, unrelated.read_text(encoding="utf-8"), 5),
    ]
    captured = {}

    def fake_text_llm(_cfg, _model, prompt, _purpose):
        captured["prompt"] = prompt
        return "The printed receipt address is Teachers Colony.\nUSED_MEMORY_FILES: receipt.md"

    monkeypatch.setattr("memory_processor.text_llm_chat", fake_text_llm)

    result = answer_memory_question("Where did I buy broccoli?", cfg, selected_memories=selected)

    assert result.answer == "The printed receipt address is Teachers Colony."
    assert result.context_paths == (relevant,)
    assert "Do not write a Sources section" in captured["prompt"]
    assert "Do not call that the purchase location" in captured["prompt"]


def test_memory_answer_rejects_invented_source_path(tmp_path: Path, monkeypatch) -> None:
    receipt = tmp_path / "receipt.md"
    receipt.write_text("Receipt", encoding="utf-8")
    cfg = SimpleNamespace(memory_query_max_context_chars=14000, memory_query_model="test-model")
    selected = [(receipt, "Receipt", 10)]
    monkeypatch.setattr(
        "memory_processor.text_llm_chat",
        lambda *_args: (
            "Answer.\nMemory file used: [receipt.md](/home/kp/wrong/receipt.md)\n"
            "Sources:\n- [receipt.md](/home/kp/wrong/receipt.md)\n"
            "USED_MEMORY_FILES: /invented/path/receipt.md"
        ),
    )

    result = answer_memory_question("Question", cfg, selected_memories=selected)

    assert result.answer == "Answer."
    assert result.context_paths == (receipt,)
