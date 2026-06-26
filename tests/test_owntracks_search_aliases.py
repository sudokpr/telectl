from __future__ import annotations

import json
from pathlib import Path

from owntracks.search_aliases import (
    DEFAULT_SEARCH_ALIASES,
    load_search_aliases,
    merge_aliases,
    parse_alias_response,
    save_generated_aliases,
    search_alias_metadata,
    stop_index_evidence,
)


def test_alias_normalization_and_merge_preserves_generated_and_local_terms(tmp_path: Path) -> None:
    generated = tmp_path / "generated.json"
    local = tmp_path / "local.json"
    generated.write_text(json.dumps({"medical": {"terms": ["Doctor", "Clinic"]}, "cycling": ["Cycle"]}), encoding="utf-8")
    local.write_text(json.dumps({"medical": ["Hospital"], "family": ["Parents"]}), encoding="utf-8")

    aliases = load_search_aliases(
        {
            "OWNTRACKS_SEARCH_ALIASES_GENERATED_PATH": str(generated),
            "OWNTRACKS_SEARCH_ALIASES_LOCAL_PATH": str(local),
        }
    )

    assert "medical" in aliases
    assert "doctor" in aliases["medical"]
    assert "clinic" in aliases["medical"]
    assert "hospital" in aliases["medical"]
    assert aliases["family"] == ["parents"]


def test_parse_alias_response_accepts_json_wrapped_in_markdown() -> None:
    aliases = parse_alias_response(
        """
        ```json
        {
          "Medical": ["Doctor", "Clinic"],
          "Cycling": {"terms": ["Cycle", "Bike"]}
        }
        ```
        """
    )

    assert aliases == {
        "cycling": ["cycle", "bike"],
        "medical": ["doctor", "clinic"],
    }


def test_stop_index_evidence_extracts_terms_without_coordinates() -> None:
    summary = {
        "scope": {"start": "2026-06-01", "end": "2026-06-30"},
        "stats": {"places": 1, "visits": 1},
        "places": [
            {
                "name": "Doctor",
                "visit_count": 1,
                "total_minutes": 30,
                "tags": ["health"],
                "visits": [
                    {
                        "raw_name": "Aster Clinic",
                        "motion_mode": "stationary",
                        "tags": ["checkup"],
                        "note": "Annual checkup",
                    }
                ],
            }
        ],
    }

    evidence = stop_index_evidence(summary)
    terms = {item["term"] for item in evidence["terms"]}

    assert {"doctor", "aster clinic", "health", "checkup"} <= terms


def test_save_generated_aliases_writes_normalized_json(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"

    save_generated_aliases(path, {"Medical": ["Doctor", "Doctor", "Clinic"]})

    assert json.loads(path.read_text(encoding="utf-8")) == {"medical": ["doctor", "clinic"]}


def test_search_alias_metadata_reports_generated_file_mtime(tmp_path: Path) -> None:
    generated = tmp_path / "generated.json"
    local = tmp_path / "local.json"

    save_generated_aliases(generated, {"Medical": ["Doctor", "Clinic"]})
    metadata = search_alias_metadata(
        {
            "OWNTRACKS_SEARCH_ALIASES_GENERATED_PATH": str(generated),
            "OWNTRACKS_SEARCH_ALIASES_LOCAL_PATH": str(local),
        }
    )

    assert metadata["generated"]["exists"] is True
    assert metadata["generated"]["updated_at"]
    assert metadata["categories"] >= 1
    assert metadata["terms"] >= 2


def test_default_aliases_cover_cycle_and_clinic_terms() -> None:
    aliases = merge_aliases(DEFAULT_SEARCH_ALIASES)

    assert "cycle" in aliases["cycling"]
    assert "clinic" in aliases["medical"]
