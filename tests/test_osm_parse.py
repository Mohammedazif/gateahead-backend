from gateahead.osm import parse_overpass


def test_parse_overpass_splits_crossings_stations_rails():
    payload = {
        "elements": [
            {"type": "node", "id": 1, "lat": 12.14, "lon": 75.17, "tags": {"railway": "level_crossing", "name": "NH66"}},
            {"type": "node", "id": 2, "lat": 12.143, "lon": 75.176, "tags": {"railway": "station", "ref": "TKQ", "name": "Thrikaripur"}},
            {
                "type": "way",
                "id": 3,
                "tags": {"railway": "rail"},
                "geometry": [{"lat": 12.14, "lon": 75.17}, {"lat": 12.15, "lon": 75.17}],
            },
            {"type": "way", "id": 4, "tags": {"highway": "primary"}, "geometry": []},
        ]
    }
    crossings, stations, rails, has_highway = parse_overpass(payload, (12.14, 75.17))
    assert crossings[0].id == "osm:node:1"
    assert stations[0].code == "TKQ"
    assert len(rails[0]) == 2
    assert has_highway


def test_missing_geometry_point_does_not_drop_the_map():
    payload = {
        "elements": [
            {"type": "node", "id": 1, "lat": 28.64, "lon": 77.22, "tags": {"railway": "level_crossing"}},
            {
                "type": "way",
                "id": 9,
                "tags": {"railway": "station", "ref": "NDLS", "name": "New Delhi"},
                "geometry": [None, {"lat": 28.64, "lon": 77.22}, {"lat": None, "lon": 77.22}],
            },
            {
                "type": "way",
                "id": 10,
                "tags": {"railway": "rail"},
                "geometry": [None, {"lat": 28.64, "lon": 77.21}, {"lat": 28.65, "lon": 77.22}],
            },
        ]
    }
    crossings, stations, rails, _has_highway = parse_overpass(payload, (28.64, 77.22))
    assert crossings[0].id == "osm:node:1"
    assert stations[0].code == "NDLS"
    assert len(rails[0]) == 2


def test_station_way_with_ref_is_parsed():
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 9,
                "tags": {"railway": "station", "ref": "PAY", "name": "Payyanur"},
                "geometry": [
                    {"lat": 12.093, "lon": 75.202},
                    {"lat": 12.094, "lon": 75.203},
                ],
            }
        ]
    }
    _c, stations, _r, _h = parse_overpass(payload, (12.08, 75.20))
    assert stations[0].code == "PAY"
    assert stations[0].name == "Payyanur"
