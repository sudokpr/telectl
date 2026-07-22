from pathlib import Path

from bot import memory_retrieval_report, visible_answer_stream


def test_retrieval_report_shows_terms_ranking_and_history() -> None:
    selected = [
        (Path("receipt.md"), "Broccoli", 27),
        (Path("previous.md"), "Earlier context", 0),
    ]

    report = memory_retrieval_report("broccoli price", selected)

    assert "Query terms: broccoli, price" in report
    assert "1. receipt.md — score 27" in report
    assert "Reused from recent query context" in report
    assert "- previous.md" in report


def test_visible_stream_hides_source_protocol_and_partial_marker() -> None:
    assert visible_answer_stream("The answer.\nUSED_MEMORY_FILES: receipt.md", 3600) == "The answer."
    assert visible_answer_stream("The answer.\nUSED_MEM", 3600) == "The answer."
