from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from http_intake import HttpIntakeConfig, make_handler
from metrics import MetricsConfig


def test_health_memory_and_manual_stop_endpoints(tmp_path: Path, monkeypatch) -> None:
    saved_memory = SimpleNamespace(path=tmp_path / "memory.md", content="# Memory")
    monkeypatch.setattr("http_intake.save_memory", lambda *_args, **_kwargs: saved_memory)
    monkeypatch.setattr(
        "http_intake.generate_stop_index",
        lambda start_text=None, end_text=None: (
            {"scope": {"start": start_text, "end": end_text}},
            "<!doctype html><title>OwnTracks stop index</title>",
        ),
    )
    monkeypatch.setattr(
        "http_intake.generate_activity_dashboard",
        lambda start_text=None, end_text=None: (
            {"scope": {"start": start_text, "end": end_text}},
            "<!doctype html><title>OwnTracks activity dashboard</title>",
        ),
    )
    monkeypatch.setattr(
        "http_intake.generate_search_aliases",
        lambda start_text=None, end_text=None: ({"medical": ["doctor", "clinic"]}, tmp_path / "aliases.json"),
    )
    cfg = SimpleNamespace(max_reply_chars=4000, log_file=tmp_path / "http.log")
    http_cfg = HttpIntakeConfig(
        enabled=True,
        host="127.0.0.1",
        port=0,
        token="test-token",
        notify_telegram=False,
        fuel_csv_path=str(tmp_path / "fuel.csv"),
        owntracks_derived_dir=str(tmp_path / "derived"),
        owntracks_user_tags_path=str(tmp_path / "user_tags.json"),
    )
    handler = make_handler(SimpleNamespace(), cfg, http_cfg, MetricsConfig(False, "127.0.0.1", 0), None)

    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/health") as response:
            assert json.load(response) == {"ok": True}

        with urlopen(f"{base_url}/owntracks/stops?token=test-token&start=2026-06-01&end=2026-06-30") as response:
            assert response.status == 200
            assert "OwnTracks stop index" in response.read().decode()

        with urlopen(f"{base_url}/owntracks/dashboard?token=test-token&start=2026-06-01&end=2026-06-30") as response:
            assert response.status == 200
            assert "OwnTracks activity dashboard" in response.read().decode()

        aliases_request = Request(
            f"{base_url}/owntracks/search-aliases?token=test-token",
            data=json.dumps({"start": "2026-06-01", "end": "2026-06-30"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(aliases_request) as response:
            payload = json.load(response)
            assert response.status == 200
            assert payload["ok"] is True
            assert payload["categories"] == 1
            assert payload["terms"] == 2

        memory_request = Request(
            f"{base_url}/memory?token=test-token",
            data=json.dumps({"text": "sample memory"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(memory_request) as response:
            assert json.load(response)["ok"] is True

        stop_request = Request(
            f"{base_url}/owntracks/stops?token=test-token",
            data=json.dumps(
                {
                    "date": "2026-06-19",
                    "lat": 12.9716,
                    "lon": 77.5946,
                    "line": 42,
                    "timestamp": 1781884800,
                    "time": "12:00",
                    "motion_mode": "stationary",
                    "name": "Quick stop",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(stop_request) as response:
            payload = json.load(response)
            assert response.status == 201
            assert payload["id"] == "manual-stop-42"

        review_request = Request(
            f"{base_url}/owntracks/stops?token=test-token",
            data=json.dumps(
                {
                    "date": "2026-06-19",
                    "id": "manual-stop-42",
                    "lat": 12.9716,
                    "lon": 77.5946,
                    "name": "Edited stop",
                    "tags": ["errand", "short-visit"],
                    "note": "Saved directly from the map",
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(review_request) as response:
            assert response.status == 200
            assert json.load(response)["ok"] is True

        saved_tags = json.loads((tmp_path / "user_tags.json").read_text())
        saved_stop = saved_tags["2026-06-19"]["stops"]["manual-stop-42"]
        assert saved_stop["name"] == "Edited stop"
        assert saved_stop["tags"] == ["errand", "short-visit"]
        assert saved_stop["note"] == "Saved directly from the map"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
