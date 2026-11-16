from dataclasses import replace
from datetime import datetime

import pytest

from gateahead.config import load_settings
from gateahead.gatecall import _on_separate_mapped_line, crossing_call, passed_ago
from gateahead.models import Crossing, TrainPosition
from gateahead.scene import build_scene
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


def _bent_leg():
    # Only the distant junctions are published stops. The mapped local rail
    # bends six kilometres west of their straight connecting line.
    stops = parse_route_rows([
        {"station_code": "NORTH", "distance_from_source": 0, "sta_min": 600, "std_min": 600,
         "lat": 12.4, "lng": 75.1},
        {"station_code": "SOUTH", "distance_from_source": 50, "sta_min": 650, "std_min": 651,
         "lat": 11.9657477, "lng": 75.0916628},
    ])
    gate = Crossing("g", "Gate at a skipped halt", 12.1434, 75.0378, "osm", nearest_station="SKIP")
    rail = [(12.2442, 75.0562), gate.coords, (12.0506, 75.0601)]
    train = TrainPosition("16512", 12.2, 75.05, "NORTH", "SOUTH", False, 0, "southbound",
                          datetime(2026, 11, 16, 10, 24), covered_km=23, age_minutes=1, run="departed")
    return stops, gate, rail, train


def test_long_nonstop_leg_uses_a_physically_compatible_local_rail_bend():
    stops, gate, rail, train = _bent_leg()
    call = crossing_call(train, gate, [rail], {}, load_settings(), stops)
    assert call.source == "timetable"
    assert call.eta == pytest.approx(5.4, abs=0.2)
    assert call.bracket == "between NORTH and SOUTH"
    assert call.slack > 3
    assert list_status(call.eta, call.slack, load_settings(), True) == "approaching"
    scene = build_scene(gate, [rail], [train], {}, load_settings(), {train.train_number: stops})
    assert scene["trains"][0]["status"] == "approaching"
    assert "motion" not in scene["trains"][0]
    past = crossing_call(replace(train, covered_km=34), gate, [rail], {}, load_settings(), stops)
    assert past.eta is None
    assert passed_ago(past) == pytest.approx(5.6, abs=0.2)


def test_bent_nonstop_leg_retains_the_reverse_train_direction():
    stops, gate, rail, train = _bent_leg()
    reverse = [replace(stops[1], seq=0, km=0, arrive_min=600, depart_min=600),
               replace(stops[0], seq=1, km=50, arrive_min=650, depart_min=650)]
    northbound = replace(train, last_station="SOUTH", next_station="NORTH", direction="northbound", covered_km=14)
    call = crossing_call(northbound, gate, [list(reversed(rail))], {}, load_settings(), reverse)
    assert call.eta == pytest.approx(5.6, abs=0.2)
    assert call.bracket == "between SOUTH and NORTH"


def test_stale_stopped_report_on_a_bent_nonstop_leg_remains_unresolved():
    stops, gate, rail, train = _bent_leg()
    stopped = replace(train, at_station=True, run="at", covered_km=0, age_minutes=14)
    call = crossing_call(stopped, gate, [rail], {}, load_settings(), stops)
    assert call.awaiting_update is True
    assert call.eta is None
    assert passed_ago(call) is None


def test_bent_leg_inference_cannot_move_a_train_onto_a_disconnected_parallel_branch():
    stops = parse_route_rows([
        {"station_code": "A", "distance_from_source": 0, "sta_min": 600, "std_min": 600,
         "lat": 12.4, "lng": 75.1},
        {"station_code": "B", "distance_from_source": 50, "sta_min": 650, "std_min": 650,
         "lat": 11.96575, "lng": 75.1},
    ])
    gate = Crossing("branch", "Parallel branch", 12.1434, 75.05, "osm")
    branch = [(12.28, 75.05), gate.coords, (12.01, 75.05)]
    main = [(12.4, 75.1), (11.96575, 75.1)]
    train = TrainPosition("16512", 12.16, 75.1, "A", "B", False, 0, "southbound",
                          datetime(2026, 11, 16, 10, 27), covered_km=27, age_minutes=0, run="departed")
    call = crossing_call(train, gate, [main, branch], {}, load_settings(), stops)
    assert call.eta is None
    assert passed_ago(call) is None
    assert not call.awaiting_update
    scene = build_scene(gate, [main, branch], [train], {}, load_settings(), {train.train_number: stops})
    assert scene["trains"] == []
    assert scene["passed"] == []


def test_mapped_components_are_checked_beyond_the_clipped_scene_segment():
    gate = Crossing("branch", "Parallel branch", 12.1434, 75.05, "osm")
    train = TrainPosition("16512", 12.16, 75.1, "A", "B", False, 0, "southbound", datetime(2026, 11, 16))
    main = [(12.4, 75.1), (11.96575, 75.1)]
    branch = [(12.28, 75.05), gate.coords, (12.01, 75.05)]
    # The connecting junction is over 25km from the train, outside either
    # short scene view. The component check still finds the original ways.
    connector = [(12.4, 75.1), (12.4, 75.05), (12.28, 75.05)]
    assert _on_separate_mapped_line(train, gate, [main, branch])
    assert not _on_separate_mapped_line(train, gate, [main, branch, connector])


@pytest.mark.parametrize("mismatch", ["no_rail", "impossible_distance", "unrelated_direction", "distant_line"])
def test_bent_leg_fallback_rejects_incompatible_geometry(mismatch):
    stops, gate, rail, train = _bent_leg()
    if mismatch == "no_rail":
        rails = []
    else:
        if mismatch == "impossible_distance":
            stops = [stops[0], replace(stops[1], km=45)]
        elif mismatch == "unrelated_direction":
            rail = [(gate.lat, gate.lon - 0.05), gate.coords, (gate.lat, gate.lon + 0.05)]
        elif mismatch == "distant_line":
            gate = replace(gate, lon=74.9)
            rail = [(point[0], point[1] - 0.1378) for point in rail]
        rails = [rail]
    call = crossing_call(train, gate, rails, {}, load_settings(), stops)
    assert call.eta is None
    assert passed_ago(call) is None
