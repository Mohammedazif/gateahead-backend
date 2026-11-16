"""When the barrier comes down, when the train is gone, and where a live report puts the train."""

from dataclasses import replace
from datetime import datetime

from gateahead.bot.formatters import gate_label, passed_label, predict_text
from gateahead.config import load_settings
from gateahead.geo import km
from gateahead.live import position_from_lts
from gateahead.models import Crossing, TrainPosition
from gateahead.predict import classify, closes_in
from gateahead.timetable import TimetableCall, merge_call, parse_route_rows, timetable_call

SETTINGS = replace(load_settings(), gate_lead_min=5, closing_soon_eta_min=10, approaching_eta_min=30)

# A straight line due south: A (km 0) -> B (km 20) -> C (km 40), about 20 km per 0.18 degrees.
_ROWS = [
    {"station_code": "AAA", "station_name": "A", "distance_from_source": "0", "sta_min": 600, "std_min": 600, "halt": 0, "stop": True, "day": 1, "lat": 12.00, "lng": 75.0},
    {"station_code": "BBB", "station_name": "B", "distance_from_source": "20", "sta_min": 620, "std_min": 622, "halt": 2, "stop": True, "day": 1, "lat": 11.82, "lng": 75.0},
    {"station_code": "CCC", "station_name": "C", "distance_from_source": "40", "sta_min": 642, "std_min": 642, "halt": 0, "stop": True, "day": 1, "lat": 11.64, "lng": 75.0},
]
GATE = Crossing("g", "Gate", 11.73, 75.0, "osm")  # km 30, timetabled 10:32


def _train(**kwargs) -> TrainPosition:
    data = dict(
        train_number="16601",
        lat=12.0,
        lon=75.0,
        last_station="AAA",
        next_station="BBB",
        at_station=False,
        delay_minutes=0,
        direction="southbound",
        as_of=datetime(2026, 11, 16, 10, 14),
        run="departed",
        covered_km=10,
        age_minutes=0,
    )
    data.update(kwargs)
    return TrainPosition(**data)


def test_report_age_is_taken_off_once():
    stops = parse_route_rows(_ROWS)
    # Reported 10 km out at 10:10 (on time), seen at 10:14: the gate is 18 minutes away.
    call = timetable_call(stops, _train(age_minutes=4), GATE)
    assert call.eta is not None and abs(call.eta - 18) < 0.6


def test_a_frozen_at_station_report_needs_an_update_instead_of_inventing_passage():
    stops = parse_route_rows(_ROWS)
    # A report at B never confirms departure, even after its predicted crossing time.
    stale = _train(
        last_station="BBB", next_station="CCC", at_station=True, run="at", covered_km=20,
        as_of=datetime(2026, 11, 16, 10, 40), age_minutes=20,
    )
    call = timetable_call(stops, stale, GATE)
    assert call.eta is None and call.placed is True
    assert call.ago is None
    assert call.awaiting_update is True


def test_a_train_that_ends_before_the_gate_never_closes_it():
    stops = parse_route_rows(_ROWS)
    beyond = Crossing("g2", "Past the end", 11.595, 75.0, "osm")  # 5 km past C
    call = timetable_call(stops, _train(), beyond)
    assert call.eta is None and call.placed is True and call.ago is None
    aside = Crossing("g3", "Another line", 11.73, 75.3, "osm")  # 30 km to one side
    assert timetable_call(stops, _train(), aside).placed is False


def test_a_distance_guess_does_not_overrule_the_timetable_for_a_standing_train():
    leaves_later = TimetableCall(150.0, None, 0.0, True)
    assert merge_call(True, 3.0, None, leaves_later, standing=True).eta == 150.0
    assert merge_call(True, 3.0, None, leaves_later, standing=False).eta == 3.0


def test_ahead_distance_is_counted_from_the_last_station():
    # The shape RailYatri returned for 16337: "3 kms ahead of WANKANER", next stop 72 km on.
    report = {
        "success": True,
        "current_station_code": "WKR",
        "current_station_name": "WANKANER JN~",
        "status": "T",
        "ahead_distance": 3,
        "distance_from_source": 297,
        "status_as_of_min": 0,
        "cur_stn_lat": "22.6100",
        "cur_stn_lng": "70.9400",
        "travelling_to_lat_lng": ["22.7300", "71.6400"],
        "upcoming_stations": [
            {"station_code": "", "non_stops": []},
            {"station_code": "SUNR", "distance_from_source": 369, "a_min": 770},
        ],
    }
    position = position_from_lts("16337", report, datetime(2026, 11, 16, 11, 45))
    assert 2.0 < km((22.61, 70.94), position.coords) < 4.5
    arrived = dict(report, ahead_distance=75, distance_from_source=369)
    near_next = position_from_lts("16337", arrived, datetime(2026, 11, 16, 11, 45))
    assert km(near_next.coords, (22.73, 71.64)) < km((22.61, 70.94), (22.73, 71.64)) * 0.1


def test_the_barrier_is_down_for_the_lead_time_and_a_little_after():
    assert classify(30.5, SETTINGS) == "open"
    assert classify(20, SETTINGS) == "approaching"
    assert classify(7, SETTINGS) == "closing_soon"
    assert classify(4.5, SETTINGS) == "closed"
    assert classify(-1.5, SETTINGS) == "closed"
    assert classify(-2.5, SETTINGS) == "passed"
    assert closes_in(12, SETTINGS) == 7
    assert closes_in(3, SETTINGS) == -2


def test_gate_copy_counts_down_to_the_barrier():
    assert gate_label(12, 7) == "closes in 7 min"
    assert gate_label(5.6, 0.6) == "closing now"
    assert gate_label(4, -1) == "closed, train in 4 min"
    assert gate_label(0.5, -4.5) == "closed, train passing"
    assert gate_label(95, 90) == "closes in 1 hr 30 min"
    assert gate_label(8) == "closes in 8 min"
    assert passed_label(0.4) == "passed just now"
    assert passed_label(2.6) == "passed 3 min ago"


def test_a_just_passed_train_is_named_once():
    text = predict_text(
        {
            "approaching": [],
            "passed": [{"train": "16601", "name": "Coastal Express", "cluster": "School Road", "ago": 1.2}],
            "sightings": [{"number": "16601", "name": "Coastal Express", "current": "ELM", "age": 1, "passed_local": False}],
        }
    )
    assert "Just passed" in text
    assert "16601 Coastal Express passed 1 min ago at School Road" in text
    assert "On this line" not in text
