"""Paired successful/failing-provider workflows, without network requests."""

import asyncio
from datetime import datetime
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from gateahead.config import load_settings
from gateahead.clock import INDIA
from gateahead.live import LiveLook
from gateahead.models import Crossing, Station
from gateahead.osm import OsmAround, OsmClient, parse_overpass
from gateahead.service import GateAhead, _discover_trains_from
from gateahead.store import Store
from gateahead.timetable import parse_route_rows


GOOD_MAP = {"elements": [{"type": "node", "id": 4, "lat": 12.1, "lon": 75.2,
                          "tags": {"railway": "level_crossing"}}]}


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return self.body

    async def text(self):
        return self.body


class _MapSession:
    def __init__(self, bodies):
        self.bodies = iter(bodies)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return _Response(next(self.bodies))


@pytest.mark.parametrize("broken", [None, [], {}, {"elements": {}},
    {"elements": [], "remark": "runtime error: Query timed out"},
    {"elements": [None]},
    {"elements": [{"type": "node", "lat": "invalid", "lon": 75.2}]},
    {"elements": [{"type": "node", "lat": float("nan"), "lon": 75.2,
                   "tags": {"railway": "level_crossing"}}]},
    {"elements": [{"type": "node", "lat": 12.1, "lon": 75.2, "tags": []}]},
    {"elements": [{"type": "node", "lat": 12.1, "lon": 75.2, "tags": {"railway": []}}]},
])
def test_bad_successful_map_reply_fails_over_instead_of_crashing_or_caching_empty(broken, monkeypatch):
    monkeypatch.setattr("gateahead.osm._cache", {})
    session = _MapSession([broken, GOOD_MAP])
    result = asyncio.run(OsmClient(session).around(12.1, 75.2, 10000))
    assert result.error is None
    assert [gate.id for gate in result.crossings] == ["osm:node:4"]
    assert session.calls == 2
    # Reopening the same place uses the recovered response, not the failure.
    again = asyncio.run(OsmClient(session).around(12.1, 75.2, 10000))
    assert [gate.id for gate in again.crossings] == ["osm:node:4"]
    assert session.calls == 2


def test_valid_empty_map_is_success_but_all_malformed_maps_remain_retryable(monkeypatch):
    monkeypatch.setattr("gateahead.osm._cache", {})
    empty = _MapSession([{"elements": []}])
    result = asyncio.run(OsmClient(empty).around(12.1, 75.2, 10000))
    assert result.error is None and result.crossings == [] and empty.calls == 1
    bad = _MapSession([{}, {}, {}, GOOD_MAP])
    failed = asyncio.run(OsmClient(bad).around(13.1, 75.2, 10000))
    assert failed.error == "map_unavailable"
    recovered = asyncio.run(OsmClient(bad).around(13.1, 75.2, 10000))
    assert recovered.error is None and recovered.crossings
    assert bad.calls == 4


@pytest.mark.parametrize("coords", [(91, 75), (12, 181), ("inf", 75), (12, "nan")])
def test_nonfinite_or_out_of_range_map_geometry_is_not_an_occupied_track(coords):
    with pytest.raises(ValueError):
        parse_overpass({"elements": [{"type": "way", "tags": {"railway": "rail"},
            "geometry": [{"lat": 12, "lon": 75}, {"lat": coords[0], "lon": coords[1]}]}]}, (12, 75))


def _stops():
    return parse_route_rows([
        {"station_code": "AAA", "distance_from_source": 0, "std_min": 600, "lat": 12, "lng": 75.2},
        {"station_code": "BBB", "distance_from_source": 30, "sta_min": 630, "lat": 12.3, "lng": 75.2},
    ])


@pytest.mark.parametrize("error", [TimeoutError("snapshot lease timed out"), ValueError("unreadable provider data")])
def test_one_failed_live_check_does_not_hide_other_trains(tmp_path, monkeypatch, error):
    service = GateAhead.__new__(GateAhead)
    service.settings = replace(load_settings(), live_check_budget=2)
    service.store = Store(tmp_path / "live.db")
    service.session = None
    service.snapshots = None
    service._discover_trains = AsyncMock(return_value=["16512", "16513"])
    service._routes = AsyncMock(return_value={})
    monkeypatch.setattr("gateahead.service.cached_live", lambda *args: None)

    async def fetch(session, number, cache):
        if number == "16512":
            raise error
        return LiveLook({"success": True, "current_station_code": "AAA", "status": "D",
                         "cur_stn_lat": 12, "cur_stn_lng": 75.2, "status_as_of_min": 1}, "", False)

    monkeypatch.setattr("gateahead.service.fetch_live", fetch)
    try:
        batch = asyncio.run(service._positions([], 12.15, 75.2))
        assert [row.train_number for row in batch.positions] == ["16513"]
        assert batch.coverage["live_checked"] == 2
        assert batch.coverage["unavailable_live"] == 1
        assert batch.coverage["live_reports"] == 1
        assert batch.coverage["pending_live"] == 0
    finally:
        service.store._conn.close()


def test_one_bad_departure_board_keeps_other_discovered_services(monkeypatch):
    async def board(session, code, name):
        if code == "AAA":
            raise UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid byte")
        return [("16513", 600)]

    monkeypatch.setattr("gateahead.service.station_rows", board)
    monkeypatch.setattr("gateahead.service.train_rows", AsyncMock(return_value=[("16512", 600)]))
    result = asyncio.run(_discover_trains_from(None, [Station("AAA", "South", 12, 75.2),
                                                        Station("BBB", "North", 12.3, 75.2)]))
    assert set(result) == {"16512", "16513"}


def test_failed_timetable_refresh_preserves_successful_routes_and_backs_off(tmp_path, monkeypatch):
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "routes.db")
    service.session = None

    async def timetable(session, number):
        if number == "16512":
            raise UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid byte")
        return _stops(), "MON"

    fetch = AsyncMock(side_effect=timetable)
    monkeypatch.setattr("gateahead.service.fetch_timetable", fetch)
    try:
        result = asyncio.run(service._routes(["16512", "16513"]))
        assert set(result) == {"16513"}
        assert service.store.route_refresh_allowed("16512") is False
        assert service.store.load_route("16513", 168).run_days == "MON"
        again = asyncio.run(service._routes(["16512", "16513"]))
        assert set(again) == {"16513"}
        assert fetch.await_count == 2
    finally:
        service.store._conn.close()


def test_one_abandoned_location_check_does_not_cancel_another_users_shared_report(tmp_path, monkeypatch):
    """A user changes area while another person is waiting on the same train."""
    import gateahead.live as live

    async def run():
        store = Store(tmp_path / "shared.db")
        service = GateAhead(replace(load_settings(), data_dir=tmp_path), store, None)
        service._discover_trains = AsyncMock(return_value=["16512"])
        service._routes = AsyncMock(return_value={})
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(session, number):
            calls.append(number)
            entered.set()
            await release.wait()
            return LiveLook(None, "", False)

        monkeypatch.setattr(live, "_fetch_live_data", provider)
        try:
            abandoned = asyncio.create_task(service._positions([], 12, 75.2))
            survivor = asyncio.create_task(service._positions([], 12.2, 75.2))
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.sleep(0)
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
            release.set()
            completed = await asyncio.wait_for(survivor, timeout=1)
            assert completed.coverage["live_checked"] == 1
            assert completed.coverage["unavailable_live"] == 1
            assert calls == ["16512"]
            # A retry reuses the shared result, including its original fetch time.
            stamp = service.snapshots.peek("live:16512")["fetched_at"]
            await service._positions([], 12, 75.2)
            assert service.snapshots.peek("live:16512")["fetched_at"] == stamp
            assert calls == ["16512"]
        finally:
            await service.snapshots.shutdown()
            store._conn.close()

    asyncio.run(run())


def test_unexpected_programming_error_is_not_silently_reported_as_provider_outage(tmp_path, monkeypatch):
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "unexpected.db")
    service.session = None
    service.snapshots = None
    service._discover_trains = AsyncMock(return_value=["16512"])
    monkeypatch.setattr("gateahead.service.cached_live", lambda *args: None)
    monkeypatch.setattr("gateahead.service.fetch_live", AsyncMock(side_effect=RuntimeError("internal bug")))
    try:
        with pytest.raises(RuntimeError, match="internal bug"):
            asyncio.run(service._positions([], 12, 75.2))
    finally:
        service.store._conn.close()


@pytest.mark.parametrize("kind", ["board", "pair"])
@pytest.mark.parametrize("status", [429, 500, 502])
def test_discovery_http_failure_is_not_a_valid_empty_response(kind, status, monkeypatch):
    from gateahead import erail

    monkeypatch.setattr(erail, "_cache", {})
    broken = _Response("unavailable")
    broken.status = status
    healthy = _Response("<p>REGIONAL EXPRESS16512, 01.00 hr</p><p>10:00</p>" if kind == "board"
                        else "~~~~~~^16512~REGIONAL EXPRESS~AAA~BBB")

    class Session:
        def __init__(self):
            self.responses = iter([broken, broken, healthy] if kind == "board" else [broken, healthy])

        def get(self, *args, **kwargs):
            return next(self.responses)

    session = Session()
    lookup = (lambda: erail.station_rows(session, "AAA", "South")) if kind == "board" else (
              lambda: erail.train_rows(session, "AAA", "BBB"))
    failed = asyncio.run(lookup())
    assert failed == [] and failed.unavailable is True
    recovered = asyncio.run(lookup())
    assert recovered[0][0] == "16512"
    assert getattr(recovered, "unavailable", False) is False


def test_unreadable_pair_page_is_retryable_but_delimited_empty_pair_is_valid(monkeypatch):
    from gateahead import erail

    monkeypatch.setattr(erail, "_cache", {})

    class Session:
        def __init__(self):
            self.bodies = iter(["<html>Loading...</html>", "~~~~~~"])
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return _Response(next(self.bodies))

    session = Session()
    unknown = asyncio.run(erail.train_rows(session, "AAA", "BBB"))
    assert unknown.unavailable is True
    empty = asyncio.run(erail.train_rows(session, "AAA", "BBB"))
    assert empty == [] and getattr(empty, "unavailable", False) is False
    assert asyncio.run(erail.train_rows(session, "AAA", "BBB")) == []
    assert session.calls == 2


def test_indexed_route_remains_visible_when_provider_discovery_is_down(tmp_path, monkeypatch):
    from gateahead.erail import TrainRows

    service = GateAhead(load_settings(), Store(tmp_path / "discovery.db"), None)
    service.store.save_route("16512", _stops())
    stations = [Station("AAA", "South", 12, 75.2), Station("BBB", "North", 12.3, 75.2)]
    service.map_around = AsyncMock(return_value=OsmAround(
        [Crossing("gate", "Gate", 12.15, 75.2, "osm")], stations, [[(12, 75.2), (12.3, 75.2)]], True))
    look = LiveLook({"success": True, "current_station_code": "AAA", "status": "D",
                    "cur_stn_lat": 12, "cur_stn_lng": 75.2, "status_as_of_min": 1,
                    "distance_from_source": 0, "train_name": "Regional Express"}, "", False)
    monkeypatch.setattr("gateahead.service.station_rows", AsyncMock(return_value=TrainRows(unavailable=True)))
    monkeypatch.setattr("gateahead.service.train_rows", AsyncMock(return_value=TrainRows(unavailable=True)))
    monkeypatch.setattr("gateahead.service.classify_passage", lambda *args: "hot")
    monkeypatch.setattr("gateahead.service.cached_live", lambda number, cache=None, start=None:
                        look if start is None or start == datetime.now(INDIA).date() else LiveLook(None, "", True))
    fetch = AsyncMock()
    monkeypatch.setattr("gateahead.service.fetch_live", fetch)
    try:
        prediction = asyncio.run(service.predict_here(12.15, 75.2))
        assert prediction["coverage"]["unavailable_discovery"] > 0
        assert {row["train"] for row in prediction["gate_predictions"]} == {"16512"}
        scene = asyncio.run(service.scene_here(12.15, 75.2, "gate"))
        assert scene["coverage"]["unavailable_discovery"] == prediction["coverage"]["unavailable_discovery"]
        assert [row["number"] for row in scene["trains"]] == ["16512"]
        fetch.assert_not_awaited()
    finally:
        service.store._conn.close()


def test_discovery_outage_counts_belong_to_each_request(monkeypatch):
    from gateahead.erail import TrainRows

    async def board(session, code, name):
        await asyncio.sleep(0)
        return TrainRows(unavailable=True) if code == "AAA" else [("16512", 600)]

    monkeypatch.setattr("gateahead.service.station_rows", board)
    monkeypatch.setattr("gateahead.service.train_rows", AsyncMock(return_value=[]))

    async def run():
        failed, healthy = await asyncio.gather(
            _discover_trains_from(None, [Station("AAA", "South", 12, 75.2)]),
            _discover_trains_from(None, [Station("BBB", "North", 12.3, 75.2)]))
        assert failed == [] and failed.unavailable == 1
        assert healthy == ["16512"] and healthy.unavailable == 0

    asyncio.run(run())


@pytest.mark.parametrize("sightings", [[], [{"number": "16512", "name": "Express", "age": 1,
    "passed_local": False, "current": "South", "at_station": True, "delay": 0}]])
def test_discovery_failure_cannot_claim_a_clear_line_in_telegram(sightings):
    from gateahead.bot.formatters import predict_text

    text = predict_text({"approaching": [], "sightings": sightings,
                         "coverage": {"unavailable_discovery": 2}})
    assert "Some train searches could not be completed" in text
    assert "The barrier’s position is unknown" in text
    assert "No train is close enough" not in text
    assert "No live trains reported" not in text
