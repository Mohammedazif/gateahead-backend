import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gateahead.bot.formatters import predict_text
from gateahead.config import load_settings
from gateahead.gatecall import crossing_call, passed_ago
from gateahead.models import Crossing, SavedRoute, Station, TrainPosition
from gateahead.osm import OsmAround
from gateahead.scene import build_scene
from gateahead.service import GateAhead, PositionBatch
from gateahead.store import Store
from gateahead.timetable import parse_route_rows


STOPS = parse_route_rows([
    {"station_code": "KPQ", "distance_from_source": 603, "sta_min": 1121, "std_min": 1122, "lat": 11.984206, "lng": 75.31394},
    {"station_code": "PAZ", "distance_from_source": 610, "sta_min": 1130, "std_min": 1131, "lat": 12.021818, "lng": 75.25991},
    {"station_code": "ELM", "distance_from_source": 618.8, "sta_min": 1140, "std_min": 1140, "lat": 12.0688668, "lng": 75.2310276},
    {"station_code": "PAY", "distance_from_source": 622, "sta_min": 1143, "std_min": 1144, "lat": 12.091948, "lng": 75.194807},
])
GATE = Crossing("elimala", "Near Elimala", 12.0688668, 75.2310276, "osm", nearest_station="ELM")
RAIL = [(stop.lat, stop.lon) for stop in STOPS]
POINTS = {stop.code: (stop.lat, stop.lon) for stop in STOPS}
PAUSED = TrainPosition(
    "16650", 12.021818, 75.25991, "PAZ", "PAY", True, 5, "northbound",
    datetime(2026, 11, 16, 19, 8), age_minutes=14, covered_km=610,
    run="at", name="Parasuram Express", current_name="Pazhayangadi",
)


@pytest.mark.parametrize("age", [8, 14, 30])
@pytest.mark.parametrize("with_timetable", [True, False])
def test_stopped_upstream_report_never_turns_into_a_passed_train(age, with_timetable):
    position = replace(PAUSED, age_minutes=age)
    call = crossing_call(position, GATE, [RAIL], POINTS, load_settings(), STOPS if with_timetable else None)
    assert call.eta is None
    assert passed_ago(call) is None
    assert call.awaiting_update is True


@pytest.mark.parametrize("age", ["8", "14.5", "nan", "invalid"])
def test_provider_string_age_keeps_stopped_train_unresolved(age):
    from gateahead.live import position_from_lts

    position = position_from_lts("16650", {
        "current_station_code": "PAZ", "next_station_code": "PAY", "current_station_name": "Pazhayangadi",
        "cur_stn_lat": PAUSED.lat, "cur_stn_lng": PAUSED.lon, "status": "A",
        "distance_from_source": 610, "delay": 5, "status_as_of_min": age,
    }, PAUSED.as_of)
    call = crossing_call(position, GATE, [RAIL], POINTS, load_settings(), STOPS)
    assert call.eta is None
    assert passed_ago(call) is None
    assert call.awaiting_update is True


@pytest.mark.parametrize("age", [0, 3, 7])
def test_fresh_stopped_report_retains_the_run_time_from_the_station(age):
    call = crossing_call(replace(PAUSED, age_minutes=age), GATE, [RAIL], POINTS, load_settings(), STOPS)
    assert call.eta == pytest.approx(9)
    assert not call.awaiting_update
    assert passed_ago(call) is None


def test_new_departure_and_position_beyond_the_gate_resolve_the_wait():
    settings = load_settings()
    departed = replace(PAUSED, at_station=False, run="departed", age_minutes=0)
    approach = crossing_call(departed, GATE, [RAIL], POINTS, settings, STOPS)
    assert approach.eta == pytest.approx(9)
    assert not approach.awaiting_update
    beyond = replace(PAUSED, lat=12.091948, lon=75.194807, last_station="PAY", next_station=None,
                     covered_km=622, age_minutes=1)
    passed = crossing_call(beyond, GATE, [RAIL], POINTS, settings, STOPS)
    assert passed_ago(passed) == pytest.approx(4)
    assert not passed.awaiting_update


def test_fresh_stopped_report_at_the_gate_cannot_expire_during_a_long_halt():
    stops = [replace(stop, depart_min=1151) if stop.code == "PAZ" else
             replace(stop, arrive_min=1160, depart_min=1160) if stop.code == "ELM" else
             replace(stop, arrive_min=1163, depart_min=1164) if stop.code == "PAY" else stop for stop in STOPS]
    gate = replace(GATE, lat=PAUSED.lat, lon=PAUSED.lon, nearest_station="PAZ")
    call = crossing_call(replace(PAUSED, age_minutes=0, delay_minutes=0), gate, [RAIL], POINTS, load_settings(), stops)
    assert call.eta == 0
    assert passed_ago(call) is None


def test_scene_retains_the_unresolved_train_and_other_train_estimates():
    other_stops = parse_route_rows([
        {"station_code": "PAY", "distance_from_source": 98, "sta_min": 1151, "std_min": 1153,
         "lat": 12.091948, "lng": 75.194807},
        {"station_code": "ELM", "distance_from_source": 103.22, "sta_min": 1158, "std_min": 1158,
         "lat": 12.0688668, "lng": 75.2310276},
        {"station_code": "PAZ", "distance_from_source": 110, "sta_min": 1164, "std_min": 1165,
         "lat": 12.021818, "lng": 75.25991},
    ])
    other = replace(PAUSED, train_number="16603", name="Maveli Express", lat=12.091948, lon=75.194807,
                    last_station="PAY", next_station="PAZ", covered_km=98, age_minutes=0, delay_minutes=0)
    scene = build_scene(GATE, [RAIL], [PAUSED, other], POINTS, load_settings(), {"16650": STOPS, "16603": other_stops})
    assert [row["number"] for row in scene["awaiting_update"]] == ["16650"]
    assert scene["awaiting_update"][0]["current"] == "Pazhayangadi"
    assert [row["number"] for row in scene["trains"]] == ["16603"]
    assert scene["passed"] == []
    assert "motion" not in scene["trains"][0]


def test_prediction_and_telegram_keep_the_same_unresolved_crossing(tmp_path):
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "stopped.db")
    service.map_around = AsyncMock(return_value=OsmAround(
        [GATE], [Station(code, code, *point) for code, point in POINTS.items()], [RAIL], False,
    ))
    service._positions = AsyncMock(return_value=PositionBatch(
        ["16650"], [PAUSED], {"16650": SavedRoute(STOPS)}, {"live_checked": 1},
    ))
    result = asyncio.run(service.predict_here(*GATE.coords))
    assert result["awaiting_update"][0]["ids"] == ["elimala"]
    assert result["awaiting_update"][0]["number"] == "16650"
    assert result["passed"] == []
    assert result["gate_predictions"] == []
    assert result["sightings"][0]["passed_local"] is False
    text = predict_text(result)
    assert "Position update needed" in text
    assert "Pazhayangadi" in text
    assert "No train is close enough" not in text
    assert "Just passed" not in text
