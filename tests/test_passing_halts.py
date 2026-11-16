from dataclasses import replace
from datetime import datetime

import pytest

from gateahead.config import load_settings
from gateahead.gatecall import crossing_call, passed_ago
from gateahead.models import Crossing, TrainPosition
from gateahead.timetable import list_status, parse_route_rows


def _stops():
    # The train's published schedule has only these two calls. There is no
    # timetable row or stopping requirement for any of the intermediate gates.
    return parse_route_rows([
        {"station_code": "SOUTH", "station_name": "South", "distance_from_source": 0, "std_min": 1020, "day": 1, "lat": 12, "lng": 75.2},
        {"station_code": "NORTH", "station_name": "North", "distance_from_source": 30, "sta_min": 1050, "std_min": 1051, "day": 1, "lat": 12.3, "lng": 75.2},
    ])


@pytest.mark.parametrize("code,gate_km", [("ELM", 6), ("TKQ", 12), ("CHV", 18)])
def test_a_gate_gets_a_closure_estimate_even_when_the_train_does_not_stop_at_its_station(code, gate_km):
    gate = Crossing(code, "Gate by a skipped station", 12 + gate_km / 100, 75.2, "osm", nearest_station=code)
    train = TrainPosition("16512", 12 + (gate_km - 2) / 100, 75.2, "SOUTH", "NORTH", False, 0,
                          "northbound", datetime(2026, 11, 16, 17), covered_km=gate_km - 2)
    settings = load_settings()
    rails = [[(12, 75.2), (12.3, 75.2)]]
    stations = {"SOUTH": (12, 75.2), "NORTH": (12.3, 75.2)}
    call = crossing_call(train, gate, rails, stations, settings, _stops())
    assert call.eta == pytest.approx(2)
    assert list_status(call.eta, call.slack, settings, call.source == "timetable") == "closed"
    assert code not in {stop.code for stop in _stops()}
    # The same gate ceases to be approaching once the train has gone over it.
    past = replace(train, lat=12 + (gate_km + 4) / 100, covered_km=gate_km + 4)
    gone = crossing_call(past, gate, rails, stations, settings, _stops())
    assert gone.eta is None
    assert passed_ago(gone) is not None


def test_a_train_on_a_different_route_does_not_close_the_gate():
    gate = Crossing("g", "Other line", 12.15, 76, "osm", nearest_station="OTHER")
    train = TrainPosition("16512", 12.13, 75.2, "SOUTH", "NORTH", False, 0, "northbound",
                          datetime(2026, 11, 16, 17), covered_km=13)
    call = crossing_call(train, gate, [[(12, 76), (12.3, 76)]], {}, load_settings(), _stops())
    assert call.eta is None
