from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from owntracks.tagger import (
    Event,
    HomeFilterConfig,
    StopJitterAnchor,
    StopJitterFilterConfig,
    build_plan,
    candidate_stops,
    filter_stop_jitter_points,
    point_dicts,
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

    assert [event.line_no for event in filtered] == [1, 2, 4, 5, 6, 7, 8, 9]
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


def test_point_dicts_include_waypoint_labels() -> None:
    points = [location(1, 0, 12.9569, 77.5181, motionactivities=["stationary"])]
    events = [waypoint(2, -10, 12.95691, 77.51811, "Home"), *points]

    rendered = point_dicts(points, events)

    assert rendered[0]["place_name"] == "Home"


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
