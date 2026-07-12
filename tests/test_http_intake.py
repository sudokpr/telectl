from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from http_intake import HttpIntakeConfig, make_handler
from metrics import MetricsConfig, http_route
from spending_index import SpendingConfig


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
        "http_intake.generate_trips",
        lambda date_text=None, origin_key=None, destination_key=None: (
            {"date": date_text, "origin": origin_key, "destination": destination_key},
            "<!doctype html><title>OwnTracks trips</title>",
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
        owntracks_media_dir=str(tmp_path / "media"),
    )
    spending_cfg = SpendingConfig(
        enabled=True,
        db_path=tmp_path / "spending.sqlite",
        evidence_dir=tmp_path / "evidence",
        owntracks_log_path=tmp_path / "mqtt.log",
        user_tags_path=tmp_path / "user_tags.json",
        poll_seconds=60,
        index_images=False,
        max_image_bytes=1024,
        nearest_stop_radius_m=300,
        nearest_stop_time_window_minutes=180,
    )
    handler = make_handler(SimpleNamespace(), cfg, http_cfg, MetricsConfig(False, "127.0.0.1", 0), spending_cfg, None)

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

        with urlopen(f"{base_url}/owntracks/trips?token=test-token&date=2026-06-28&from=name%3Ahome&to=name%3Asugganahalli%20land") as response:
            assert response.status == 200
            assert "OwnTracks trips" in response.read().decode()

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
                    "tags": ["errand"],
                    "note": "Saved from route point",
                    "place": True,
                    "entry_time": "11:55",
                    "exit_time": "~12:20",
                    "radius_m": 240,
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
                    "exit_time": "~12:30",
                    "radius_m": 250,
                    "place": True,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(review_request) as response:
            assert response.status == 200
            assert json.load(response)["ok"] is True

        boundary = "----telegram-control-test"
        media_body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"date\"\r\n\r\n2026-06-19\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"id\"\r\n\r\nmanual-stop-42\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"lat\"\r\n\r\n12.9716\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"lon\"\r\n\r\n77.5946\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\nRunning certificate\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"certificate.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\nfake-jpeg\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        media_request = Request(
            f"{base_url}/owntracks/media?token=test-token",
            data=media_body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urlopen(media_request) as response:
            media_payload = json.load(response)
            assert response.status == 201
            assert media_payload["media"]["caption"] == "Running certificate"
            assert media_payload["media"]["kind"] == "image"
        media_id = media_payload["media"]["id"]
        media_filename = media_payload["media"]["filename"]

        with urlopen(f"{base_url}/owntracks/media/2026-06-19/{media_filename}?token=test-token") as response:
            assert response.status == 200
            assert response.read() == b"fake-jpeg"

        delete_media_request = Request(
            f"{base_url}/owntracks/media?token=test-token",
            data=json.dumps({"date": "2026-06-19", "id": "manual-stop-42", "media_id": media_id}).encode(),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with urlopen(delete_media_request) as response:
            assert response.status == 200
            assert json.load(response)["media_id"] == media_id

        dismiss_request = Request(
            f"{base_url}/owntracks/stops?token=test-token",
            data=json.dumps(
                {
                    "date": "2026-06-19",
                    "id": "unnamed-stop-9-99",
                    "lat": 12.9816,
                    "lon": 77.6046,
                    "ignored": True,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(dismiss_request) as response:
            payload = json.load(response)
            assert response.status == 200
            assert payload["ignored"] is True

        saved_tags = json.loads((tmp_path / "user_tags.json").read_text())
        saved_stop = saved_tags["2026-06-19"]["stops"]["manual-stop-42"]
        assert saved_stop["name"] == "Edited stop"
        assert saved_stop["tags"] == ["errand", "short-visit"]
        assert saved_stop["note"] == "Saved directly from the map"
        assert saved_stop["place"] is True
        assert saved_stop["entry_time"] == "11:55"
        assert saved_stop["exit_time"] == "~12:30"
        assert saved_stop["radius_m"] == 250
        assert saved_tags["2026-06-19"]["stops"]["unnamed-stop-9-99"]["ignored"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_owntracks_http_routes_are_normalized_for_metrics() -> None:
    assert http_route("/owntracks/stops?token=secret") == "/owntracks/stops"
    assert http_route("/owntracks/stops.html") == "/owntracks/stops"
    assert http_route("/owntracks/dashboard?start=2026-06-01") == "/owntracks/dashboard"
    assert http_route("/owntracks/trips?date=2026-06-28") == "/owntracks/trips"
    assert http_route("/owntracks/search-aliases") == "/owntracks/search-aliases"
    assert http_route("/owntracks/media?token=secret") == "/owntracks/media"
    assert http_route("/owntracks/media/2026-06-19/photo.jpg?token=secret") == "/owntracks/media/:file"
    assert http_route("/owntracks/map/2026-06-28?filter=walking") == "/owntracks/map/:scope"
