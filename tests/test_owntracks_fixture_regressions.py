from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from owntracks.tagger import (
    Event,
    HomeFilterConfig,
    StopJitterFilterConfig,
    build_activity_dashboard_summary,
    build_stop_index_summary,
    build_trip_summary,
    build_plan,
    parse_log,
    render_leaflet_map_html,
    render_activity_dashboard_html,
    render_stop_index_html,
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


def event_at(line_no: int, iso_time: str, kind: str, lat: float, lon: float, **payload: object) -> Event:
    recorded_at = datetime.fromisoformat(iso_time)
    body = {
        "_type": kind,
        "lat": lat,
        "lon": lon,
        "tst": int(recorded_at.timestamp()),
    }
    body.update(payload)
    return Event(line_no, recorded_at, "owntracks/test/device", body, TZ)


@pytest.mark.parametrize(
    ("target_date", "expected_stop_ranges", "expected_hidden"),
    [
        (date(2026, 6, 12), [(3, 4), (6, 7), (10, 11)], []),
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
    assert plan["stats"]["visual_track_points"] == 12
    assert stop_line_ranges(plan) == [(3, 4), (6, 7), (10, 11)]
    assert line_numbers(plan, "sampled_track") == [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]


def test_stop_travel_segments_use_stop_boundaries_and_raw_points(fixture_events) -> None:
    user_tags = {
        "2026-06-12": {
            "stops": {
                "unnamed-stop-2-6": {"name": "Lunch"},
                "unnamed-stop-3-10": {"name": "Snacks"},
            }
        }
    }
    plan, _ = build_plan(fixture_events, date(2026, 6, 12), user_tags=user_tags, stop_jitter_filter=jitter_config())

    assert hidden_lines(plan) == []
    assert [(segment["start_name"], segment["end_name"], segment["duration_minutes"]) for segment in plan["travel_segments"]] == [
        ("Office", "Lunch", 165),
        ("Lunch", "Office", 9),
        ("Office", "Snacks", 145),
        ("Snacks", "Office", 9),
    ]
    lunch = next(stop for stop in plan["candidate_stops"] if stop.get("reviewed_name") == "Lunch")
    assert lunch["previous_travel"]["start_name"] == "Office"
    assert lunch["previous_travel"]["duration_minutes"] == 165
    assert lunch["next_travel"]["end_name"] == "Office"
    assert lunch["next_travel"]["duration_minutes"] == 9


def test_long_silent_gap_visit_shows_departure_window_before_correction() -> None:
    events = [
        event_at(1, "2026-06-27T09:00:00+05:30", "transition", 12.9569, 77.5180, desc="Home", event="leave"),
        event_at(2, "2026-06-27T09:45:00+05:30", "location", 12.9800, 77.5700, motionactivities=["automotive"]),
        event_at(3, "2026-06-27T10:09:16+05:30", "location", 12.9300, 77.6800, motionactivities=["stationary"]),
        event_at(4, "2026-06-27T11:45:30+05:30", "location", 12.9301, 77.6801, motionactivities=["stationary"]),
        event_at(5, "2026-06-27T14:41:55+05:30", "location", 12.9800, 77.5700, motionactivities=["automotive"]),
        event_at(6, "2026-06-27T15:30:00+05:30", "transition", 12.9569, 77.5180, desc="Home", event="enter"),
        event_at(7, "2026-06-27T15:30:00+05:30", "location", 12.9569, 77.5180, motionactivities=["stationary"], inregions=["Home"]),
    ]
    user_tags = {"2026-06-27": {"stops": {"unnamed-stop-2-3": {"name": "Cloudera"}}}}

    plan, _ = build_plan(events, date(2026, 6, 27), user_tags=user_tags)
    cloudera = next(stop for stop in plan["candidate_stops"] if stop.get("reviewed_name") == "Cloudera")

    assert cloudera["entry_display"] == "2026-06-27 10:09:16 IST"
    assert cloudera["exit_status"] == "window"
    assert cloudera["exit_display"] == "unknown/window 11:45:30-14:41:55"
    assert cloudera["exit_window"]["start"] == "2026-06-27 11:45:30 IST"
    assert cloudera["exit_window"]["end"] == "2026-06-27 14:41:55 IST"
    assert cloudera["confidence"] == "low"


def test_corrected_visit_exit_updates_travel_segments_and_ui_payload() -> None:
    events = [
        event_at(1, "2026-06-27T09:00:00+05:30", "transition", 12.9569, 77.5180, desc="Home", event="leave"),
        event_at(2, "2026-06-27T09:45:00+05:30", "location", 12.9800, 77.5700, motionactivities=["automotive"]),
        event_at(3, "2026-06-27T10:09:16+05:30", "location", 12.9300, 77.6800, motionactivities=["stationary"]),
        event_at(4, "2026-06-27T11:45:30+05:30", "location", 12.9301, 77.6801, motionactivities=["stationary"]),
        event_at(5, "2026-06-27T14:41:55+05:30", "location", 12.9800, 77.5700, motionactivities=["automotive"]),
        event_at(6, "2026-06-27T15:30:00+05:30", "transition", 12.9569, 77.5180, desc="Home", event="enter"),
        event_at(7, "2026-06-27T15:30:00+05:30", "location", 12.9569, 77.5180, motionactivities=["stationary"], inregions=["Home"]),
    ]
    user_tags = {
        "2026-06-27": {
            "stops": {
                    "unnamed-stop-2-3": {
                    "name": "Cloudera",
                    "exit_time": "~14:30",
                    "place": True,
                }
            }
        }
    }

    plan, _ = build_plan(events, date(2026, 6, 27), user_tags=user_tags)
    cloudera = next(stop for stop in plan["candidate_stops"] if stop.get("reviewed_name") == "Cloudera")

    assert cloudera["exit_status"] == "corrected"
    assert cloudera["end"] == "2026-06-27 14:30:00 IST"
    assert cloudera["duration"] == "4h 21m"
    assert [(segment["start_name"], segment["end_name"], segment["duration_minutes"]) for segment in plan["travel_segments"]] == [
        ("Home", "Cloudera", 69),
        ("Cloudera", "Home", 60),
    ]
    summary = build_trip_summary(
        events,
        user_tags,
        target_date=date(2026, 6, 27),
        origin_key="visit:unnamed-stop-2-3",
        destination_key="name:home",
    )
    assert summary["query"]["ok"] is True
    assert summary["query"]["departure"]["corrected"] is True
    assert summary["query"]["duration_seconds"] == 3600

    html = render_leaflet_map_html(plan)
    assert "Visits" in html
    assert "Entry override" in html
    assert "Exit override" in html
    assert "Save visit changes" in html
    assert "Show transition points" in html
    assert "let placeLabelsVisible = false" in html


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


def test_manual_route_point_is_restored_as_stop(fixture_events) -> None:
    user_tags = {
        "2026-06-14": {
            "stops": {
                "manual-stop-26": {
                    "manual": True,
                    "lat": 12.9716,
                    "lon": 77.5946,
                    "line": 26,
                    "timestamp": 1781395200,
                    "time": "08:00",
                    "motion_mode": "stationary",
                    "name": "Quick errand",
                }
            }
        }
    }

    plan, _ = build_plan(fixture_events, date(2026, 6, 14), user_tags=user_tags)

    stop = next(stop for stop in plan["candidate_stops"] if stop["id"] == "manual-stop-26")
    assert stop["manual"] is True
    assert stop["reviewed_name"] == "Quick errand"
    assert stop["points"] == 1

    html = render_leaflet_map_html(plan)
    assert "Save visit" in html
    assert "/owntracks/stops" in html
    assert "Save visit changes" in html
    assert "saveStopReviews" in html
    assert "routeAnimationMaxZoom = 16" in html
    assert "map.fitBounds(routeBounds" in html
    assert "map.panInside(currentPosition" not in html
    assert 'map.createPane("routePointsPane")' in html
    assert 'routePointRenderer = L.svg({ pane: "routePointsPane"' in html
    assert "renderer: routePointRenderer" in html
    assert "weight: 10" in html
    assert "World_Imagery" in html
    assert '"Satellite": satelliteTiles' in html


def test_day_map_renders_poi_locations_with_embedded_media() -> None:
    events = [
        Event(
            1,
            datetime.fromisoformat("2026-07-06T19:48:18+05:30"),
            "owntracks/test/device",
            {
                "_type": "location",
                "lat": 12.956929,
                "lon": 77.518039,
                "tst": 1783347499,
                "poi": "test media",
                "image": "/9j/test",
                "imagename": "IMG_4081",
                "motionactivities": ["stationary"],
                "acc": 14,
            },
            TZ,
        )
    ]

    plan, _ = build_plan(events, date(2026, 7, 6))

    assert plan["poi_events"][0]["name"] == "test media"
    assert plan["poi_events"][0]["imagename"] == "IMG_4081"
    assert plan["poi_events"][0]["image_data_url"] == "data:image/jpeg;base64,/9j/test"
    assert plan["sampled_track"][0]["poi"] == "test media"
    assert plan["sampled_track"][0]["has_image"] is True

    html = render_leaflet_map_html(plan)

    assert "poiEvents" in html
    assert "togglePois" in html
    assert "POI:" in html
    assert "test media" in html
    assert "data:image/jpeg;base64,/9j/test" in html
    assert "IMG_4081" in html
    assert "renderPois" in html


def test_stop_index_groups_reviewed_places_and_renders_visit_details(fixture_events) -> None:
    user_tags = {
        "2026-06-12": {
            "stops": {
                "unnamed-stop-2-6": {
                    "name": "Doctor",
                    "tags": ["health", "doctor"],
                    "note": "Annual checkup",
                }
            }
        },
        "2026-06-13": {
            "stops": {
                "unnamed-stop-3-18": {
                    "name": "Doctor",
                    "tags": ["health"],
                }
            }
        },
    }

    summary = build_stop_index_summary(
        fixture_events,
        user_tags,
        start=date(2026, 6, 12),
        end=date(2026, 6, 13),
    )

    doctor = next(place for place in summary["places"] if place["name"] == "Doctor")
    waypoint = next(place for place in summary["places"] if place["name"] == "Known family waypoint")
    office = next(place for place in summary["places"] if place["name"] == "Office")
    assert doctor["visit_count"] == 2
    assert doctor["first_visit"] == "2026-06-12"
    assert doctor["latest_visit"] == "2026-06-13"
    assert doctor["tags"] == ["doctor", "health"]
    assert [visit["date"] for visit in doctor["visits"]] == ["2026-06-13", "2026-06-12"]
    assert waypoint["visit_count"] == 1
    assert waypoint["visits"][0]["source"] == "waypoint"
    assert waypoint["visits"][0]["motion_mode"] == "waypoint"
    assert any(visit["source"] == "transition" for visit in office["visits"])

    html = render_stop_index_html(summary)
    assert "OwnTracks stop index" in html
    assert "Doctor" in html
    assert "Known family waypoint" in html
    assert "Annual checkup" in html
    assert "/owntracks/map/" in html
    assert "Refresh search aliases" in html
    assert "/owntracks/search-aliases" in html
    assert "not generated yet" in html
    assert "placeSearchScore" in html
    assert "Between places travel time" not in html
    assert "travelFrom" not in html
    assert "travelTo" not in html


def test_stop_index_includes_later_location_points_inside_known_waypoint() -> None:
    events = [
        Event(
            1,
            datetime.fromisoformat("2026-06-01T09:00:00+05:30"),
            "owntracks/test/device",
            {"_type": "waypoint", "desc": "Sugganahalli Land", "lat": 12.795737, "lon": 77.324158, "rad": 30, "rid": "land"},
            TZ,
        ),
        Event(
            2,
            datetime.fromisoformat("2026-06-14T15:59:21+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.795646, "lon": 77.324224, "tst": 1781432961, "motionactivities": ["automotive"], "vel": 20},
            TZ,
        ),
        Event(
            3,
            datetime.fromisoformat("2026-06-14T16:04:21+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.795767, "lon": 77.324287, "tst": 1781433261, "motionactivities": ["automotive"], "vel": 20},
            TZ,
        ),
    ]

    summary = build_stop_index_summary(events, start=date(2026, 6, 14), end=date(2026, 6, 14))

    land = next(place for place in summary["places"] if place["name"] == "Sugganahalli Land")
    assert land["visit_count"] == 1
    assert land["visits"][0]["source"] == "waypoint-proximity"
    assert land["visits"][0]["date"] == "2026-06-14"
    assert land["visits"][0]["duration"] == "5 min"


def test_stop_index_includes_later_location_points_inside_saved_place() -> None:
    user_tags = {
        "2026-06-28": {
            "stops": {
                "manual-stop-1443": {
                    "manual": True,
                    "place": True,
                    "lat": 12.79568,
                    "lon": 77.32431,
                    "name": "Sugganahalli Land",
                }
            }
        }
    }
    events = [
        Event(
            1,
            datetime.fromisoformat("2026-07-04T11:23:17+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.795412, "lon": 77.323697, "tst": 1783144397, "vel": 6},
            TZ,
        ),
        Event(
            2,
            datetime.fromisoformat("2026-07-04T11:23:18+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.79582, "lon": 77.324272, "tst": 1783144398, "vel": 1},
            TZ,
        ),
        Event(
            3,
            datetime.fromisoformat("2026-07-04T13:17:10+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.957036, "lon": 77.518074, "tst": 1783151230},
            TZ,
        ),
    ]

    summary = build_stop_index_summary(events, user_tags, start=date(2026, 7, 4), end=date(2026, 7, 4))

    land = next(place for place in summary["places"] if place["name"] == "Sugganahalli Land")
    assert land["visit_count"] == 1
    assert land["visits"][0]["source"] == "review-proximity"
    assert land["visits"][0]["points"] == 2
    assert land["visits"][0]["date"] == "2026-07-04"


def test_daily_plan_includes_saved_place_proximity_as_named_place() -> None:
    user_tags = {
        "2026-06-28": {
            "stops": {
                "manual-stop-1443": {
                    "manual": True,
                    "place": True,
                    "lat": 12.79568,
                    "lon": 77.32431,
                    "name": "Sugganahalli Land",
                }
            }
        }
    }
    events = [
        Event(
            1,
            datetime.fromisoformat("2026-07-04T11:23:17+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.795412, "lon": 77.323697, "tst": 1783144397, "vel": 6},
            TZ,
        ),
        Event(
            2,
            datetime.fromisoformat("2026-07-04T11:23:18+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.79582, "lon": 77.324272, "tst": 1783144398, "vel": 1},
            TZ,
        ),
    ]

    plan, _track_points = build_plan(events, date(2026, 7, 4), user_tags)

    land = next(place for place in plan["named_places"] if place["name"] == "Sugganahalli Land")
    assert land["action"] == "visit"
    assert land["source"] == "review-proximity"
    assert land["points"] == 2


def test_stop_index_travel_pairs_summarize_fastest_average_and_median() -> None:
    events = [
        Event(
            1,
            datetime.fromisoformat("2026-06-01T09:00:00+05:30"),
            "owntracks/test/device",
            {"_type": "transition", "desc": "Home", "event": "leave", "lat": 12.9, "lon": 77.5, "tst": 1780284600},
            TZ,
        ),
        Event(
            2,
            datetime.fromisoformat("2026-06-01T09:20:00+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.91, "lon": 77.51, "tst": 1780285800, "motionactivities": ["automotive"]},
            TZ,
        ),
        Event(
            3,
            datetime.fromisoformat("2026-06-01T09:30:00+05:30"),
            "owntracks/test/device",
            {"_type": "transition", "desc": "Office", "event": "enter", "lat": 12.92, "lon": 77.52, "tst": 1780286400},
            TZ,
        ),
        Event(
            4,
            datetime.fromisoformat("2026-06-02T09:00:00+05:30"),
            "owntracks/test/device",
            {"_type": "transition", "desc": "Home", "event": "leave", "lat": 12.9, "lon": 77.5, "tst": 1780371000},
            TZ,
        ),
        Event(
            5,
            datetime.fromisoformat("2026-06-02T09:40:00+05:30"),
            "owntracks/test/device",
            {"_type": "location", "lat": 12.91, "lon": 77.51, "tst": 1780373400, "motionactivities": ["automotive"]},
            TZ,
        ),
        Event(
            6,
            datetime.fromisoformat("2026-06-02T10:00:00+05:30"),
            "owntracks/test/device",
            {"_type": "transition", "desc": "Office", "event": "enter", "lat": 12.92, "lon": 77.52, "tst": 1780374600},
            TZ,
        ),
    ]

    summary = build_stop_index_summary(events, start=date(2026, 6, 1), end=date(2026, 6, 2))

    home_office = next(pair for pair in summary["travel_pairs"] if pair["start_name"] == "Home" and pair["end_name"] == "Office")
    assert home_office["count"] == 2
    assert home_office["min_duration"] == "30 min"
    assert home_office["avg_duration"] == "45 min"
    assert home_office["median_duration"] == "45 min"
    assert home_office["fastest"]["date"] == "2026-06-01"


def test_activity_dashboard_summarizes_days_and_places(fixture_events) -> None:
    summary = build_activity_dashboard_summary(
        fixture_events,
        start=date(2026, 6, 12),
        end=date(2026, 6, 14),
        home_filter=HomeFilterConfig(True, ("Office",), 120),
        stop_jitter_filter=jitter_config(),
    )

    assert summary["scope"]["days"] == 3
    assert summary["stats"]["observed_days"] == 3
    assert summary["stats"]["out_of_home_days"] >= 1
    assert summary["stats"]["longest_out_of_home_streak_days"] >= 1
    assert summary["stats"]["longest_home_only_streak_days"] >= 0
    assert summary["stats"]["longest_travel_streak_days"] >= 0
    assert summary["stats"]["places"] >= 1
    assert len(summary["daily"]) == 3

    html = render_activity_dashboard_html(summary)
    assert "OwnTracks activity dashboard" in html
    assert "Longest out streak" in html
    assert "Longest travel streak" in html
    assert "Active day calendar" in html
    assert "Last month" in html
    assert "Year to date" in html
    assert "/owntracks/dashboard" in html
    assert "/owntracks/map/" in html
