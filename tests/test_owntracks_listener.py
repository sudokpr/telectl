from __future__ import annotations

import json

from owntracks.listener import owntracks_poi_message, parse_mqtt_log_line


def test_parse_mqtt_log_line_and_build_poi_message() -> None:
    payload = {
        "_type": "location",
        "lat": 12.956978,
        "lon": 77.518089,
        "poi": "Paid INR 440 at store",
    }
    line = f"2026-07-08T13:05:00+0530 owntracks/user/device {json.dumps(payload)}"

    parsed = parse_mqtt_log_line(line)

    assert parsed is not None
    stamp, topic, parsed_payload = parsed
    message = owntracks_poi_message(stamp, topic, parsed_payload)
    assert message is not None
    assert "OwnTracks POI" in message
    assert "Paid INR 440 at store" in message
    assert "https://www.google.com/maps?q=12.956978,77.518089" in message


def test_non_poi_location_does_not_build_notification() -> None:
    payload = {"_type": "location", "lat": 12.956978, "lon": 77.518089}
    line = f"2026-07-08T13:05:00+0530 owntracks/user/device {json.dumps(payload)}"

    parsed = parse_mqtt_log_line(line)

    assert parsed is not None
    stamp, topic, parsed_payload = parsed
    assert owntracks_poi_message(stamp, topic, parsed_payload) is None
