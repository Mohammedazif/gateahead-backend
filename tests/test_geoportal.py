import json
from pathlib import Path

from gateahead.geoportal import collect_crossings, collect_stations, load_geoportal
from gateahead.models import Crossing
from gateahead.service import merge_saved_crossings
from gateahead.store import Store


def _line(props: dict) -> str:
    return json.dumps({"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [0, 0]}})


def test_ir_stations_keep_one_coded_row_and_its_zone():
    lines = [
        _line(
            {
                "objectid": 2,
                "sttncode": "pay",
                "sttnname": "PAYYANUR",
                "sttntype": " ",
                "railway": "SR",
                "xcoord": 75.20,
                "ycoord": 12.09,
            }
        ),
        _line(
            {
                "objectid": 9,
                "sttncode": "DMYA",
                "sttnname": "DAMOYA",
                "sttntype": "HALT",
                "railway": "WCR",
                "xcoord": 80.99,
                "ycoord": 23.97,
                "centerline_of_stn": "Y",
            }
        ),
        _line(
            {
                "objectid": 1,
                "sttncode": "DMYA",
                "sttnname": "OTHER",
                "railway": "WCR",
                "xcoord": 81.10,
                "ycoord": 24.10,
            }
        ),
        _line({"objectid": 3, "sttncode": " ", "sttnname": "NO CODE", "xcoord": 75.2, "ycoord": 12.1, "railway": "SR"}),
        _line({"objectid": 4, "sttncode": "OUT", "sttnname": "OUT", "xcoord": 10, "ycoord": 50, "railway": "NR"}),
        _line(
            {
                "objectid": 5,
                "sttncode": "BQF",
                "sttnname": "BARAH MAUFI",
                "railway": "METRO RAILWAY",
                "latitude": "25°2'33.8352\"N",
                "longitude": "81°1'14.9424\"E",
            }
        ),
    ]
    halts, zones, skipped = collect_stations(lines)
    by_code = {halt.code: halt for halt in halts}
    assert set(by_code) == {"PAY", "DMYA", "BQF"}
    assert by_code["DMYA"].name == "DAMOYA"
    assert by_code["DMYA"].kind == "halt"
    assert by_code["DMYA"].lat == 23.97
    assert by_code["PAY"].source == "irgeoportal"
    assert by_code["BQF"].zone == ""
    assert abs(by_code["BQF"].lat - 25.0427) < 0.001
    assert zones["PAY"] == "SR"
    assert zones["DMYA"] == "WCR"
    assert "BQF" not in zones
    assert skipped == 2


def test_only_level_crossings_are_kept():
    lines = [
        _line({"objectid": 1, "layer": "Cutting End", "fdetail": "Cutting End", "xcoord": 75.2, "ycoord": 12.1}),
        _line(
            {
                "objectid": 2,
                "layer": "Level Xing",
                "fdetail": "Level Crossing",
                "remark": "LC NO.73",
                "stnfrom": "SWM",
                "xcoord": 75.800514,
                "ycoord": 26.788767,
            }
        ),
        _line({"objectid": 3, "layer": "Level Xing", "fdetail": "LC  JC/50", "xcoord": 86.14593, "ycoord": 22.99702}),
        _line({"objectid": 4, "layer": "Point Xing", "fdetail": "Point", "xcoord": 75.2, "ycoord": 12.1}),
        _line({"objectid": 5, "layer": "Level Xing", "fdetail": "Level Crossing", "xcoord": None, "ycoord": None}),
    ]
    crossings, skipped = collect_crossings(lines)
    assert skipped == 1
    assert [crossing.id for crossing in crossings] == ["ir:2", "ir:3"]
    assert crossings[0].name == "LC NO.73"
    assert crossings[0].nearest_station == "SWM"
    assert crossings[0].source == "irgeoportal"
    assert crossings[0].status == "public"
    assert crossings[1].name == "LC JC/50"


def test_the_dump_does_not_replace_a_known_halt_or_a_user_pin(tmp_path: Path):
    store = Store(tmp_path / "rail.db")
    stations = tmp_path / "stations.geojsonl"
    pois = tmp_path / "pois.geojsonl"
    stations.write_text(
        "\n".join(
            [
                _line(
                    {
                        "objectid": 1,
                        "sttncode": "PAY",
                        "sttnname": "FROM IR",
                        "railway": "WR",
                        "xcoord": 75.21,
                        "ycoord": 12.10,
                    }
                ),
                _line(
                    {
                        "objectid": 2,
                        "sttncode": "DMYA",
                        "sttnname": "DAMOYA",
                        "sttntype": "HALT",
                        "railway": "WCR",
                        "xcoord": 80.99,
                        "ycoord": 23.97,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    pois.write_text(
        "\n".join(
            [
                _line({"objectid": 7, "layer": "Level Xing", "fdetail": "LC 1", "xcoord": 75.202, "ycoord": 12.093}),
                _line({"objectid": 8, "layer": "Bridge Minor", "fdetail": "bridge", "xcoord": 75.2, "ycoord": 12.1}),
            ]
        ),
        encoding="utf-8",
    )
    from gateahead.models import Halt

    store.upsert_halts([Halt("PAY", "Payyanur", 12.093, 75.202, "station", "", "datameet")])
    pin = Crossing("user:1:2", "Crossing near PAY", 12.093, 75.202, "user", "pending", "PAY")
    store.save_crossing(pin, added_by=1)
    store.insert_crossings([Crossing("user:1:2", "replaced", 1.0, 2.0, "irgeoportal", "public")])
    summary = load_geoportal(store, stations, pois)
    assert summary["stations_added"] == 1
    assert summary["zones_filled"] == 1
    assert summary["crossings_added"] == 1
    kept = store.halt("PAY")
    assert kept is not None
    assert kept.name == "Payyanur"
    assert kept.source == "datameet"
    assert kept.zone == "WR"
    assert kept.lat == 12.093
    added = store.halt("DMYA")
    assert added is not None and added.source == "irgeoportal" and added.kind == "halt"
    near = store.crossings_near(12.093, 75.202, 500)
    assert {item.id for item in near} == {"user:1:2", "ir:7"}
    assert next(item for item in near if item.id == "user:1:2").name == "Crossing near PAY"
    assert store.crossings_near(28.6, 77.2, 1000) == []
    again = load_geoportal(store, stations, pois)
    assert again["stations_added"] == 0
    assert again["crossings_added"] == 0
    assert again["zones_filled"] == 0


def test_a_saved_gate_beside_an_osm_gate_is_not_drawn_twice():
    osm = [Crossing("osm:node:1", "NH66", 12.093, 75.202, "osm")]
    same = Crossing("ir:1", "Level crossing", 12.0931, 75.2021, "irgeoportal")
    other = Crossing("ir:2", "Level crossing", 12.12, 75.20, "irgeoportal")
    pin = Crossing("user:1:2", "Mine", 12.093, 75.202, "user", "pending")
    merged = merge_saved_crossings(osm, [same, other, pin])
    assert [item.id for item in merged] == ["osm:node:1", "ir:2", "user:1:2"]
