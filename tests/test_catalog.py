import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gateahead.catalog import sync_catalog
from gateahead.config import load_settings
from gateahead.live import LiveLook
from gateahead.models import Crossing, Halt, Station
from gateahead.osm import OsmAround
from gateahead.service import GateAhead, PositionBatch, _discover_trains_from
from gateahead.store import Store
from gateahead.timetable import _best_delta, _run_now, parse_route_rows


def stops(lon=75.2):
    return parse_route_rows([
        {"station_code": "AAA", "station_name": "South", "distance_from_source": 0,
         "std_min": 600, "day": 1, "lat": 12, "lng": lon},
        {"station_code": "BBB", "station_name": "North", "distance_from_source": 30,
         "sta_min": 630, "std_min": 631, "day": 1, "lat": 12.3, "lng": lon},
    ])


def test_discovery_keeps_every_provider_candidate(monkeypatch):
    numbers = [str(10000 + i) for i in range(75)]
    monkeypatch.setattr("gateahead.service.station_rows", AsyncMock(return_value=[(n, 600) for n in numbers]))
    monkeypatch.setattr("gateahead.service.train_rows", AsyncMock(return_value=[]))
    found = asyncio.run(_discover_trains_from(None, [Station("AAA", "South", 12, 75.2)]))
    assert found == numbers


def test_spatial_index_keeps_more_than_200_routes_and_replaces_old_segments(tmp_path):
    store = Store(tmp_path / "index.db")
    for i in range(205):
        store.save_route(str(10000 + i), stops())
    assert len(store.route_numbers_near(12.15, 75.2, 1, 168)) == 205
    store.save_route("10000", stops(lon=77))
    assert "10000" not in store.route_numbers_near(12.15, 75.2, 1, 168)
    assert store.route_numbers_near(12.15, 77, 1, 168) == ["10000"]
    assert store._conn.execute("SELECT COUNT(*) FROM route_segment_bounds").fetchone()[0] == 205


def test_index_migration_preserves_route_freshness(tmp_path):
    path = tmp_path / "old.db"
    old = Store(path)
    old.save_route("16512", stops())
    old._conn.execute("UPDATE train_routes SET fetched_at='2020-01-01T00:00:00'")
    old._conn.execute("DELETE FROM route_segments")
    old._conn.execute("DELETE FROM meta WHERE key='route_segment_index'")
    old._conn.commit()
    old._conn.close()
    migrated = Store(path)
    assert migrated.route_numbers_near(12.15, 75.2, 1, 168) == []
    assert migrated.route_numbers_near(12.15, 75.2, 1, None) == ["16512"]
    assert migrated.load_route("16512", 168) is None
    assert migrated.catalog_status()["stale_routes"] == 1


def test_sync_resumes_with_unscanned_stations_and_unfetched_trains(tmp_path, monkeypatch):
    store = Store(tmp_path / "sync.db")
    store.upsert_halts([Halt("AAA", "South", 12, 75.2), Halt("BBB", "North", 12.3, 75.2)])
    board = AsyncMock(side_effect=[[("16512", 600), ("16513", 630)], [("16514", 650)]])
    timetable = AsyncMock(return_value=(stops(), "MON,TUE"))
    monkeypatch.setattr("gateahead.catalog.station_rows", board)
    monkeypatch.setattr("gateahead.catalog.fetch_timetable", timetable)
    first = asyncio.run(sync_catalog(None, store, station_limit=1, route_limit=1, request_interval=0))
    assert first["routes_refreshed"] == 1
    assert store.catalog_status()["missing_routes"] == 1
    second = asyncio.run(sync_catalog(None, store, station_limit=1, route_limit=2, request_interval=0))
    assert second["routes_refreshed"] == 2
    assert [call.args[1] for call in board.await_args_list] == ["AAA", "BBB"]
    assert store.catalog_status()["missing_routes"] == 0
    assert store.catalog_status()["stations_scanned"] == 2
    assert len(list(store.station_coverage())) == 2
    assert store.catalog_status()["national_coverage_verified"] is False


def test_refresh_failure_keeps_old_route_and_backs_off(tmp_path, monkeypatch):
    store = Store(tmp_path / "failure.db")
    store.save_route("16512", stops(), "SAT")
    store._conn.execute("UPDATE train_routes SET fetched_at='2020-01-01T00:00:00'")
    store._conn.commit()
    fetch = AsyncMock(return_value=([], ""))
    monkeypatch.setattr("gateahead.catalog.fetch_timetable", fetch)
    result = asyncio.run(sync_catalog(None, store, station_limit=0, route_limit=10, request_interval=0))
    assert result["status"] == "partial"
    assert store.load_route("16512", None).run_days == "SAT"
    assert store.load_route("16512", 168) is None
    assert store.due_routes(10, 24) == []
    assert store.route_refresh_allowed("16512") is False
    assert store.catalog_status()["failed_refreshes"] == 1
    assert Store(store.path).route_refresh_allowed("16512") is False
    asyncio.run(sync_catalog(None, store, station_limit=0, route_limit=10, request_interval=0))
    fetch.assert_awaited_once()
    with pytest.raises(ValueError):
        store.save_route("16512", [])
    assert len(store.load_route("16512", None).stops) == 2


def test_empty_boards_remain_unverified_and_provider_outage_stops_after_three(tmp_path, monkeypatch):
    store = Store(tmp_path / "outage.db")
    store.upsert_halts([Halt(code, code, 12, 75) for code in ("AAA", "BBB", "CCC", "DDD")])
    board = AsyncMock(return_value=[])
    monkeypatch.setattr("gateahead.catalog.station_rows", board)
    result = asyncio.run(sync_catalog(None, store, station_limit=10, route_limit=0, request_interval=0))
    assert result["station_errors"] == 3
    assert board.await_count == 3
    assert store.catalog_status()["stations_scanned"] == 0
    assert [s.code for s in store.due_stations(10, 168)] == ["DDD"]


def test_refresh_lease_is_shared_across_connections_and_released_on_cancellation(tmp_path, monkeypatch):
    store = Store(tmp_path / "lease.db")
    store.remember_train_candidates(["16512"])

    async def run():
        entered = asyncio.Event()

        async def fetch(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("gateahead.catalog.fetch_timetable", fetch)
        task = asyncio.create_task(sync_catalog(None, store, station_limit=0, request_interval=0))
        await entered.wait()
        other = Store(store.path)
        assert await sync_catalog(None, other, station_limit=0, request_interval=0) == {"status": "busy"}
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert other.claim_catalog("next-worker")
        other.release_catalog("next-worker")

    asyncio.run(run())


def test_live_budget_rotates_through_all_indexed_candidates(tmp_path, monkeypatch):
    service = GateAhead.__new__(GateAhead)
    service.settings = replace(load_settings(), live_check_budget=3)
    service.store = Store(tmp_path / "budget.db")
    service.session = None
    service.snapshots = None
    service._discover_trains = AsyncMock(return_value=[])
    service._routes = AsyncMock(return_value={})
    numbers = [str(10000 + i) for i in range(9)]
    for number in numbers:
        service.store.save_route(number, stops())
    monkeypatch.setattr("gateahead.service.classify_passage", lambda *args: "hot")
    monkeypatch.setattr("gateahead.service.cached_live", lambda *args: None)
    fetch = AsyncMock(return_value=LiveLook(None, "", True))
    monkeypatch.setattr("gateahead.service.fetch_live", fetch)
    first = asyncio.run(service._positions([], 12.15, 75.2))
    assert first.numbers == numbers
    assert first.coverage["live_checked"] == 3
    assert first.coverage["pending_live"] == first.coverage["live_candidates"] - 3
    assert first.coverage["limited"] is True
    asyncio.run(service._positions([], 12.15, 75.2))
    asyncio.run(service._positions([], 12.15, 75.2))
    assert {call.args[1] for call in fetch.await_args_list} == set(numbers)


def test_fresh_snapshots_do_not_consume_live_budget(tmp_path, monkeypatch):
    service = GateAhead.__new__(GateAhead)
    service.settings = replace(load_settings(), live_check_budget=1)
    service.store = Store(tmp_path / "snapshots.db")
    service.session = None
    service.snapshots = None
    service._discover_trains = AsyncMock(return_value=["16512", "16513"])
    service._routes = AsyncMock(return_value={})
    monkeypatch.setattr("gateahead.service.cached_live", lambda n, *args: LiveLook(None, "", True) if n == "16512" else None)
    fetch = AsyncMock(return_value=LiveLook(None, "", True))
    monkeypatch.setattr("gateahead.service.fetch_live", fetch)
    result = asyncio.run(service._positions([], 12.15, 75.2))
    assert result.coverage["live_checked"] == 2
    assert result.coverage["pending_live"] == 0
    fetch.assert_awaited_once()
    assert fetch.await_args.args[1] == "16513"


def test_origin_operating_days_work_on_day_three_and_four():
    monday = datetime(2026, 11, 16, 10)
    assert _best_delta(2 * 1440 + 600, "SAT", monday)[0] == 0
    assert _best_delta(3 * 1440 + 600, "FRI", monday)[0] == 0
    assert _run_now(monday, 3 * 1440 + 600, 0) == 3 * 1440 + 600


def test_current_moving_report_survives_schedule_exclusion(tmp_path, monkeypatch):
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "late.db")
    service.store.save_route("16512", stops(), "SAT")
    service._discover_trains = AsyncMock(return_value=[])
    look = LiveLook({"success": True, "current_station_code": "AAA", "cur_stn_lat": 12,
                     "cur_stn_lng": 75.2, "status": "T", "delay": 600}, "", False)
    monkeypatch.setattr("gateahead.service.cached_live", lambda *args: look)
    monkeypatch.setattr("gateahead.service.classify_passage", lambda *args: "skip")
    fetch = AsyncMock()
    monkeypatch.setattr("gateahead.service.fetch_live", fetch)
    batch = asyncio.run(service._positions([], 12.15, 75.2))
    assert [p.train_number for p in batch.positions] == ["16512"]
    assert batch.coverage["live_reports"] == 1
    fetch.assert_not_awaited()


def test_simultaneous_locations_keep_their_own_coverage(tmp_path):
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "locations.db")
    service.map_around = AsyncMock(return_value=OsmAround(
        [Crossing("g", "Gate", 12.15, 75.2, "osm")], [], [], False,
    ))

    async def positions(stations, lat, lon):
        await asyncio.sleep(0)
        return PositionBatch([], [], {}, {"live_checked": int(lat), "pending_live": int(lat) + 1})

    service._positions = positions

    async def run():
        first, second = await asyncio.gather(service.predict_here(12, 75.2), service.predict_here(13, 75.2))
        assert first["coverage"]["pending_live"] == 13
        assert second["coverage"]["pending_live"] == 14
        assert first["checked"] == 12
        assert second["checked"] == 13
        scene = await service.scene_here(12, 75.2, "g")
        assert scene["coverage"]["pending_live"] == 13

    asyncio.run(run())
