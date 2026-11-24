import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

from gateahead.config import load_settings
from gateahead.live import LiveLook
from gateahead.models import Crossing, TrainPosition
from gateahead.osm import OsmAround
from gateahead.service import GateAhead
from gateahead.store import Store
from gateahead.timetable import Call


def test_each_gate_uses_its_own_eta_and_a_passed_gate_is_excluded(tmp_path, monkeypatch):
    # Three gates in one cluster; the train has passed the first one.
    gates = [Crossing(str(i), f"Gate {i}", 12 + i * 0.001, 75, "osm") for i in range(3)]
    train = TrainPosition("16323", 12, 75, "AAA", "BBB", False, 0, "northbound", datetime.now(), name="Express")
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "gates.db")
    service.map_around = AsyncMock(return_value=OsmAround(gates, [], [], False))
    service._positions = AsyncMock(return_value=(["16323"], [train], {}))
    calls = {
        "0": Call(None, None, "rail", 0, ago=2),
        "1": Call(3, None, "rail", 0),
        "2": Call(5, None, "rail", 0),
    }
    monkeypatch.setattr("gateahead.service._crossing_eta", lambda position, crossing, *args: calls[crossing.id])
    result = asyncio.run(service.predict_here(12, 75))
    assert [(row["id"], row["eta"]) for row in result["gate_predictions"]] == [("1", 3), ("2", 5)]
    assert result["approaching"][0]["ids"] == ["1", "2"]
    assert result["approaching"][0]["count"] == 2
    assert result["gate_predictions"][0]["closes_in"] != result["gate_predictions"][1]["closes_in"]


def test_train_estimates_are_published_while_another_live_report_is_pending(tmp_path, monkeypatch):
    service = GateAhead(load_settings(), Store(tmp_path / "progress.db"), None)
    gate = Crossing("gate", "School Road", 12, 75.2, "osm")
    service.map_around = AsyncMock(return_value=OsmAround([gate], [], [], True))
    service._discover_trains = AsyncMock(return_value=["16512", "16513"])
    service._routes = AsyncMock(return_value={})
    monkeypatch.setattr("gateahead.service._crossing_eta", lambda *args: Call(3, None, "rail", 0))
    look = LiveLook({"success": True, "current_station_code": "AAA", "status": "D",
                     "cur_stn_lat": 12, "cur_stn_lng": 75.2, "status_as_of_min": 1,
                     "distance_from_source": 0, "train_name": "Regional Express"}, "", False)

    async def run():
        release = asyncio.Event()
        published = asyncio.Event()
        results = []

        async def fetch(_session, number, _cache):
            if number == "16513":
                await release.wait()
            return look

        def on_update(update):
            results.append(update)
            published.set()

        monkeypatch.setattr("gateahead.service.fetch_live", fetch)
        request = asyncio.create_task(service.predict_here(*gate.coords, on_update=on_update))
        try:
            await asyncio.wait_for(published.wait(), timeout=1)
            assert not request.done()
            assert [row["number"] for row in results[0]["sightings"]] == ["16512"]
            assert results[0]["approaching"] == []
            assert results[0]["coverage"]["checking"] is True
            assert results[0]["coverage"]["pending_live"] == 1
            release.set()
            final = await asyncio.wait_for(request, timeout=1)
            assert {row["train"] for row in final["approaching"]} == {"16512", "16513"}
            assert not final["coverage"].get("checking")
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            await service.snapshots.shutdown()
            service.store._conn.close()

    asyncio.run(run())
