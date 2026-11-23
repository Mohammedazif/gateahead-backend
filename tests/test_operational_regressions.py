import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

import httpx

from gateahead import osm
from gateahead.bot.formatters import nearby_text, predict_text, reply_pages
from gateahead.cities import search_cities
from gateahead.config import load_settings
from gateahead.halts import ensure_catalog
from gateahead.live import LiveLook
from gateahead.models import Crossing, TrainPosition
from gateahead.reports import Reports
from gateahead.service import GateAhead, PositionBatch
from gateahead.store import Store
from gateahead.timetable import Call


def make_service(tmp_path):
    return GateAhead(replace(load_settings(), data_dir=tmp_path), Store(tmp_path / "test.db"), None)


def test_legacy_public_pin_keeps_scene_and_reports_without_exposing_old_id(tmp_path, monkeypatch):
    from gateahead.api import app as api

    service = make_service(tmp_path)
    old = "user:123000789:1700000000"
    private = "user:123000789:1700000001"
    public_gate = Crossing(old, "Public pin", 12.15, 75.2, "user")
    service.store.save_crossing(public_gate, added_by=123000789)
    service.store.save_crossing(replace(public_gate, id=private, status="pending"), added_by=123000789)
    reports = Reports(service.store.path)
    reports.submit(old, "test-installation", "closed")
    service.store.migrate_crossing_ids()
    service.store.migrate_crossing_ids()
    canonical = service.store.canonical_crossing_id(old)
    assert canonical.startswith("pin:") and canonical != old
    monkeypatch.setattr(api, "_store", service.store)
    monkeypatch.setattr(api, "_service", service)
    monkeypatch.setattr(api, "_reports", reports)
    monkeypatch.setattr(api, "_HITS", {})
    service.osm.around = AsyncMock(return_value=osm.OsmAround([], [], [[(12.13, 75.2), (12.17, 75.2)]], True))
    service._positions = AsyncMock(return_value=PositionBatch([], [], {}, {"status": "complete"}))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            coordinates = {"lat": 12.15, "lon": 75.2}
            gates = (await client.get("/gates", params=coordinates)).json()
            assert [item["id"] for item in gates["gates"]] == [canonical]
            scene = (await client.get("/scene", params={**coordinates, "crossing_id": old})).json()
            assert scene["available"] and scene["legacy_alias_resolved"]
            assert scene["crossing"]["id"] == canonical
            assert old not in json.dumps(scene)
            missing = (await client.get("/scene", params={**coordinates, "crossing_id": private})).json()
            assert missing["error"] == "crossing_not_found"
            before = (await client.get("/reports", params={"crossing_id": old})).json()
            assert before["reports"][0]["state"] == "closed"
    asyncio.run(check())


def test_no_road_submission_stays_private_and_uses_opaque_id(tmp_path):
    service = make_service(tmp_path)
    service.osm.around = AsyncMock(return_value=osm.OsmAround([], [], [[(12.14, 75.2), (12.16, 75.2)]], False))

    async def check():
        pin = (await service.mark_gate(123000789, 12.15, 75.2)).match
        assert pin and pin.id.startswith("pin:") and "123000789" not in pin.id
        assert pin.status == "pending"
        assert (await service.map_around(12.15, 75.2)).crossings == []
    asyncio.run(check())


def test_saved_map_on_outage_is_marked_partial_in_all_responses(tmp_path, monkeypatch):
    from gateahead.api import app as api

    service = make_service(tmp_path)
    crossing = Crossing("osm:node:1", "Stored crossing", 12.15, 75.2, "osm")
    service.store.save_crossing(crossing)
    service.osm.around = AsyncMock(return_value=osm.OsmAround([], [], [], False, "map_busy"))
    service._positions = AsyncMock(return_value=PositionBatch([], [], {}, {"status": "complete"}))
    monkeypatch.setattr(api, "_service", service)
    monkeypatch.setattr(api, "_HITS", {})

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            coordinates = {"lat": 12.15, "lon": 75.2}
            gates = (await client.get("/gates", params=coordinates)).json()
            assert gates["gates"][0]["id"] == crossing.id and gates["map_warning"] == "map_busy"
            prediction = (await client.get("/predict", params=coordinates)).json()
            assert prediction["map_warning"] == "map_busy"
            assert prediction["coverage"]["status"] == "partial"
            assert "barrier’s position is unknown" in predict_text(prediction)
            scene = (await client.get("/scene", params={**coordinates, "crossing_id": crossing.id})).json()
            assert not scene["available"] and scene["error"] == "map_busy"
            submission = await client.post("/reports", json={
                **coordinates, "crossing_id": crossing.id, "installation_id": "test-installation", "state": "closed",
            })
            assert submission.status_code == 503
    asyncio.run(check())


def test_map_mirrors_recover_from_bad_geometry(monkeypatch):
    monkeypatch.setattr(osm, "_cache", {})
    urls = []
    node = {"type": "node", "id": 1, "lat": 12.15, "lon": 75.2, "tags": {"railway": "level_crossing"}}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, **kwargs):
            road = {"type": "way", "id": 2, "tags": {"highway": "primary"}, "geometry": 17}
            return {"elements": [node, road] if len(urls) == 1 else [node]}

    class Session:
        def post(self, url, **kwargs):
            urls.append(url)
            return Response()

    result = asyncio.run(osm.OsmClient(Session()).around(12.15, 75.2, 10_000))
    assert len(urls) == 2 and result.error is None
    assert result.crossings[0].id == "osm:node:1"


def test_bad_station_catalog_preserves_existing_stations(tmp_path, monkeypatch):
    from gateahead import halts

    monkeypatch.setattr(halts, "_failed_at", 0.0)
    store = Store(tmp_path / "catalog.db")
    from gateahead.models import Halt
    store.upsert_halts([Halt("PAY", "Payyanur", 12.15, 75.2)])

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return '{"features": 2}'

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    assert asyncio.run(ensure_catalog(Session(), store)) == 1
    assert store.halt("PAY").name == "Payyanur"


def test_tracking_deadline_keeps_finished_checks(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service._discover_trains = AsyncMock(return_value=["12345", "67890"])
    service._routes = AsyncMock(return_value={})

    async def fetch(_session, number, _cache):
        if number == "67890":
            await asyncio.Event().wait()
        return LiveLook(None, "", True)

    monkeypatch.setattr("gateahead.service.fetch_live", fetch)

    async def check():
        loop = asyncio.get_running_loop()
        batch = await asyncio.wait_for(service._positions([], 12.15, 75.2, deadline=loop.time() + 2.1), timeout=1)
        assert batch.coverage["live_checked"] == 1
        assert batch.coverage["pending_live"] == 1
        assert batch.coverage["status"] == "partial" and batch.coverage["deadline_exhausted"]
    asyncio.run(check())


def test_only_the_crossing_with_passage_evidence_is_reported(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    crossing = Crossing("first", "First road", 12.15, 75.2, "osm")
    other = replace(crossing, id="second", name="Second road", lat=12.1501)
    position = TrainPosition("12345", 12.15, 75.2, None, None, False, 0, "northbound", datetime.now())
    service.map_around = AsyncMock(return_value=osm.OsmAround([crossing, other], [], [], True))
    service._positions = AsyncMock(return_value=PositionBatch(["12345"], [position], {}, {"status": "complete"}))
    monkeypatch.setattr("gateahead.service._crossing_eta",
                        lambda _train, gate, *_args: Call(None, None, "rail", 0, ago=3 if gate == crossing else None))
    prediction = asyncio.run(service.predict_here(12.15, 75.2))
    assert [row["ids"] for row in prediction["passed"]] == [["first"]]


def test_map_read_yields_when_another_connection_is_writing(tmp_path):
    service = make_service(tmp_path)
    service.osm.around = AsyncMock(return_value=osm.OsmAround(
        [Crossing("osm:node:1", "Mapped crossing", 12.15, 75.2, "osm")], [], [], False,
    ))

    async def check():
        writer = sqlite3.connect(service.store.path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            map_read = asyncio.create_task(service.map_around(12.15, 75.2))
            await asyncio.sleep(0)
            assert not map_read.done()
            writer.commit()
            result = await asyncio.wait_for(map_read, timeout=0.5)
            assert result.crossings[0].id == "osm:node:1"
        finally:
            writer.close()
    asyncio.run(check())


def test_maintenance_prunes_expired_rows_and_preserves_an_active_fetch(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    reports = Reports(service.store.path)
    clock = [100_000.0]
    monkeypatch.setattr("gateahead.reports.time.time", lambda: clock[0])
    reports.submit("osm:node:1", "test-installation", "closed")
    db = service.snapshots.db
    db.execute("INSERT INTO provider_snapshots VALUES ('old','{}',1,NULL,0)")
    db.execute("INSERT INTO provider_snapshots VALUES ('leased',NULL,0,'worker',?)", (clock[0] + 700,))
    clock[0] += Reports.ttl
    service.store.prune_retained_data()
    assert reports.db.execute("SELECT COUNT(*) FROM gate_reports").fetchone()[0] == 0
    assert db.execute("SELECT key FROM provider_snapshots").fetchall() == [("leased",)]


def test_city_lookup_recovers_common_typos_and_normalizes_local_names():
    assert search_cities("bengluru")[0].name == "Bengaluru"
    assert search_cities("Coim batore")[0].name == "Coimbatore"
    assert search_cities("New-Delhi")[0].name == "New Delhi"
    assert search_cities("കൊച്ചി")[0].name == "Kochi"
    assert search_cities("कोलकाता")[0].name == "Kolkata"
    assert search_cities("16650") == []


def test_nearby_list_splits_large_telegram_messages_at_complete_lines():
    crossings = [
        (Crossing(str(i), f"Public crossing {i}", 12 + (i // 9) * .005, 75 + (i % 9) * .005, "osm"), 1.0)
        for i in range(72)
    ]
    text = nearby_text(crossings)
    pages = reply_pages(text)
    assert len(pages) > 1
    assert all(len(page) <= 4096 for page in pages)
    assert "\n".join(pages) == text
