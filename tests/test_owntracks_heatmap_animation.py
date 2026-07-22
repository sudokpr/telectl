from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from owntracks.tagger import Event, OwnTracksScope, build_heatmap_summary, render_heatmap_html


TZ = ZoneInfo("Asia/Kolkata")


def location(line_no: int, timestamp: str, lat: float = 12.9716, lon: float = 77.5946) -> Event:
    captured_at = datetime.fromisoformat(timestamp).replace(tzinfo=TZ)
    return Event(
        line_no=line_no,
        received_at=None,
        topic="owntracks/test/device",
        payload={
            "_type": "location",
            "lat": lat,
            "lon": lon,
            "tst": int(captured_at.timestamp()),
            "motionactivities": ["stationary"],
        },
        local_tz=TZ,
    )


def test_heatmap_summary_keeps_daily_bucket_contributions() -> None:
    events = [
        location(1, "2026-06-01T09:00:00"),
        location(2, "2026-06-01T09:20:00"),
        location(3, "2026-06-02T10:00:00"),
        location(4, "2026-06-02T10:30:00"),
    ]
    scope = OwnTracksScope("month", "2026-06", date(2026, 6, 1), date(2026, 6, 30))

    summary = build_heatmap_summary(events, scope)

    assert len(summary["heat_points"]) == 1
    point = summary["heat_points"][0]
    assert point["weight"] == 4
    assert point["duration_minutes"] == 50
    assert point["timeline"] == [
        {"date": "2026-06-01", "weight": 2, "duration_minutes": 20, "visit_count": 1},
        {"date": "2026-06-02", "weight": 2, "duration_minutes": 30, "visit_count": 1},
    ]


def test_hosted_heatmap_has_cumulative_playback_controls() -> None:
    scope = OwnTracksScope("month", "2026-06", date(2026, 6, 1), date(2026, 6, 30))
    summary = build_heatmap_summary([location(1, "2026-06-01T09:00:00")], scope)

    html = render_heatmap_html(summary)

    assert 'id="toggleHeatmapPlayback"' in html
    assert 'id="heatmapTimeline"' in html
    assert "spotThroughDate" in html
    assert "Through ${timelineDates[timelineIndex]}" in html
