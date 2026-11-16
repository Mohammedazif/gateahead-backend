import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

from gateahead.config import load_settings
from gateahead.models import SavedRoute, TrainPosition
from gateahead.service import GateAhead, name_position
from gateahead.store import Store
from gateahead.timetable import parse_route_rows


def _route():
    return SavedRoute(parse_route_rows([
        {"station_code": "MAQ", "station_name": "Mangaluru Central", "distance_from_source": "0", "std_min": 300, "day": 1, "lat": 12.87, "lng": 74.88},
        {"station_code": "CAN", "station_name": "Kannur", "distance_from_source": "130", "sta_min": 420, "day": 1, "lat": 11.87, "lng": 75.37},
    ]))


def _position():
    return TrainPosition("16630", 12.0, 75.2, "MAQ", "CAN", False, 0, "southbound", datetime.now())


def test_names_survive_restart_and_blank_payloads_do_not_erase_them(tmp_path):
    path = tmp_path / "names.db"
    first = Store(path)
    first.remember_train_names({"16630": " MALABAR  EXPRESS "})
    first.remember_train_names({"16630": "", "16603": "16603"})
    second = Store(path)
    assert second.train_names(["16630", "16603"]) == {"16630": "MALABAR EXPRESS"}
    assert second.train_names([]) == {}


def test_official_names_are_preferred_to_route_descriptions():
    assert name_position(_position(), "MALABAR EXPRESS", _route()).name == "MALABAR EXPRESS"
    live = replace(_position(), name="Malabar Express")
    assert name_position(live, "Older cached name", _route()) is live
    numeric = replace(_position(), name="16630")
    assert name_position(numeric, "MALABAR EXPRESS", _route()).name == "MALABAR EXPRESS"


def test_route_is_a_fallback_when_no_official_name_is_available():
    assert name_position(_position(), "", _route()).name == "Mangaluru Central → Kannur"
    assert name_position(_position(), "", None).name == ""


def test_timetable_only_positions_receive_discovered_names_without_live_calls(tmp_path, monkeypatch):
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "names.db")
    service._discover_trains = AsyncMock(return_value=["16630"])
    service._cached_routes = lambda numbers: {"16630": _route()}
    service._routes = AsyncMock(return_value={})
    monkeypatch.setattr("gateahead.service.known_train_names", lambda numbers: {"16630": "MALABAR EXPRESS"})
    from gateahead.runs import RunRequest
    monkeypatch.setattr("gateahead.service.requests_for", lambda *args: [RunRequest("16630", cold=True)])
    monkeypatch.setattr("gateahead.service.scheduled_position", lambda *args: _position())
    fetch_live = AsyncMock()
    monkeypatch.setattr("gateahead.service.fetch_live", fetch_live)
    _, positions, _ = asyncio.run(service._positions([], 12, 75.2))
    assert positions[0].name == "MALABAR EXPRESS"
    assert service.store.train_names(["16630"]) == {"16630": "MALABAR EXPRESS"}
    fetch_live.assert_not_awaited()
