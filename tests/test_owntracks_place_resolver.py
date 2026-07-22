from owntracks.place_resolver import overpass_query, parse_overpass_candidates


def test_parse_overpass_candidates_prefers_poi_over_roads_and_boundaries() -> None:
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "center": {"lat": 12.95172, "lon": 77.52092},
                "tags": {"highway": "trunk", "name": "Outer Ring Road"},
            },
            {
                "type": "relation",
                "id": 2,
                "center": {"lat": 12.95124, "lon": 77.51903},
                "tags": {"boundary": "administrative", "name": "Nagarbhavi"},
            },
            {
                "type": "node",
                "id": 3,
                "lat": 12.95149,
                "lon": 77.52089,
                "tags": {"shop": "car_repair", "name": "Sri Maruthi Car Water Service"},
            },
        ]
    }

    candidates = parse_overpass_candidates(payload, 12.9514638, 77.5209029)

    assert candidates[0]["name"] == "Sri Maruthi Car Water Service"
    assert candidates[0]["category"] == "shop:car_repair"
    assert candidates[0]["distance_m"] <= 5
    assert "shop-car-repair" in candidates[0]["tags"]


def test_overpass_query_uses_around_radius_and_coordinates() -> None:
    query = overpass_query(12.9514638, 77.5209029, 120)

    assert "node(around:120,12.9514638,77.5209029)[name]" in query
    assert "way(around:120,12.9514638,77.5209029)[name]" in query
    assert "relation(around:120,12.9514638,77.5209029)[name]" in query
