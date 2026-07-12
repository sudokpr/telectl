from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import spending_index
from owntracks.tagger import parse_log
from spending_index import SpendingConfig, index_scope, nearby_location, query_spending


def make_cfg(tmp_path: Path) -> SpendingConfig:
    return SpendingConfig(
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


def write_log(path: Path) -> None:
    rows = [
        (
            "2026-07-07T10:00:00+0530",
            "owntracks/user/device",
            {
                "_type": "location",
                "tst": 1783407600,
                "lat": 12.9716,
                "lon": 77.5946,
                "desc": "Fruit Market",
                "inregions": ["Fruit Market"],
            },
        ),
        (
            "2026-07-07T10:05:00+0530",
            "owntracks/user/device",
            {
                "_type": "location",
                "tst": 1783407900,
                "lat": 12.9717,
                "lon": 77.5947,
                "poi": "Paid Rs.450 at Fruit Market on 07/07/2026\nApples 2 kg 120 240\nPizza 1 pc 210",
            },
        ),
        (
            "2026-07-08T12:00:00+0530",
            "owntracks/user/device",
            {
                "_type": "location",
                "tst": 1783501200,
                "lat": 12.9717,
                "lon": 77.5947,
                "poi": "Restaurant bill INR 300 on 2026-07-08\nPizza 1 pc 300",
            },
        ),
    ]
    path.write_text("\n".join(f"{stamp} {topic} {json.dumps(payload)}" for stamp, topic, payload in rows) + "\n")


def test_index_and_query_spending_pois(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    write_log(cfg.owntracks_log_path)
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log")

    result = index_scope(cfg, image_cfg, "2026-07")

    assert result.scanned == 2
    assert result.indexed == 2
    answer = query_spending(cfg, "Where was Rs.450 spent on 7th July 2026?")
    assert "Fruit Market" in answer
    assert "INR 450.00" in answer

    avg = query_spending(cfg, "what is the avg price of pizza I paid in 2026?")
    assert "Average indexed price for pizza" in avg
    assert "255.00" in avg

    last = query_spending(cfg, "what is the last price of apples per kg?")
    assert "Apples" in last
    assert "120.00" in last


def test_location_context_uses_trip_segment_and_ignores_other_poi_labels(tmp_path: Path, monkeypatch) -> None:
    cfg = make_cfg(tmp_path)
    rows = [
        (
            "2026-07-09T10:00:00+0530",
            "owntracks/user/device",
            {"_type": "location", "tst": 1783571400, "lat": 12.0, "lon": 77.0, "poi": "Previous bank SMS Rs.10"},
        ),
        (
            "2026-07-09T10:30:00+0530",
            "owntracks/user/device",
            {"_type": "location", "tst": 1783573200, "lat": 12.1, "lon": 77.1, "poi": "Paid Rs.9999"},
        ),
    ]
    cfg.owntracks_log_path.write_text(
        "\n".join(f"{stamp} {topic} {json.dumps(payload)}" for stamp, topic, payload in rows) + "\n"
    )
    events = parse_log(cfg.owntracks_log_path, ZoneInfo("Asia/Kolkata"))

    def fake_build_plan(_events, _target_date, _user_tags):
        return {
            "candidate_stops": [],
            "travel_segments": [
                {
                    "label": "Home to Office",
                    "start_timestamp": 1783572000,
                    "end_timestamp": 1783575000,
                }
            ],
        }, []

    monkeypatch.setattr(spending_index, "build_plan", fake_build_plan)

    location = nearby_location(events[1], events, cfg, {}, {})

    assert location is not None
    assert location["label"] == "en route: Home to Office"
