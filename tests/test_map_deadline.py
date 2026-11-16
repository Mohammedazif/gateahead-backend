"""Cold map failover must finish before the browser abandons the request."""

import asyncio
from types import SimpleNamespace

import pytest

import gateahead.osm as osm


BODY = {"elements": [{"type": "node", "id": 4, "lat": 12.1, "lon": 75.2,
                       "tags": {"railway": "level_crossing"}}]}
KEY = (12.1, 75.2, 10000)


class FakeSession:
    def __init__(self, clock, outcomes):
        self.clock = clock
        self.outcomes = iter(outcomes)
        self.timeouts = []

    def post(self, *args, **kwargs):
        timeout = kwargs["timeout"]
        self.timeouts.append(timeout.total)
        outcome = next(self.outcomes)
        session = self

        class Response:
            status = 200

            async def __aenter__(self):
                if outcome == "timeout":
                    session.clock[0] += timeout.total
                    raise TimeoutError("map mirror timed out")
                session.clock[0] += outcome
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self, **kwargs):
                return BODY

        return Response()


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(osm, "time", SimpleNamespace(monotonic=lambda: value[0]))
    monkeypatch.setattr(osm, "_cache", {})
    return value


def test_all_cold_mirrors_share_one_deadline_below_browser_timeout(clock):
    session = FakeSession(clock, ["timeout", "timeout", "timeout"])
    result = asyncio.run(osm.OsmClient(session).around(*KEY))
    assert result.error == "map_busy"
    assert session.timeouts == [30, 30, 15]
    assert clock[0] - 1000 == 75
    assert clock[0] - 1000 < 90
    assert KEY not in osm._cache


def test_last_mirror_can_recover_inside_remaining_budget_and_warm_lookup_needs_no_network(clock):
    session = FakeSession(clock, ["timeout", "timeout", 14])
    client = osm.OsmClient(session)
    result = asyncio.run(client.around(*KEY))
    assert result.error is None
    assert [gate.id for gate in result.crossings] == ["osm:node:4"]
    assert clock[0] - 1000 == 74
    assert session.timeouts == [30, 30, 15]
    assert asyncio.run(client.around(*KEY)) == result
    assert session.timeouts == [30, 30, 15]


def test_deadline_preserves_stale_map_without_making_it_fresh(clock):
    crossing, stations, rails, highway = osm.parse_overpass(BODY, KEY[:2])
    previous = osm.OsmAround(crossing, stations, rails, highway)
    stale_at = clock[0] - osm._CACHE_TTL - 1
    osm._cache[KEY] = (stale_at, previous)
    session = FakeSession(clock, ["timeout", "timeout", "timeout"])
    assert asyncio.run(osm.OsmClient(session).around(*KEY)) is previous
    assert clock[0] - 1000 == 75
    assert osm._cache[KEY][0] == stale_at


def test_overall_deadline_is_enforced_even_if_transport_ignores_its_timeout(monkeypatch):
    monkeypatch.setattr(osm, "_cache", {})
    monkeypatch.setattr(osm, "_LOOKUP_BUDGET", 0.02)
    calls = []

    class Session:
        def post(self, *args, **kwargs):
            calls.append(kwargs["timeout"].total)

            class Response:
                async def __aenter__(self):
                    await asyncio.Event().wait()

                async def __aexit__(self, *args):
                    return False

            return Response()

    async def run():
        result = await asyncio.wait_for(osm.OsmClient(Session()).around(*KEY), timeout=0.5)
        assert result.error == "map_busy"
        assert len(calls) == 1
        assert 0 < calls[0] <= 0.02

    asyncio.run(run())


@pytest.mark.parametrize("cached", [False, True], ids=["cold", "stale-cache"])
def test_user_cancellation_stops_failover_instead_of_becoming_a_map_result(monkeypatch, cached):
    monkeypatch.setattr(osm, "_cache", {})
    if cached:
        osm._cache[KEY] = (osm.time.monotonic() - osm._CACHE_TTL - 1, osm.OsmAround([], [], [], False))

    async def run():
        started = asyncio.Event()
        calls = []

        class Session:
            def post(self, *args, **kwargs):
                calls.append(1)

                class Response:
                    async def __aenter__(self):
                        started.set()
                        await asyncio.Event().wait()

                    async def __aexit__(self, *args):
                        return False

                return Response()

        task = asyncio.create_task(osm.OsmClient(Session()).around(*KEY))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1

    asyncio.run(run())
