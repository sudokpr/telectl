from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import poi_memory
from memory_processor import relevant_memories
from types import SimpleNamespace


IST = ZoneInfo("Asia/Kolkata")


def log_line(stamp: str, payload: dict) -> str:
    return f"{stamp} owntracks/user/device {json.dumps(payload)}"


def test_sync_saves_every_poi_as_idempotent_memory(tmp_path: Path, monkeypatch) -> None:
    day = dt.datetime.now(IST).date() - dt.timedelta(days=1)
    first = dt.datetime.combine(day, dt.time(9, 0), IST)
    rows = [
        {"_type": "location", "tst": int(first.timestamp()), "lat": 12.9, "lon": 77.5, "poi": "Forest viewpoint"},
        {
            "_type": "location",
            "tst": int((first + dt.timedelta(minutes=10)).timestamp()),
            "lat": 12.91,
            "lon": 77.51,
            "poi": "fav trail but its 5-10min back",
        },
        {
            "_type": "location",
            "tst": int((first + dt.timedelta(minutes=20)).timestamp()),
            "lat": 12.92,
            "lon": 77.52,
            "poi": "Paid Rs.450 at Fruit Market",
        },
    ]
    log_path = tmp_path / "mqtt.log"
    log_path.write_text(
        "\n".join(log_line((first + dt.timedelta(minutes=index * 10)).isoformat(), payload) for index, payload in enumerate(rows))
        + "\n",
        encoding="utf-8",
    )
    tags_path = tmp_path / "user_tags.json"
    tags_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        poi_memory,
        "build_plan",
        lambda *_args: ({"candidate_stops": [], "travel_segments": []}, []),
    )

    result = poi_memory.sync_poi_memories(log_path, tags_path, tmp_path / "memories", "yesterday")
    repeated = poi_memory.sync_poi_memories(log_path, tags_path, tmp_path / "memories", "yesterday")

    assert result.scanned == 3
    assert result.created == 3
    assert result.errors == 0
    assert repeated.created == 0
    assert repeated.unchanged == 3
    contents = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "memories").glob("*.md"))
    assert "Forest viewpoint" in contents
    assert "fav trail but its 5-10min back" in contents
    assert 'category: "poi"' in contents
    assert '"spending"' in contents


def test_structured_poi_memory_uses_inner_capture_context(tmp_path: Path, monkeypatch) -> None:
    captured = "2026-07-17T11:48:36+05:30"
    raw = {
        "capture_id": "b3adca44-2f72-4e42-8729-d262fc55df77",
        "time": captured,
        "lat": 12.95,
        "lon": 77.50,
        "poi": "Ridge photo note",
    }
    payload = {
        "_type": "location",
        "tst": int(dt.datetime.fromisoformat("2026-07-18T01:00:00+05:30").timestamp()),
        "lat": 13.0,
        "lon": 77.6,
        "poi": json.dumps(raw),
    }
    log_path = tmp_path / "mqtt.log"
    log_path.write_text(log_line("2026-07-18T01:01:00+05:30", payload) + "\n", encoding="utf-8")
    tags_path = tmp_path / "user_tags.json"
    tags_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        poi_memory,
        "build_plan",
        lambda *_args: ({"candidate_stops": [], "travel_segments": []}, []),
    )

    result = poi_memory.sync_poi_memories(log_path, tags_path, tmp_path / "memories", "2026-07-17")

    assert result.created == 1
    path = next((tmp_path / "memories").glob("*.md"))
    content = path.read_text(encoding="utf-8")
    assert "20260717-114836" in path.name
    assert "- latitude: 12.95" in content
    assert "- longitude: 77.5" in content
    assert captured in content
    assert raw["capture_id"] in content


def test_relative_poi_query_can_retrieve_all_same_day_memories(tmp_path: Path) -> None:
    day_token = (dt.datetime.now(IST).date() - dt.timedelta(days=1)).strftime("%Y%m%d")
    for index in range(6):
        (tmp_path / f"owntracks-poi-{day_token}-00000{index}-{index}-place.md").write_text(
            f'category: "poi"\nPOI place {index}', encoding="utf-8"
        )
    cfg = SimpleNamespace(memory_dir=tmp_path, memory_query_top_k=3)

    matches = relevant_memories("What POIs did I capture yesterday?", cfg)

    assert len(matches) == 6
