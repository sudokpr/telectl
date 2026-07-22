from __future__ import annotations

from pathlib import Path

import fuel_tracker
from fuel_tracker import build_fuel_config, extract_fuel_fields, normalize_date


def test_normalize_date_accepts_two_digit_receipt_year() -> None:
    assert normalize_date("26/05/26") == "2026-05-26"


def test_fuel_config_defaults_to_codex() -> None:
    cfg = build_fuel_config({}, fallback_chat_id=123)

    assert cfg.llm_provider == "codex"
    assert cfg.model == "minicpm-v:latest"


def test_fuel_config_can_use_ollama() -> None:
    cfg = build_fuel_config({"FUEL_LLM_PROVIDER": "ollama", "FUEL_MODEL": "local-vision"}, fallback_chat_id=123)

    assert cfg.llm_provider == "ollama"
    assert cfg.model == "local-vision"


def test_extract_fuel_fields_uses_codex_images(tmp_path: Path, monkeypatch) -> None:
    receipt = tmp_path / "receipt.jpg"
    odo = tmp_path / "odo.jpg"
    receipt.write_bytes(b"receipt")
    odo.write_bytes(b"odo")
    cfg = build_fuel_config({}, fallback_chat_id=123)
    seen: dict[str, object] = {}

    def fake_ask_codex_image(prompt: str, image_paths: list[Path], codex_cfg: object) -> str:
        seen["prompt"] = prompt
        seen["image_paths"] = image_paths
        seen["codex_cfg"] = codex_cfg
        return """
        {
          "date": "24/06/2026",
          "time": "17:45",
          "odometer_km": "071234",
          "fuel_volume_l": "40.500 L",
          "fuel_rate": "101.25",
          "total_amount": "4100.62",
          "station": "Test Fuel",
          "fuel_type": "Petrol",
          "receipt_no": "R123",
          "notes": ""
        }
        """

    monkeypatch.setattr(fuel_tracker, "ask_codex_image", fake_ask_codex_image)

    extracted = extract_fuel_fields([receipt, odo], cfg)

    assert seen["image_paths"] == [receipt, odo]
    assert seen["codex_cfg"] == cfg.codex_llm_config
    assert "fuel receipt" in str(seen["prompt"])
    assert extracted == {
        "date": "2026-06-24",
        "time": "17:45",
        "odometer_km": "71234",
        "fuel_volume_l": "40.5",
        "fuel_rate": "101.25",
        "total_amount": "4100.62",
        "station": "Test Fuel",
        "fuel_type": "Petrol",
        "receipt_no": "R123",
        "notes": "",
    }
