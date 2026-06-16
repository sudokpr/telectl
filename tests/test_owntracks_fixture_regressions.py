from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from owntracks.tagger import (
    StopJitterFilterConfig,
    build_plan,
    parse_log,
    render_leaflet_map_html,
)


TZ = ZoneInfo("Asia/Kolkata")
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "owntracks" / "significant_mode_jitter.jsonl"


@pytest.fixture(scope="module")
def fixture_events():
    return parse_log(FIXTURE_PATH, TZ)


def jitter_config() -> StopJitterFilterConfig:
    return StopJitterFilterConfig(
        enabled=True,
        radius_m=150,
        min_dwell_minutes=10,
        include_geofences=True,
        include_candidate_stops=True,
    )


def line_numbers(plan: dict, key: str) -> list[int]:
    return [int(point["line"]) for point in plan[key]]


def hidden_lines(plan: dict) -> list[int]:
    return sorted(set(line_numbers(plan, "raw_sampled_track")) - set(line_numbers(plan, "sampled_track")))


def stop_line_ranges(plan: dict) -> list[tuple[int, int]]:
    return [(int(stop["start_line"]), int(stop["end_line"])) for stop in plan["candidate_stops"]]


@pytest.mark.parametrize(
    ("target_date", "expected_stop_ranges", "expected_hidden"),
    [
        (date(2026, 6, 12), [(3, 4), (6, 7), (10, 11)], [9]),
        (date(2026, 6, 13), [(18, 20)], [19]),
    ],
)
def test_jitter_filter_keeps_raw_track_and_stop_boundaries(
    fixture_events,
    target_date: date,
    expected_stop_ranges: list[tuple[int, int]],
    expected_hidden: list[int],
) -> None:
    unfiltered, _ = build_plan(fixture_events, target_date)
    filtered, _ = build_plan(fixture_events, target_date, stop_jitter_filter=jitter_config())

    assert line_numbers(unfiltered, "sampled_track") == line_numbers(unfiltered, "raw_sampled_track")
    assert line_numbers(filtered, "raw_sampled_track") == line_numbers(unfiltered, "raw_sampled_track")
    assert hidden_lines(filtered) == expected_hidden
    assert stop_line_ranges(filtered) == expected_stop_ranges

    visible_lines = set(line_numbers(filtered, "sampled_track"))
    for start_line, end_line in expected_stop_ranges:
        assert start_line in visible_lines
        assert end_line in visible_lines


def test_repeated_stop_visits_remain_separate_after_jitter_filtering(fixture_events) -> None:
    plan, _ = build_plan(fixture_events, date(2026, 6, 12), stop_jitter_filter=jitter_config())

    assert plan["stats"]["track_points"] == 12
    assert plan["stats"]["visual_track_points"] == 11
    assert stop_line_ranges(plan) == [(3, 4), (6, 7), (10, 11)]
    assert line_numbers(plan, "sampled_track") == [1, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13]


def test_sparse_significant_mode_stop_is_detected_after_long_quiet_gap(fixture_events) -> None:
    plan, _ = build_plan(fixture_events, date(2026, 6, 13), stop_jitter_filter=jitter_config())

    assert stop_line_ranges(plan) == [(18, 20)]
    stop = plan["candidate_stops"][0]
    assert stop["duration_minutes"] == 102
    assert stop["points"] == 3
    assert stop["motion_modes"] == "stationary:2, automotive:1"


def test_automotive_dwell_stops_and_filtered_overlay_payload_render(fixture_events) -> None:
    plan, _ = build_plan(fixture_events, date(2026, 6, 14), stop_jitter_filter=jitter_config())

    assert stop_line_ranges(plan) == [(27, 29), (30, 31)]
    assert plan["candidate_stops"][1]["reviewed_name"] == "Known family waypoint"
    assert line_numbers(plan, "raw_sampled_track") == line_numbers(plan, "sampled_track")

    html = render_leaflet_map_html(plan)

    assert "rawSampledTrack" in html
    assert "sampledTrack" in html
    assert "toggleFilteredPoints" in html
    assert "Show filtered points" in html
