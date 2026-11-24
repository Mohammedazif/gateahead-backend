import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx

from gateahead.config import load_settings
from gateahead.clock import INDIA
from gateahead.crossing_details import CrossingDetails
from gateahead.models import Crossing, CrossingModes
from gateahead.osm import OsmAround, parse_overpass
from gateahead.service import GateAhead, PositionBatch
from gateahead.store import Store


def test_access_uses_connected_road_modes_and_crossing_restrictions():
    crossing = {"type": "node", "id": 1, "lat": 12.1, "lon": 75.2,
                "tags": {"railway": "level_crossing", "motorcycle": "no"}}
    exact_road = {"type": "way", "id": 2, "nodes": [1, 4],
                  "tags": {"highway": "residential", "foot": "designated"}}
    nearby_bridge = {"type": "way", "id": 3, "nodes": [5, 6],
                     "tags": {"highway": "residential", "bridge": "yes", "motorcar": "no"}}
    elements = [crossing, exact_road, nearby_bridge]
    gates, *_ = parse_overpass({"elements": elements}, (12.1, 75.2))
    assert gates[0].modes == CrossingModes("allowed", "restricted", "allowed")

    exact_road["tags"]["motorcar:conditional"] = "no @ (Mo-Fr 08:00-09:00)"
    split_road = {"type": "way", "id": 4, "nodes": [7, 1],
                  "tags": {"highway": "residential", "foot": "no"}}
    gates, *_ = parse_overpass({"elements": [*elements, split_road]}, (12.1, 75.2))
    assert gates[0].modes == CrossingModes("unknown", "restricted", "unknown")


def test_verified_bypass_is_tied_to_crossing_identity_location_and_date(tmp_path):
    crossing = Crossing("osm:node:1", "Gate", 12.1, 75.2, "osm", modes=CrossingModes(cars="allowed"))
    yesterday = (datetime.now(INDIA).date() - timedelta(days=1)).isoformat()
    evidence = {"verified_on": yesterday, "evidence": "https://roads.example/inspection"}
    bypass = {"kind": "underpass", "name": "Station underpass", "lat": 12.101, "lon": 75.2,
              "replacement": True}
    access = {"cars": "restricted", "two_wheelers": "allowed", "pedestrians": "allowed"}
    (tmp_path / "crossing-details.json").write_text(json.dumps({crossing.id: {
        "lat": crossing.lat, "lon": crossing.lon,
        "access": {**access, **evidence},
        "bypasses": [
            {**bypass, **evidence}, {**bypass, **evidence},
            {**bypass, **evidence, "lat": 12.102, "verified_on": "2099-01-01"},
            {**bypass, **evidence, "lat": 12.103, "verified_on": "2020-01-01"},
        ],
    }}))
    details = CrossingDetails(tmp_path)
    assert details.for_crossing(crossing) == {
        **access, "source": "verified", "verified_on": evidence["verified_on"],
        "bypasses": [{**bypass, "verified_on": evidence["verified_on"]}],
    }
    for mismatch in (replace(crossing, id="osm:node:2"), replace(crossing, lat=12.2)):
        assert details.for_crossing(mismatch) == {
            "cars": "allowed", "two_wheelers": "unknown", "pedestrians": "unknown",
            "source": "mapped", "bypasses": [],
        }


def test_default_catalog_packages_the_verified_kerala_pilot(tmp_path):
    catalog = CrossingDetails(tmp_path)
    with_bypasses = {
        record.bypasses[0].name: record.bypasses[0].evidence.host
        for record in catalog.verified.values()
    }
    assert with_bypasses == {
        "Kottachery Rail Overbridge": "www.openstreetmap.org",
        "Cheruvathur–Padanna Rail Overbridge": "www.openstreetmap.org",
    }
    assert len(catalog.verified) == 6
    (tmp_path / "crossing-details.json").write_text("{}")
    override = CrossingDetails(tmp_path)
    assert override.verified == {}
    assert override.for_crossing(Crossing("ir:1234", "Gate", 12.1, 75.2, "irgeoportal")) == {
        "cars": "unknown", "two_wheelers": "unknown", "pedestrians": "unknown",
        "source": "unavailable", "bypasses": [],
    }


def test_scene_and_outdated_flags_use_existing_crossing_and_keep_reports_private(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path))
    from gateahead.api import app as api

    from gateahead.reports import Reports

    store = Store(tmp_path / "gates.db")
    service = GateAhead(replace(load_settings(), data_dir=tmp_path), store, None)
    crossing = Crossing("osm:node:2", "Gate", 12.1, 75.2, "osm",
                        modes=CrossingModes("allowed", "allowed", "restricted"))
    service.osm.around = AsyncMock(return_value=OsmAround([crossing], [], [], True))
    service._positions = AsyncMock(return_value=PositionBatch([], [], {}, {"status": "complete"}))
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "_service", service)
    monkeypatch.setattr(api, "_reports", Reports(store.path))
    monkeypatch.setattr(api, "_HITS", {})

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            gate_request = {"crossing_id": crossing.id, "lat": crossing.lat, "lon": crossing.lon}
            detail = (await client.get("/crossing-details", params=gate_request)).json()
            assert detail["crossing"]["id"] == crossing.id
            assert detail["crossing_access"]["cars"] == "allowed"
            missing = await client.get("/crossing-details", params={**gate_request, "lat": 12.2})
            assert missing.status_code == 404
            assert missing.json()["detail"]["code"] == "crossing_not_found"
            assert service._positions.await_count == 0
            scene = (await client.get("/scene", params=gate_request)).json()
            assert scene["available"] is False
            assert scene["crossing_access"] == {
                "cars": "allowed", "two_wheelers": "allowed", "pedestrians": "restricted",
                "source": "mapped", "bypasses": [],
            }
            flag_path = "/crossing-details/flags"
            submission = {**gate_request, "installation_id": "test-installation"}
            for _ in range(2):
                response = await client.post(flag_path, json=submission)
                assert response.status_code == 202
                assert response.json() == {"accepted": True}
            assert (await client.post(flag_path, json={**submission, "lat": 12.2})).status_code == 404
            service.osm.around.return_value = OsmAround([crossing], [], [], True, map_warning="map_busy")
            assert (await client.post(flag_path, json=submission)).status_code == 503
            detail = await client.get("/crossing-details", params=gate_request)
            assert detail.status_code == 200 and detail.json()["map_warning"] == "map_busy"
            assert service._positions.await_count == 1
            assert not (await client.get("/reports", params={"crossing_id": crossing.id})).json()["reports"]

    asyncio.run(run())
    rows = store._conn.execute("SELECT crossing_id, reporter FROM crossing_detail_flags").fetchall()
    assert len(rows) == 1
    assert rows[0]["crossing_id"] == crossing.id
    assert rows[0]["reporter"] != "test-installation"
    with store._conn:
        store._conn.execute("UPDATE crossing_detail_flags SET created_at=0")
    store.prune_retained_data()
    assert not store._conn.execute("SELECT 1 FROM crossing_detail_flags").fetchone()


def test_node_permission_requires_usable_approaches():
    crossing = {"type": "node", "id": 1, "lat": 12.1, "lon": 75.2,
                "tags": {"railway": "level_crossing", "motorcar": "yes", "foot": "yes"}}
    approaches = [
        {"type": "way", "id": 2, "nodes": [1, 4],
         "tags": {"highway": "residential", "motorcar": "no", "foot": "no"}},
        {"type": "way", "id": 3, "nodes": [5, 1],
         "tags": {"highway": "residential", "motorcar": "no", "foot": "designated"}},
    ]
    gates, *_ = parse_overpass({"elements": [crossing, *approaches]}, (12.1, 75.2))
    assert gates[0].modes == CrossingModes("restricted", "allowed", "unknown")
    approaches[0]["tags"] = {"highway": "residential", "motorcar:conditional": "no @ (Mo-Fr 08:00-09:00)"}
    approaches[1]["tags"]["motorcar"] = "yes"
    gates, *_ = parse_overpass({"elements": [crossing, *approaches]}, (12.1, 75.2))
    assert gates[0].modes == CrossingModes("unknown", "allowed", "unknown")
