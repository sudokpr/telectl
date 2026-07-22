from __future__ import annotations

import datetime as dt
import base64
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import spending_index
from owntracks.tagger import parse_log
from spending_index import (
    SpendingConfig,
    index_scope,
    memory_poi_context,
    nearby_location,
    query_spending,
    spending_poi_event,
)


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


def test_numbered_trail_note_is_not_spending(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    payload = {
        "_type": "location",
        "tst": 1783407900,
        "lat": 12.9717,
        "lon": 77.5947,
        "poi": "fav trail but its 5-10min back",
    }
    cfg.owntracks_log_path.write_text(
        f"2026-07-07T10:05:00+0530 owntracks/user/device {json.dumps(payload)}\n",
        encoding="utf-8",
    )
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log")

    result = index_scope(cfg, image_cfg, "2026-07-07")

    assert result.scanned == 1
    assert result.indexed == 0
    assert result.skipped == 1
    with sqlite3.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


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


def test_structured_poi_uses_ios_capture_context(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    structured = {
        "time ": "2026-07-17T11:48:36+05:30",
        "lat": 12.95900953072699,
        "lon": 77.5007669503874,
        "poi": "ICICI Bank Acct XX837 debited for Rs 9270.00 on 17-Jul-26; ZERODHA BROKING credited.",
    }
    envelope = {
        "_type": "location",
        "tst": 1784269529,
        "lat": 12.956949,
        "lon": 77.518101,
        "poi": json.dumps(structured, separators=(",", ":")),
    }
    cfg.owntracks_log_path.write_text(
        f"2026-07-17T12:03:06+0530 owntracks/user/device {json.dumps(envelope)}\n",
        encoding="utf-8",
    )
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log")

    result = index_scope(cfg, image_cfg, "2026-07-17")

    assert result.indexed == 1
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        event = conn.execute("SELECT * FROM events").fetchone()
        transaction = conn.execute("SELECT * FROM transactions").fetchone()
        location = conn.execute("SELECT * FROM location_matches").fetchone()
    assert event is not None
    assert event["recorded_at"] == "2026-07-17T11:48:36+05:30"
    assert event["received_at"] == "2026-07-17T12:03:06+05:30"
    assert event["lat"] == structured["lat"]
    assert event["lon"] == structured["lon"]
    assert json.loads(event["poi_text"])["poi"].startswith("ICICI Bank")
    assert event["extracted_text"] == structured["poi"]
    assert transaction is not None
    assert transaction["amount"] == 9270
    assert transaction["transaction_date"] == "2026-07-17"
    assert location is not None
    assert location["maps_url"] == "https://www.google.com/maps?q=12.959010,77.500767"


def test_structured_poi_scope_uses_capture_time_not_envelope_time(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    structured = {
        "time ": "2026-07-17T23:59:00+05:30",
        "lat": 12.95,
        "lon": 77.50,
        "poi": "Paid Rs 25.00 at Test Store",
    }
    envelope = {
        "_type": "location",
        "tst": int(dt.datetime.fromisoformat("2026-07-18T00:05:00+05:30").timestamp()),
        "lat": 13.0,
        "lon": 77.6,
        "poi": json.dumps(structured),
    }
    cfg.owntracks_log_path.write_text(
        f"2026-07-18T00:10:00+0530 owntracks/user/device {json.dumps(envelope)}\n",
        encoding="utf-8",
    )
    parsed = parse_log(cfg.owntracks_log_path, ZoneInfo("Asia/Kolkata"))[0]

    normalized = spending_poi_event(parsed)
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log")
    result = index_scope(cfg, image_cfg, "2026-07-17")

    assert result.indexed == 1
    assert normalized.recorded_at is not None
    assert normalized.recorded_at.isoformat(timespec="seconds") == "2026-07-17T23:59:00+05:30"
    assert normalized.lat == structured["lat"]
    assert normalized.lon == structured["lon"]
    assert normalized.payload["poi"] == structured["poi"]


def test_memory_links_to_poi_by_capture_id(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    capture_id = "b3adca44-2f72-4e42-8729-d262fc55df77"
    rows = [
        (
            "2026-07-20T15:00:00+0530",
            "owntracks/user/device",
            {"_type": "location", "tst": 1784549400, "lat": 12.95, "lon": 77.50, "desc": "Fruit Market"},
        ),
        (
            "2026-07-20T15:03:00+0530",
            "owntracks/user/device",
            {
                "_type": "location",
                "tst": 1784549580,
                "lat": 12.9501,
                "lon": 77.5001,
                "poi": json.dumps(
                    {
                        "capture_id": capture_id,
                        "time": "2026-07-20T15:03:00+05:30",
                        "lat": 12.9501,
                        "lon": 77.5001,
                        "poi": "Paid Rs.511.50 at GO GREEN on 20/07/2026",
                    }
                ),
            },
        ),
    ]
    cfg.owntracks_log_path.write_text(
        "\n".join(f"{stamp} {topic} {json.dumps(payload)}" for stamp, topic, payload in rows) + "\n",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    memory = memory_dir / "go-green.md"
    memory.write_text(
        f'---\nsource: {{"capture_id": "{capture_id}"}}\n---\n# GO GREEN\n\n## Key Fields\n\n'
        "- amount: 511.50\n- date: 20/07/2026\n- merchant: GO GREEN\n",
        encoding="utf-8",
    )
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log", memory_dir=memory_dir)

    result = index_scope(cfg, image_cfg, "2026-07-20")

    assert result.linked == 1
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        link = conn.execute("SELECT * FROM memory_poi_links").fetchone()
        event = conn.execute("SELECT * FROM events").fetchone()
    assert link is not None
    assert link["match_method"] == "capture_id"
    assert link["confidence"] == 1.0
    assert event["capture_id"] == capture_id
    context = memory_poi_context(cfg, (memory,))
    assert "POI capture/associated place: Fruit Market" in context
    assert "not by itself proof" in context
    assert "capture_id confidence=1.00" in context
    spending_answer = query_spending(cfg, "Where was Rs.511.50 spent on 20th July 2026?")
    assert "memory: go-green.md" in spending_answer


def test_memory_links_to_poi_by_image_sha256(tmp_path: Path) -> None:
    cfg = replace(make_cfg(tmp_path), index_images=True, max_image_bytes=4096)
    image_body = b"same receipt image bytes"
    image_sha = hashlib.sha256(image_body).hexdigest()
    payload = {
        "_type": "location",
        "tst": 1784549580,
        "lat": 12.95,
        "lon": 77.50,
        "poi": "Paid Rs.100 at Hash Store on 20/07/2026",
        "image": base64.b64encode(image_body).decode("ascii"),
    }
    cfg.owntracks_log_path.write_text(
        f"2026-07-20T15:03:00+0530 owntracks/user/device {json.dumps(payload)}\n",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / "hash-receipt.md").write_text(
        f'---\nsource: {{"image_sha256": "{image_sha}"}}\n---\n# Hash receipt\n',
        encoding="utf-8",
    )
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log", memory_dir=memory_dir)

    result = index_scope(cfg, image_cfg, "2026-07-20")

    assert result.linked == 1
    with sqlite3.connect(cfg.db_path) as conn:
        method = conn.execute("SELECT match_method FROM memory_poi_links").fetchone()[0]
    assert method == "image_sha256"


def test_memory_links_to_unique_poi_by_receipt_signature(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cfg.owntracks_log_path.write_text(
        "2026-07-07T10:05:00+0530 owntracks/user/device "
        + json.dumps(
            {
                "_type": "location",
                "tst": 1783407900,
                "lat": 12.97,
                "lon": 77.59,
                "poi": "Paid Rs.450 at Fruit Market on 07/07/2026",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / "fruit-market.md").write_text(
        "# Fruit Market receipt\n\n## Key Fields\n\n"
        "- amount: 450\n- date: 07/07/2026\n- merchant: Fruit Market\n",
        encoding="utf-8",
    )
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log", memory_dir=memory_dir)

    result = index_scope(cfg, image_cfg, "2026-07-07")

    assert result.linked == 1
    with sqlite3.connect(cfg.db_path) as conn:
        row = conn.execute("SELECT match_method, confidence FROM memory_poi_links").fetchone()
    assert row == ("merchant_amount_date", 0.9)


def test_two_timed_receipts_link_to_one_near_total_payment(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    cfg.owntracks_log_path.write_text(
        "2026-07-21T08:54:02+0530 owntracks/user/device "
        + json.dumps(
            {
                "_type": "location",
                "tst": int(dt.datetime.fromisoformat("2026-07-21T08:54:02+05:30").timestamp()),
                "lat": 12.95,
                "lon": 77.50,
                "poi": "ICICI Bank debited for Rs 122.00 on 21/07/2026",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    first = memory_dir / "bill-97604.md"
    second = memory_dir / "bill-97607.md"
    first.write_text(
        "# GO GREEN receipt\n\n## Key Fields\n\n- amount: 71.00\n- date: 21/07/26 08:22\n"
        "- merchant: GO GREEN DIRECT\n",
        encoding="utf-8",
    )
    second.write_text(
        "# GO GREEN receipt\n\n## Key Fields\n\n- amount: 51.50\n- date: 21/07/26 08:26\n"
        "- merchant: GO GREEN DIRECT\n",
        encoding="utf-8",
    )
    image_cfg = SimpleNamespace(ocr_enabled=False, log_file=tmp_path / "worker.log", memory_dir=memory_dir)

    result = index_scope(cfg, image_cfg, "2026-07-21")

    assert result.linked == 2
    with sqlite3.connect(cfg.db_path) as conn:
        rows = conn.execute(
            "SELECT memory_path, match_method, confidence FROM memory_poi_links ORDER BY memory_path"
        ).fetchall()
    assert len(rows) == 2
    assert {row[1] for row in rows} == {"grouped_near_amount_datetime"}
    assert {row[2] for row in rows} == {0.7}
