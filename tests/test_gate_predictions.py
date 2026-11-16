import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

from gateahead.config import load_settings
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
