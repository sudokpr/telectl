from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo

from owntracks.tagger import (
    Event,
    HomeFilterConfig,
    StopJitterAnchor,
    StopJitterFilterConfig,
    apply_stop_labels_to_point_dicts,
    build_trip_summary,
    build_plan,
    candidate_stops,
    filter_stop_jitter_points,
    location_override_for,
    point_dicts,
    route_points_via_manual_stops,
    saved_stop_matches_stop,
    render_leaflet_map_html,
    render_trip_html,
    stop_override_for,
)


TZ = ZoneInfo("Asia/Kolkata")


def location(line_no: int, minutes: int, lat: float, lon: float, **payload: object) -> Event:
    base = datetime(2026, 6, 12, 12, 0, tzinfo=TZ)
    body = {
        "_type": "location",
        "lat": lat,
        "lon": lon,
        "tst": int((base + timedelta(minutes=minutes)).timestamp()),
    }
    body.update(payload)
    return Event(line_no=line_no, received_at=None, topic="owntracks/test/device", payload=body, local_tz=TZ)


def transition(line_no: int, minutes: int, lat: float, lon: float, desc: str, event: str) -> Event:
    base = datetime(2026, 6, 12, 12, 0, tzinfo=TZ)
    body = {
        "_type": "transition",
        "lat": lat,
        "lon": lon,
        "desc": desc,
        "event": event,
        "tst": int((base + timedelta(minutes=minutes)).timestamp()),
    }
    return Event(line_no=line_no, received_at=None, topic="owntracks/test/device", payload=body, local_tz=TZ)


def waypoint(line_no: int, minutes: int, lat: float, lon: float, desc: str, radius: int = 20) -> Event:
    base = datetime(2026, 6, 12, 12, 0, tzinfo=TZ)
    body = {
        "_type": "waypoint",
        "lat": lat,
        "lon": lon,
        "desc": desc,
        "rad": radius,
        "tst": int((base + timedelta(minutes=minutes)).timestamp()),
    }
    return Event(line_no=line_no, received_at=None, topic="owntracks/test/device", payload=body, local_tz=TZ)


def jitter_config(radius_m: float = 150) -> StopJitterFilterConfig:
    return StopJitterFilterConfig(
        enabled=True,
        radius_m=radius_m,
        min_dwell_minutes=10,
        include_geofences=True,
        include_candidate_stops=True,
    )


def test_stop_jitter_preserves_transition_and_stop_boundary_connectors() -> None:
    office = StopJitterAnchor(12.9004, 77.5950, "Office", "geofence")
    lunch = StopJitterAnchor(12.8993, 77.5970, "Lunch", "candidate_stop")
    points = [
        location(1, 0, 12.8990, 77.5900, motionactivities=["cycling"]),
        location(2, 5, 12.9004, 77.5950, t="c", inregions=["Office"]),
        location(3, 20, 12.9004, 77.5951, inregions=["Office"], motionactivities=["stationary"]),
        location(4, 30, 12.8995, 77.5966, t="c", motionactivities=["walking"]),
        location(5, 45, 12.8993, 77.5970, motionactivities=["stationary"]),
        location(6, 60, 12.8992, 77.5971, motionactivities=["stationary"]),
        location(7, 75, 12.9004, 77.5950, t="c", inregions=["Office"]),
        location(8, 90, 12.9004, 77.5951, inregions=["Office"], motionactivities=["stationary"]),
        location(9, 105, 12.9040, 77.6000, motionactivities=["cycling"]),
    ]

    filtered, removed = filter_stop_jitter_points(
        points,
        jitter_config(),
        [office, lunch],
        preserve_lines={5, 6},
    )

    assert [event.line_no for event in filtered] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert removed == 0


def test_stop_jitter_keeps_connector_when_hidden_run_crosses_anchor_areas() -> None:
    service = StopJitterAnchor(12.9230, 77.4965, "Service", "candidate_stop")
    land = StopJitterAnchor(12.795737, 77.324158, "Sugganahalli Land", "geofence")
    home = StopJitterAnchor(12.9570, 77.5181, "Home", "geofence")
    points = [
        location(1, 0, 12.9240, 77.4978, motionactivities=["automotive"]),
        location(2, 2, 12.9234, 77.4974, motionactivities=["automotive"]),
        location(3, 4, 12.9230, 77.4966, motionactivities=["automotive"]),
        location(4, 6, 12.9231, 77.4965, motionactivities=["automotive"]),
        location(5, 64, 12.7954, 77.3237, motionactivities=["moving"]),
        location(6, 65, 12.7958, 77.3243, motionactivities=["stationary"]),
        location(7, 125, 12.9570, 77.5181, motionactivities=["stationary"]),
        location(8, 130, 12.9570, 77.5182, motionactivities=["stationary"]),
    ]

    filtered, removed = filter_stop_jitter_points(points, jitter_config(), [service, land, home])

    assert [event.line_no for event in filtered] == [1, 2, 4, 5, 6, 7, 8]
    assert removed == 1


def test_isolated_overnight_jitter_without_route_context_is_hidden() -> None:
    home = StopJitterAnchor(12.9569, 77.5181, "Home", "geofence")
    points = [location(1, 0, 12.95692, 77.51812, vel=2, inregions=[])]

    filtered, removed = filter_stop_jitter_points(points, jitter_config(), [home])

    assert filtered == []
    assert removed == 1


def test_stop_jitter_run_keeps_boundary_connector_to_visible_route() -> None:
    office = StopJitterAnchor(12.9004, 77.5950, "Office", "geofence")
    points = [
        location(1, 0, 12.8990, 77.5900, motionactivities=["cycling"]),
        location(2, 5, 12.9004, 77.5950, inregions=["Office"]),
        location(3, 20, 12.9004, 77.5951, inregions=["Office"], motionactivities=["stationary"]),
        location(4, 35, 12.9050, 77.6000, motionactivities=["cycling"]),
    ]

    filtered, removed = filter_stop_jitter_points(points, jitter_config(), [office])

    assert [event.line_no for event in filtered] == [1, 2, 3, 4]
    assert removed == 0


def test_stop_jitter_keeps_day_endpoints_inside_stop_area() -> None:
    home = StopJitterAnchor(12.9569, 77.5181, "Home", "geofence")
    points = [
        location(1, 0, 12.9569, 77.5181, motionactivities=["stationary"]),
        location(2, 10, 12.9450, 77.5270, motionactivities=["walking"]),
        location(3, 20, 12.95691, 77.51811, motionactivities=["stationary"]),
    ]

    filtered, removed = filter_stop_jitter_points(points, jitter_config(), [home])

    assert [event.line_no for event in filtered] == [1, 2, 3]
    assert removed == 0


def test_candidate_stops_include_significant_mode_automotive_dwell() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["automotive"]),
        location(2, 45, 12.9001, 77.5901, vel=2, motionactivities=["automotive"]),
    ]

    stops = candidate_stops(points)

    assert len(stops) == 1
    assert stops[0]["start_line"] == 1
    assert stops[0]["end_line"] == 2
    assert stops[0]["duration_minutes"] == 45
    assert stops[0]["motion_mode"] == "automotive"


def test_candidate_stops_do_not_turn_highway_samples_into_stops() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["automotive"]),
        location(2, 15, 12.9800, 77.6700, motionactivities=["automotive"]),
        location(3, 30, 13.0600, 77.7500, motionactivities=["automotive"]),
    ]

    assert candidate_stops(points) == []


def test_candidate_stops_bridge_sparse_same_place_significant_mode_gap() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        location(2, 5, 12.90002, 77.59002),
        location(3, 100, 12.90003, 77.59001, motionactivities=["stationary", "automotive"]),
    ]

    stops = candidate_stops(points)

    assert len(stops) == 1
    assert stops[0]["start_line"] == 1
    assert stops[0]["end_line"] == 3
    assert stops[0]["duration_minutes"] == 100


def test_candidate_stops_do_not_bridge_sparse_same_place_gap_across_trip() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        location(2, 50, 12.9500, 77.6400, motionactivities=["walking"]),
        location(3, 100, 12.90003, 77.59001, motionactivities=["stationary"]),
    ]

    assert candidate_stops(points) == []


def test_candidate_stops_include_same_place_dwell_with_bad_speed_sample() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        location(2, 12, 12.90001, 77.59001, vel=7),
    ]

    stops = candidate_stops(points)

    assert len(stops) == 1
    assert stops[0]["start_line"] == 1
    assert stops[0]["end_line"] == 2
    assert stops[0]["duration_minutes"] == 12


def test_candidate_stops_same_place_fallback_excludes_home() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, inregions=["Home"], motionactivities=["stationary"]),
        location(2, 12, 12.90001, 77.59001, inregions=["Home"], vel=7),
    ]

    assert candidate_stops(points) == []


def test_sparse_buffered_route_points_are_suggested_as_possible_missed_stops() -> None:
    received_at = datetime(2026, 6, 12, 23, 3, tzinfo=TZ)

    def buffered_location(line_no: int, minutes: int, lat: float, lon: float, **payload: object) -> Event:
        event = location(line_no, minutes, lat, lon, **payload)
        event.received_at = received_at
        return event

    events = [
        buffered_location(1, 0, 12.9755, 77.6065, motionactivities=["automotive"]),
        buffered_location(2, 29, 12.9172, 77.5802, motionactivities=["automotive"], acc=207),
        buffered_location(3, 45, 12.8886, 77.5636, acc=42),
        buffered_location(4, 98, 12.8962, 77.5705, motionactivities=["automotive"], t="v", acc=61),
        buffered_location(5, 118, 12.9245, 77.5650, t="v", acc=40),
        buffered_location(6, 144, 12.9263, 77.5483, motionactivities=["automotive"], t="v", acc=40),
    ]

    plan, _track_points = build_plan(events, datetime(2026, 6, 12, tzinfo=TZ).date())

    assert plan["candidate_stops"] == []
    assert [item["line"] for item in plan["possible_missed_stops"]] == [3, 5]
    assert "buffered upload" in plan["possible_missed_stops"][0]["reason"]
    assert plan["possible_missed_stops"][1]["next_gap_minutes"] == 26


def test_leaflet_map_payload_includes_possible_missed_stops() -> None:
    plan = {
        "date": "2026-06-12",
        "sampled_track": [],
        "raw_sampled_track": [],
        "candidate_stops": [],
        "possible_missed_stops": [
            {
                "id": "possible-stop-3",
                "line": 3,
                "lat": 12.8886,
                "lon": 77.5636,
                "time": "2026-06-12 21:02:19 IST",
                "reason": "buffered upload delayed 2h 01m",
            }
        ],
        "named_places": [],
        "motion_summary": {"counts": {}, "dominant": "unknown"},
        "travel_segments": [],
        "ride_segments": [],
        "elevation_summary": {},
    }

    html = render_leaflet_map_html(plan)
    assert 'id="today"' in html
    assert "navigateToday" in html
    match = re.search(r"const data = (\{.*?\});\n", html, re.S)

    assert match is not None
    payload = json.loads(match.group(1))
    assert [item["line"] for item in payload["possibleMissedStops"]] == [3]


def test_ignored_stop_is_removed_but_route_points_remain() -> None:
    events = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        location(2, 15, 12.9001, 77.5901, motionactivities=["stationary"]),
        location(3, 25, 12.9100, 77.6000, motionactivities=["walking"]),
    ]
    user_tags = {
        "2026-06-12": {
            "stops": {
                "unnamed-stop-1-1": {
                    "lat": 12.9000,
                    "lon": 77.5900,
                    "ignored": True,
                }
            }
        }
    }

    plan, _ = build_plan(events, datetime(2026, 6, 12, tzinfo=TZ).date(), user_tags=user_tags)

    assert plan["candidate_stops"] == []
    assert [point["line"] for point in plan["sampled_track"]] == [1, 2, 3]


def test_saved_stop_review_matches_legacy_line_range_after_renumbering() -> None:
    stop = {
        "id": "unnamed-stop-4-412",
        "start_line": 412,
        "end_line": 412,
    }
    stop_overrides = {
        "unnamed-stop-5-412-416": {
            "name": "MTB trials - siddle kallu",
            "tags": ["mtb"],
        }
    }

    assert stop_override_for(stop, stop_overrides) == {
        "name": "MTB trials - siddle kallu",
        "tags": ["mtb"],
    }


def test_boundary_home_stop_added_for_significant_change_day_start() -> None:
    events = [
        waypoint(1, -10, 12.9000, 77.5900, "Home", radius=30),
        location(2, 0, 12.9022, 77.5900, motionactivities=["stationary"]),
        location(3, 10, 12.9400, 77.6200, motionactivities=["automotive"]),
    ]

    plan, _ = build_plan(
        events,
        datetime(2026, 6, 12, tzinfo=TZ).date(),
        home_filter=HomeFilterConfig(enabled=True, region_names=("Home",), radius_m=150),
    )

    assert [(stop["id"], stop["name"], stop.get("boundary")) for stop in plan["candidate_stops"]] == [
        ("boundary-home-start-2", "Home", "start")
    ]


def test_proximity_override_does_not_rename_existing_named_stop() -> None:
    user_tags = {
        "2026-06-12": {
            "stops": {
                "manual-stop-12": {
                    "name": "Hair salon",
                    "lat": 12.90005,
                    "lon": 77.59005,
                    "tags": ["errand"],
                }
            }
        }
    }

    home_stop = {"name": "Home", "lat": 12.9000, "lon": 77.5900}
    unnamed_stop = {"name": "unnamed-stop-7", "lat": 12.9000, "lon": 77.5900}

    assert location_override_for(home_stop, user_tags, "2026-06-13") == {}
    assert location_override_for(unnamed_stop, user_tags, "2026-06-13")["name"] == "Hair salon"


def test_single_use_detected_stop_is_exact_only_not_reused_by_proximity() -> None:
    user_tags = {
        "2026-06-12": {
            "stops": {
                "unnamed-stop-7-42": {
                    "name": "One-time errand",
                    "lat": 12.90005,
                    "lon": 77.59005,
                    "timestamp": 1_718_200_100,
                    "place": False,
                }
            }
        }
    }

    exact_stop = {
        "name": "unnamed-stop-7",
        "lat": 12.90005,
        "lon": 77.59005,
        "start_timestamp": 1_718_200_000,
        "end_timestamp": 1_718_200_200,
    }
    future_stop = {"name": "unnamed-stop-8", "lat": 12.9000, "lon": 77.5900}

    assert location_override_for(exact_stop, user_tags, "2026-06-12")["name"] == "One-time errand"
    assert location_override_for(future_stop, user_tags, "2026-06-13") == {}


def test_manual_stop_with_corrected_times_routes_through_saved_location() -> None:
    points = [
        {"lat": 12.8000, "lon": 77.3200, "timestamp": 100, "line": 1},
        {"lat": 12.8100, "lon": 77.3300, "timestamp": 250, "line": 2},
        {"lat": 12.8200, "lon": 77.3400, "timestamp": 400, "line": 3},
    ]
    stop = {
        "id": "manual-stop-1-200",
        "manual": True,
        "reviewed_name": "Missed temple",
        "lat": 12.8050,
        "lon": 77.3500,
        "start_line": 1,
        "visit_start_timestamp": 200,
        "visit_end_timestamp": 300,
        "entry_display": "14:01",
        "exit_display": "14:20",
        "entry_corrected": True,
        "exit_corrected": True,
    }

    routed = route_points_via_manual_stops(points, [stop])

    assert [point["timestamp"] for point in routed] == [100, 200, 300, 400]
    assert [(point["lat"], point["lon"]) for point in routed[1:3]] == [
        (12.805, 77.35),
        (12.805, 77.35),
    ]
    assert [point["manual_stop_phase"] for point in routed[1:3]] == ["arrival", "departure"]
    assert points[1]["timestamp"] == 250


def test_manual_route_point_does_not_rename_detected_home_stop() -> None:
    events = [
        waypoint(1, -5, 12.9000, 77.5900, "Home", radius=250),
        location(2, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        location(3, 12, 12.90003, 77.59003, motionactivities=["stationary"]),
        location(4, 60, 12.9050, 77.5950, motionactivities=["walking"]),
        location(5, 90, 12.90004, 77.59004, motionactivities=["stationary"]),
        location(6, 105, 12.90005, 77.59005, motionactivities=["stationary"]),
    ]
    user_tags = {
        "2026-06-12": {
            "stops": {
                "manual-stop-2": {
                    "manual": True,
                    "line": 2,
                    "timestamp": int(events[1].recorded_at.timestamp()),
                    "name": "Hair salon",
                    "lat": 12.9000,
                    "lon": 77.5900,
                }
            }
        }
    }

    plan, _ = build_plan(events, datetime(2026, 6, 12, tzinfo=TZ).date(), user_tags=user_tags)

    assert [
        (stop["id"], stop["start_line"], stop["end_line"], stop.get("name"), stop.get("reviewed_name"))
        for stop in plan["candidate_stops"]
    ] == [
        ("manual-stop-2", 2, 2, "Hair salon", "Hair salon"),
        ("unnamed-stop-1-2", 2, 3, "unnamed-stop-1", "Home"),
        ("unnamed-stop-2-5", 5, 6, "unnamed-stop-2", "Home"),
    ]


def test_trip_summary_does_not_interpolate_long_boundary_gaps() -> None:
    events = [
        waypoint(1, -10, 12.9569, 77.5180, "Home", radius=30),
        waypoint(2, -10, 12.795737, 77.324158, "Sugganahalli Land", radius=20),
        location(3, 0, 12.9569, 77.5180, motionactivities=["stationary"]),
        location(4, 33, 12.95688, 77.5179, motionactivities=["stationary"]),
        location(5, 85, 12.9300, 77.4900, motionactivities=["stationary"]),
        location(6, 91, 12.9000, 77.4500, motionactivities=["automotive"], t="v"),
        location(7, 161, 12.79574, 77.32416, motionactivities=["stationary"]),
        location(8, 205, 12.7959, 77.3243, motionactivities=["stationary"]),
    ]

    summary = build_trip_summary(
        events,
        target_date=datetime(2026, 6, 12, tzinfo=TZ).date(),
        origin_key="name:home",
        destination_key="name:sugganahalli land",
    )

    assert summary["query"]["ok"] is True
    assert summary["query"]["departure"]["line"] == 5
    assert summary["query"]["departure"]["estimated"] is False
    assert summary["query"]["departure"]["source"] == "sample"
    assert summary["query"]["arrival"]["line"] == 7
    assert summary["query"]["arrival"]["estimated"] is False
    assert summary["query"]["duration_seconds"] == 4560
    assert summary["query"]["last_origin"]["line"] == 4


def test_trip_summary_interpolates_only_short_boundary_gaps() -> None:
    events = [
        waypoint(1, -10, 12.9569, 77.5180, "Home", radius=30),
        waypoint(2, -10, 12.795737, 77.324158, "Sugganahalli Land", radius=20),
        location(3, 0, 12.9569, 77.5180, motionactivities=["stationary"]),
        location(4, 33, 12.95688, 77.5179, motionactivities=["stationary"]),
        location(5, 40, 12.9300, 77.4900, motionactivities=["automotive"], t="v"),
        location(6, 155, 12.8200, 77.3500, motionactivities=["automotive"], t="v"),
        location(7, 161, 12.79574, 77.32416, motionactivities=["stationary"]),
    ]

    summary = build_trip_summary(
        events,
        target_date=datetime(2026, 6, 12, tzinfo=TZ).date(),
        origin_key="name:home",
        destination_key="name:sugganahalli land",
    )

    assert summary["query"]["ok"] is True
    assert summary["query"]["departure"]["source"] == "interpolated"
    assert summary["query"]["departure"]["estimated"] is True
    assert summary["query"]["arrival"]["source"] == "interpolated"
    assert summary["query"]["arrival"]["estimated"] is True


def test_trip_summary_lists_day_visits_and_marks_reusable_places() -> None:
    events = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        location(2, 10, 12.9100, 77.6000, motionactivities=["walking"]),
    ]
    user_tags = {
        "2026-06-12": {
            "stops": {
                "manual-stop-1": {
                    "manual": True,
                    "line": 1,
                    "name": "One-off errand",
                    "lat": 12.9000,
                    "lon": 77.5900,
                },
                "manual-stop-2": {
                    "manual": True,
                    "line": 2,
                    "name": "Reusable place",
                    "lat": 12.9100,
                    "lon": 77.6000,
                    "place": True,
                },
            }
        }
    }

    summary = build_trip_summary(
        events,
        user_tags=user_tags,
        target_date=datetime(2026, 6, 12, tzinfo=TZ).date(),
    )

    place_names = {place["name"] for place in summary["places"]}
    assert "One-off errand" in place_names
    assert "Reusable place" in place_names
    one_off = next(place for place in summary["places"] if place["name"] == "One-off errand")
    reusable = [place for place in summary["places"] if place["name"] == "Reusable place"]
    assert one_off["key"] == "visit:manual-stop-1"
    assert "visit" in one_off["sources"]
    assert any("review" in place["sources"] for place in reusable)


def test_trip_dashboard_renders_place_sources() -> None:
    summary = {
        "title": "OwnTracks trips",
        "date": "2026-06-12",
        "places": [
            {
                "key": "name:hair salon",
                "name": "Hair Salon",
                "display_name": "s2: Hair Salon · 2026-06-12 12:10 IST",
                "radius_m": 150,
                "sources": ["visit", "review"],
            }
        ],
        "selected": {"origin_key": "name:hair salon", "destination_key": "name:hair salon"},
        "query": {"ok": False, "reason": "Select two different places."},
        "timeline": [],
    }

    html = render_trip_html(summary)

    assert "Trip Places" in html
    assert "s2: Hair Salon" in html
    assert "saved trip place" in html


def test_reviewed_stop_labels_use_timestamps_not_line_ranges() -> None:
    plan = {
        "candidate_stops": [
            {
                "reviewed_name": "Sugganahalli Land",
                "user_reviewed": True,
                "start_line": 1430,
                "end_line": 1448,
                "start_timestamp": 1782636397,
                "end_timestamp": 1782637100,
            }
        ],
        "raw_sampled_track": [
            {"line": 1435, "timestamp": 1782633131},
            {"line": 1430, "timestamp": 1782636397},
        ],
        "sampled_track": [
            {"line": 1443, "timestamp": 1782633739},
            {"line": 1448, "timestamp": 1782637100},
        ],
    }

    apply_stop_labels_to_point_dicts(plan)

    assert "place_name" not in plan["raw_sampled_track"][0]
    assert plan["raw_sampled_track"][1]["place_name"] == "Sugganahalli Land"
    assert "place_name" not in plan["sampled_track"][0]
    assert plan["sampled_track"][1]["place_name"] == "Sugganahalli Land"


def test_saved_stop_exact_match_prefers_timestamp_over_line_range() -> None:
    saved_stop = {
        "manual": True,
        "line": 1443,
        "timestamp": 1782633739,
        "name": "Sugganahalli Land",
    }
    detected_stop = {
        "start_line": 1430,
        "end_line": 1448,
        "start_timestamp": 1782636397,
        "end_timestamp": 1782637100,
    }

    assert saved_stop_matches_stop(saved_stop, detected_stop) is False


def test_point_dicts_include_waypoint_labels() -> None:
    points = [location(1, 0, 12.9569, 77.5181, motionactivities=["stationary"])]
    events = [waypoint(2, -10, 12.95691, 77.51811, "Home"), *points]

    rendered = point_dicts(points, events)

    assert rendered[0]["place_name"] == "Home"


def test_point_dicts_respect_waypoint_radius_for_route_labels() -> None:
    points = [location(1, 0, 12.956746, 77.517196, motionactivities=["walking"])]
    events = [waypoint(2, -10, 12.956921, 77.518023, "Home", radius=30), *points]

    rendered = point_dicts(points, events)

    assert "place_name" not in rendered[0]


def test_build_plan_keeps_raw_track_for_filtered_point_overlay() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["walking"]),
        location(2, 5, 12.9001, 77.5901, inregions=["Home"], motionactivities=["stationary"]),
        location(3, 10, 12.9010, 77.5910, motionactivities=["walking"]),
    ]

    plan, _ = build_plan(
        points,
        datetime(2026, 6, 12, tzinfo=TZ).date(),
        home_filter=HomeFilterConfig(enabled=True, region_names=("Home",), radius_m=150),
    )

    assert [point["line"] for point in plan["raw_sampled_track"]] == [1, 2, 3]
    assert [point["line"] for point in plan["sampled_track"]] == [1, 3]


def test_build_plan_keeps_full_day_when_ride_points_are_not_home_bracketed() -> None:
    points = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        location(2, 20, 12.9100, 77.6000, motionactivities=["cycling"]),
        location(3, 40, 12.9200, 77.6100, motionactivities=["cycling"]),
        location(4, 80, 12.9300, 77.6200, motionactivities=["stationary"]),
    ]

    plan, _ = build_plan(points, datetime(2026, 6, 12, tzinfo=TZ).date())

    assert plan["activity_window"]["basis"] == "full day activity review"
    assert [point["line"] for point in plan["sampled_track"]] == [1, 2, 3, 4]


def test_build_plan_uses_home_bracketed_ride_window_when_available() -> None:
    events = [
        location(1, 0, 12.9000, 77.5900, motionactivities=["stationary"]),
        transition(2, 10, 12.9000, 77.5900, "Home", "leave"),
        location(3, 20, 12.9100, 77.6000, motionactivities=["cycling"]),
        location(4, 40, 12.9200, 77.6100, motionactivities=["cycling"]),
        transition(5, 50, 12.9300, 77.6200, "Home", "enter"),
        location(6, 80, 12.9300, 77.6200, motionactivities=["stationary"]),
    ]

    plan, _ = build_plan(events, datetime(2026, 6, 12, tzinfo=TZ).date())

    assert plan["activity_window"]["basis"] == "Home leave to Home enter around ride points"
    assert [point["line"] for point in plan["sampled_track"]] == [3, 4]
