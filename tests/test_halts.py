from pathlib import Path

from smartrail.halts import halt_kind, parse_station_catalog
from smartrail.models import Halt, Station
from smartrail.service import known_points
from smartrail.store import Store


def _feature(code: str, name: str, lon: float | None, lat: float | None, zone: str = "SR") -> dict:
    geometry = None if lon is None else {"type": "Point", "coordinates": [lon, lat]}
    return {"type": "Feature", "geometry": geometry, "properties": {"code": code, "name": name, "zone": zone}}


def test_catalog_keeps_halts_and_stations_with_coordinates():
    payload = {
        "type": "FeatureCollection",
        "features": [
            _feature("KLNA", "KOLHANA HALT", 85.1887, 24.8108),
            _feature("PAY", "Payyanur", 75.202, 12.093),
            _feature("KMB", "Kalv Amba", None, None),
            _feature("BAD", "Outside", 10.0, 50.0),
            _feature("pay", "Payyanur duplicate", 75.21, 12.10),
        ],
    }
    halts = {halt.code: halt for halt in parse_station_catalog(payload)}
    assert set(halts) == {"KLNA", "PAY"}
    assert halts["KLNA"].kind == "halt"
    assert halts["PAY"].kind == "station"
    assert halt_kind("SULAH HIMACHAL PRADESH") == "station"
    assert halts["PAY"].lat == 12.10


def test_nearby_halts_come_from_our_store(tmp_path: Path):
    store = Store(tmp_path / "halts.db")
    store.upsert_halts(
        [
            Halt("PAY", "Payyanur", 12.093, 75.202, "station", "SR", "datameet"),
            Halt("CHV", "Charvattur Halt", 12.11, 75.19, "halt", "SR", "datameet"),
            Halt("NDLS", "New Delhi", 28.64, 77.22, "station", "NR", "datameet"),
        ]
    )
    store.meta_set("catalog_source", "datameet")
    near = store.halts_near(12.10, 75.20, 15, 10, "halt")
    assert [halt.code for halt, _km in near] == ["CHV"]
    assert store.halt("pay").name == "Payyanur"
    assert store.search_halts("char", 5)[0].code == "CHV"
    page = store.halts_page(10, 0)
    assert [halt.code for halt in page] == ["CHV", "NDLS", "PAY"]
    assert store.meta("catalog_source") == "datameet"


def test_a_blank_zone_is_filled_without_touching_a_known_one(tmp_path: Path):
    store = Store(tmp_path / "zones.db")
    store.upsert_halts(
        [
            Halt("PAY", "Payyanur", 12.093, 75.202, "station", "", "datameet"),
            Halt("CAN", "Kannur", 11.87, 75.37, "station", "SR", "osm"),
        ]
    )
    filled = store.fill_blank_zones({"PAY": "SR", "CAN": "WR", "missing": "NR"})
    assert filled == 1
    assert store.halt("PAY").zone == "SR"
    assert store.halt("CAN").zone == "SR"


def test_catalog_coordinates_fill_a_station_the_map_missed():
    osm = [Station("CAN", "Kannur", 12.00, 75.20)]
    extra = [(Halt("TLY", "Thalassery", 11.75, 75.49, "station"), 30.0)]
    points = known_points(osm, extra)
    assert points["CAN"] == (12.00, 75.20)
    assert points["TLY"] == (11.75, 75.49)
    fresher = known_points([Station("TLY", "Thalassery", 11.76, 75.50)], extra)
    assert fresher["TLY"] == (11.76, 75.50)
