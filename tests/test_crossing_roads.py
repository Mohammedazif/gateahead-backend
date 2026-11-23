import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from gateahead.models import Crossing, Station
from gateahead.osm import OsmAround, OsmClient, _around_query, parse_overpass
from gateahead.service import GateAhead
from gateahead.store import Store


def node(number, longitude, **tags):
    return {"type": "node", "id": number, "lat": 12, "lon": longitude,
            "tags": {"railway": "level_crossing", **tags}}


def road(number, nodes, *, name="Station Road", **tags):
    return {"type": "way", "id": number, "nodes": [n["id"] for n in nodes],
            "geometry": [{"lat": n["lat"], "lon": n["lon"]} for n in nodes],
            "tags": {"highway": "residential", "name": name, **tags}}


def parse(*elements):
    return parse_overpass({"elements": list(elements)}, (12, 75))[0]


def fixture(area):
    return json.loads((Path(__file__).parent / "fixtures" / f"osm_{area}_crossings.json").read_text())


def test_actual_fci_godown_points_stay_hidden_when_access_tags_are_missing():
    payload = fixture("peelamedu")
    private = {f"osm:node:{n['id']}" for n in payload["elements"]
               if n["type"] == "node" and n["tags"].get("access") == "private"}
    assert len(private) == 26
    for element in payload["elements"]:
        if element["tags"].get("access") == "private":
            del element["tags"]["access"]
    crossings, _, rails, _ = parse_overpass(payload, (11.029, 77.001))
    assert not private.intersection(c.id for c in crossings)
    assert {c.id for c in crossings} == {
        "osm:node:296492201", "osm:node:4512893177", "osm:node:4522282475", "osm:node:4522282476",
    }
    # Eligibility does not remove the rail geometry used for route discovery.
    assert rails


def test_actual_public_payyanur_roads_crossing_spurs_remain_and_pair_per_road():
    crossings, _, rails, _ = parse_overpass(fixture("payyanur"), (12.0935, 75.20249))
    found = {c.id: c for c in crossings}
    assert rails
    # Explicit motor_vehicle=no road is outside public vehicle wait coverage.
    assert "osm:node:580724448" not in found
    assert "osm:node:5595616409" not in found
    pairs = [(5999772397, 5999772400), (10268149299, 10268149300),
             (10268149301, 10268149302), (10268149303, 10268149304)]
    groups = set()
    for a, b in pairs:
        first, second = found[f"osm:node:{a}"], found[f"osm:node:{b}"]
        assert first.group_id == second.group_id != ""
        assert first.group_size == second.group_size == 2
        groups.add(first.group_id)
    assert len(groups) == 4


def test_shared_road_groups_parallel_tracks_and_preserves_each_original_id():
    a, b = node(1, 75), node(2, 75.00005)
    crossings = parse(a, b, road(10, [a, b]))
    assert {c.id for c in crossings} == {"osm:node:1", "osm:node:2"}
    assert {c.group_id for c in crossings} == {"road:osm:node:1"}
    assert {c.group_name for c in crossings} == {"Station Road"}
    assert {c.group_size for c in crossings} == {2}
    assert {c.road for c in crossings} == {"Station Road"}
    assert [c.group_id for c in parse(b, a, road(10, [a, b]))] == [c.group_id for c in crossings]


def test_split_road_ways_continue_across_tracks():
    a, mid, b = node(1, 75), node(9, 75.00003), node(2, 75.00006)
    crossings = parse(a, b, road(10, [a, mid]), road(11, [mid, b]))
    assert {c.group_size for c in crossings} == {2}


def test_road_loop_crossing_the_same_rail_twice_keeps_separate_gates():
    a, b = node(1, 75), node(2, 75.00005)
    rail = {"type": "way", "id": 20, "nodes": [1, 2], "tags": {"railway": "rail"},
            "geometry": [{"lat": n["lat"], "lon": n["lon"]} for n in [a, b]]}
    crossings = parse(a, b, road(10, [a, b]), rail)
    assert all(not c.group_id for c in crossings)


def test_nearby_same_name_different_roads_are_not_grouped():
    a, b = node(1, 75), node(2, 75.00005)
    before, after = node(8, 74.9998), node(9, 75.0003)
    crossings = parse(a, b, road(10, [before, a]), road(11, [b, after]))
    assert all(not c.group_id and c.group_size == 1 for c in crossings)


def test_junction_does_not_join_distinct_nearby_crossing_roads():
    a, junction, b, side = node(1, 75), node(9, 75.00005), node(2, 75.0001), node(8, 75.0002)
    side["lat"] += .0001
    crossings = parse(a, b, road(10, [a, junction]), road(11, [junction, b]), road(12, [junction, side]))
    assert all(not c.group_id for c in crossings)


def test_long_road_and_transitive_chains_do_not_form_large_gate_groups():
    a, b, c = node(1, 75), node(2, 75.0006), node(3, 75.0012)
    crossings = parse(a, b, c, road(10, [a, b, c]))
    assert max(c.group_size for c in crossings) == 2
    assert len({c.group_id for c in crossings}) == 2
    assert all(not c.group_id for c in parse(a, c, road(10, [a, c])))


def test_folded_road_cannot_hide_a_long_transitive_group_inside_small_radius():
    a, b, c = node(1, 75), node(2, 75.0004), node(3, 75)
    c["lat"] += .0003
    crossings = parse(a, b, c, road(10, [a, b, c]))
    # Each neighboring pair is under80m and every straight-line pair is
    # under80m, but traversing the entire folded road exceeds80m.
    assert max(c.group_size for c in crossings) == 2


def test_eligibility_uses_connected_evidence_and_specific_access():
    a, b, c, d = (node(i, 75 + i * .0001) for i in range(1, 5))
    private_road = road(10, [a], access="private")
    footpath = road(11, [b], highway="path")
    public_spur = road(12, [c], access="private", motor_vehicle="yes")
    # No connected road evidence keeps an ordinary level crossing. A nearby
    # private road must never hide or label an unrelated public crossing.
    crossings = parse(a, b, c, d, private_road, footpath, public_spur)
    assert {c.id for c in crossings} == {"osm:node:3", "osm:node:4"}
    assert next(c for c in crossings if c.id == "osm:node:4").road == ""
    assert not parse(node(5, 75.001, access="private"))
    assert not parse(node(6, 75.001, railway="crossing"))


def test_only_retained_public_members_count_toward_group_size():
    a, b, private = node(1, 75), node(2, 75.00005), node(3, 75.0001, access="private")
    crossings = parse(a, b, private, road(10, [a, b, private]))
    assert len(crossings) == 2
    assert {c.group_size for c in crossings} == {2}


def test_goods_shed_filters_service_roads_but_keeps_public_roads_across_its_tracks():
    goods, public, open_service, unrelated = (node(i, 75 + i * .0001) for i in range(1, 5))
    for point in (goods, public, open_service, unrelated):
        point["lat"] = 30.9
    rail = {"type": "way", "id": 20, "nodes": [1, 2, 3],
            "tags": {"railway": "rail", "name": "Railway Goods Shed"}}
    payload = {"elements": [goods, public, open_service, unrelated, rail,
                      road(10, [goods], highway="service"),
                      road(11, [public]),
                      road(12, [open_service], highway="service", motor_vehicle="yes"),
                      road(13, [unrelated], highway="service")]}
    crossings, _, _, _ = parse_overpass(payload, (30.9, 75))
    assert {c.id for c in crossings} == {"osm:node:2", "osm:node:3", "osm:node:4"}


def test_map_query_fetches_connected_roads_across_full_crossing_radius():
    query = _around_query(12, 75, 10000)
    assert 'node["railway"="level_crossing"](around:10000,12,75)' in query
    assert 'way(bn.crossings)["highway"]' in query
    assert 'way(bn.crossings)["railway"]' in query
    assert '["name:en"~' in query and '["operator"~' in query and 'godowns?' in query


def test_map_client_carries_exact_rejected_ids_to_saved_crossing_merge(monkeypatch):
    import gateahead.osm as osm
    monkeypatch.setattr(osm, "_cache", {})
    a, private = node(1, 75), node(2, 75.00005, access="private")

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self, **kwargs): return {"elements": [a, private]}

    class Session:
        def post(self, *args, **kwargs): return Response()

    mapped = asyncio.run(OsmClient(Session()).around(12, 75, 10000))
    assert {c.id for c in mapped.crossings} == {"osm:node:1"}
    assert mapped.excluded_crossing_ids == {"osm:node:2"}


def test_station_labels_preserve_groups_and_fresh_exclusions_do_not_resurrect_saved_osm(tmp_path):
    store = Store(tmp_path / "gates.db")
    service = GateAhead(SimpleNamespace(nearby_radius_m=10000), store, None)
    a, b = node(1, 75), node(2, 75.00005)
    crossings = parse(a, b, road(10, [a, b]))
    rejected = Crossing("osm:node:3", "Private yard", 12, 75.01, "osm")
    user_pin = Crossing("user:4", "My saved pin", 12, 75.02, "user", osm_id=rejected.id)
    unrelated = Crossing("static:5", "Other saved crossing", 12, 75.03, "irgeoportal")
    store.save_crossing(rejected)
    store.save_crossing(user_pin)
    store.save_crossing(unrelated)
    service.osm.around = AsyncMock(return_value=OsmAround(
        crossings, [Station("PAY", "Payyanur", 12, 75)], [], True,
        excluded_crossing_ids={rejected.id},
    ))
    try:
        mapped = asyncio.run(service.map_around(12, 75))
        assert {c.id for c in mapped.crossings} == {"osm:node:1", "osm:node:2", user_pin.id, unrelated.id}
        grouped = [c for c in mapped.crossings if c.group_id]
        assert {c.group_size for c in grouped} == {2}
        assert {c.group_name for c in grouped} == {"Station Road · Payyanur"}
        assert {c.nearest_station for c in grouped} == {"PAY"}
        service.osm.around = AsyncMock(return_value=OsmAround([], [], [], False, "map_busy"))
        fallback = asyncio.run(service.map_around(12, 75))
        assert {c.id for c in fallback.crossings} == {user_pin.id, unrelated.id}
        # The exclusion survives a process restart; later positive evidence can
        # restore only map-restricted rows without restoring moderation rejects.
        reopened = Store(store.path)
        try:
            assert {c.id for c in reopened.public_crossings()} == {user_pin.id, unrelated.id}
            reopened.reconcile_crossing_access({rejected.id}, set())
            assert rejected.id in {c.id for c in reopened.public_crossings()}
        finally:
            reopened._conn.close()
    finally:
        store._conn.close()


def test_gates_api_exposes_group_metadata_without_collapsing_original_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path))
    import gateahead.api.app as api
    a, b = node(1, 75), node(2, 75.00005)
    crossings = parse(a, b, road(10, [a, b]))
    service = AsyncMock()
    service.nearby.return_value = ([(c, 0) for c in crossings], None)
    monkeypatch.setattr(api, "_service", service)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            body = (await client.get("/gates", params={"lat": 12, "lon": 75})).json()
            assert {g["id"] for g in body["gates"]} == {"osm:node:1", "osm:node:2"}
            assert {g["group_id"] for g in body["gates"]} == {"road:osm:node:1"}
            assert {g["group_size"] for g in body["gates"]} == {2}
            assert {g["road"] for g in body["gates"]} == {"Station Road"}
    asyncio.run(run())


def test_fci_crossings_are_hidden_from_map_and_details_even_when_saved(monkeypatch, tmp_path):
    monkeypatch.setattr("gateahead.osm._cache", {})
    import gateahead.api.app as api
    payload = fixture("peelamedu")

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def json(self, **kwargs): return payload

    class Session:
        def post(self, *args, **kwargs): return Response()

    store = Store(tmp_path / "gates.db")
    freight_id = "osm:node:4518094360"
    freight_point = (11.0344613, 76.9973631)
    store.save_crossing(Crossing("ir:1", "FCI Godown", *freight_point, "irgeoportal", osm_id=freight_id))
    store.save_crossing(Crossing("ir:2", "Level crossing", *freight_point, "irgeoportal"))
    service = GateAhead(SimpleNamespace(nearby_radius_m=10000), store, Session())
    service._positions = AsyncMock()
    monkeypatch.setattr(api, "_service", service)

    async def run():
        area = {"lat": 11.029, "lon": 77.001}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            body = (await client.get("/gates", params=area)).json()
            assert {g["id"] for g in body["gates"]} == {
                "osm:node:296492201", "osm:node:4512893177",
                "osm:node:4522282475", "osm:node:4522282476",
            }
            for crossing_id in (freight_id, "ir:1", "ir:2"):
                scene = await service.scene_here(**area, crossing_id=crossing_id)
                assert scene["error"] == "crossing_not_found"
                assert scene["trains"] == []
            assert not store.public_crossings()
            service._positions.assert_not_called()

    try:
        asyncio.run(run())
        store.reconcile_crossing_access({freight_id}, set(), public_points={freight_point})
        assert {c.id for c in store.public_crossings()} == {"ir:1", "ir:2"}
    finally:
        store._conn.close()
