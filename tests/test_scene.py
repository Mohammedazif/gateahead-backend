from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from gateahead.config import load_settings
from gateahead.geo import along_route, km, stitch_rail
from gateahead.models import Crossing, TrainPosition
from gateahead.scene import build_scene

A = (12.00, 75.20)
B = (12.01, 75.20)
C = (12.02, 75.20)


def test_stitch_joins_a_reversed_fragment():
    rail = stitch_rail([[A, B], [C, B]], B, half_km=3)
    assert rail is not None
    assert km(rail[0], A) < 0.2
    assert km(rail[-1], C) < 0.2


def test_stitch_leaves_a_gap_broken():
    far = (12.01, 75.22)
    rail = stitch_rail([[A, B], [far, (12.02, 75.22)]], B, half_km=3)
    assert rail is not None
    assert km(rail[-1], B) < 0.3


def test_scene_places_a_train_on_the_drawn_rail():
    now = datetime(2026, 9, 21, 17, 0)
    crossing = Crossing("g", "Gate", B[0], B[1], "osm")
    train = TrainPosition(
        train_number="16348",
        lat=A[0],
        lon=A[1],
        last_station="CAN",
        next_station="TLY",
        at_station=True,
        delay_minutes=0,
        direction="northbound",
        as_of=now,
        upcoming_codes=("TLY",),
        station_etas=(("TLY", now + timedelta(minutes=20)),),
        name="Express",
        age_minutes=1,
    )
    stations = {"CAN": A, "TLY": C}
    body = build_scene(crossing, [[A, B], [B, C]], [train], stations, load_settings())
    assert body["available"] is True
    assert len(body["rail"]) >= 2
    assert body["trains"][0]["number"] == "16348"
    assert body["trains"][0]["eta_min"] > 0
    assert body["trains"][0]["position_on_segment"] is True
    assert "railyatri" not in str(body).lower()


def test_scene_without_a_rail_stays_closed():
    crossing = Crossing("g", "Gate", 11.0, 76.0, "osm")
    body = build_scene(crossing, [[A, B]], [], {}, load_settings())
    assert body["available"] is False
    assert body["trains"] == []


def test_far_train_keeps_its_eta_but_is_not_drawn_at_the_local_rail_endpoint(monkeypatch):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda *args: Call(23.0, None, "timetable", 0))
    train = TrainPosition(
        train_number="16323", name="Next train", lat=11.80, lon=75.20,
        last_station="AAA", next_station="BBB", at_station=False, delay_minutes=0,
        direction="northbound", as_of=datetime(2026, 11, 16, 13, 22), age_minutes=1,
    )
    body = build_scene(Crossing("g", "Gate", *B, "osm"), [[A, B], [B, C]], [train], {}, load_settings())
    assert body["trains"][0]["eta_min"] == 23.0
    assert body["trains"][0]["position_on_segment"] is False


@pytest.mark.parametrize("reversed_rail,expected", [(False, 1), (True, -1)])
def test_motion_uses_travel_direction_on_the_selected_rail(monkeypatch, reversed_rail, expected):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda *args: Call(1, None, "rail", 0))
    now = datetime(2026, 11, 16, 14, 16)
    train = TrainPosition("16606", *A, "AAA", "BBB", False, 0, "unknown", now, age_minutes=1)
    line = [C, B, A] if reversed_rail else [A, B, C]
    row = build_scene(Crossing("g", "Gate", *B, "osm"), [line], [train], {"AAA": A, "BBB": C}, load_settings())["trains"][0]
    assert row["motion"]["direction"] == expected
    assert row["motion"]["valid_for_sec"] == 420
    assert row["snapshot_at"] == now.timestamp()


@pytest.mark.parametrize("kind", ["stopped", "old", "unknown", "uncertain"])
def test_motion_is_not_invented_for_stopped_old_or_directionless_trains(monkeypatch, kind):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda *args: Call(1, None, "timetable", 5 if kind == "uncertain" else 0))
    train = TrainPosition("16606", *A, "AAA", "BBB", False, 0, "unknown", datetime.now())
    if kind == "stopped":
        train = replace(train, at_station=True)
    elif kind == "old":
        train = replace(train, age_minutes=8)
    stations = {} if kind == "unknown" else {"AAA": A, "BBB": C}
    row = build_scene(Crossing("g", "Gate", *B, "osm"), [[A, B, C]], [train], stations, load_settings())["trains"][0]
    assert "motion" not in row


def test_later_estimates_remain_available_in_gate_details(monkeypatch):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda *args: Call(70, None, "timetable", 0))
    train = TrainPosition("16606", *A, "AAA", "BBB", False, 0, "northbound", datetime.now())
    row = build_scene(Crossing("g", "Gate", *B, "osm"), [[A, B, C]], [train], {}, load_settings())["trains"][0]
    assert row["eta_min"] == 70
    assert row["status"] == "later"


def test_closed_gate_view_includes_the_train_during_the_barrier_lead_time(monkeypatch):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda *args: Call(4, None, "rail", 0))
    crossing = Crossing("g", "Gate", 12, 75.20, "osm")
    train = TrainPosition("16606", 11.964, 75.20, "AAA", "BBB", False, 0, "northbound", datetime.now(), age_minutes=1)
    rails = [[(11.9, 75.20), (12, 75.20), (12.1, 75.20)]]
    settings = replace(load_settings(), default_speed_kmh=60, gate_lead_min=5)
    body = build_scene(crossing, rails, [train], {"AAA": rails[0][0], "BBB": rails[0][-1]}, settings)
    row = body["trains"][0]
    assert row["status"] == "closed"
    assert row["position_on_segment"] is True
    gate_km, _ = along_route(body["rail"], crossing.coords)
    assert gate_km > 4
    assert row["motion"]["valid_for_sec"] > row["eta_min"] * 60


def _parallel_tracks():
    # A nearby way appears first in the map response; it must not win over
    # the continuation whose endpoint actually meets this track.
    return [
        [(12.00, 75), (12.01, 75)],
        [(12.01, 75.00015), (12.02, 75.00015)],
        [(12.01, 75), (12.02, 75)],
        [(12.00, 75.00015), (12.01, 75.00015)],
    ]


@pytest.mark.parametrize("variant", ["original", "reordered", "reversed_ways"])
def test_parallel_tracks_stay_separate_without_returning_along_the_neighbour(variant):
    rails = _parallel_tracks()
    if variant == "reordered":
        rails.reverse()
    elif variant == "reversed_ways":
        rails = [list(reversed(line)) if index % 2 == 0 else line for index, line in enumerate(rails)]
    for lon in (75, 75.00015):
        for lat in (12.005, 12.015):
            chain = stitch_rail(rails, (lat, lon), half_km=15)
            assert chain is not None
            assert all(abs(point[1] - lon) < 1e-8 for point in chain)
            steps = [after[0] - before[0] for before, after in zip(chain, chain[1:])]
            assert all(step > 0 for step in steps) or all(step < 0 for step in steps)


def test_a_straight_gap_can_be_joined_but_a_lateral_jump_to_a_parallel_track_cannot():
    straight = stitch_rail([[(12, 75), (12.01, 75)], [(12.0101, 75), (12.02, 75)]], (12.005, 75), half_km=15)
    assert straight[-1] == (12.02, 75)
    separate = stitch_rail([[(12, 75), (12.01, 75)], [(12.01, 75.00015), (12.02, 75.00015)]], (12.005, 75), half_km=15)
    assert separate[-1] == (12.01, 75)


@pytest.mark.parametrize("reverse_ways", [False, True])
def test_one_service_keeps_its_geographic_direction_between_neighbouring_gates(monkeypatch, reverse_ways):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda *args: Call(0.5, None, "timetable", 0))
    train = TrainPosition("16512", 12, 75, "AAA", "CCC", False, 0, "northbound", datetime.now())
    rails = _parallel_tracks()
    if reverse_ways:
        rails = [list(reversed(line)) if index % 2 == 0 else line for index, line in enumerate(rails)]
    for lat in (12.005, 12.015):
        body = build_scene(Crossing(str(lat), "Gate", lat, 75, "osm"), rails, [train], {"AAA": (12, 75), "CCC": (12.02, 75)}, load_settings())
        assert [row["number"] for row in body["trains"]] == ["16512"]
        row = body["trains"][0]
        assert row["motion"]["direction"] * (body["rail"][-1][0] - body["rail"][0][0]) > 0


def test_two_opposing_services_retain_distinct_identities_and_directions(monkeypatch):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda *args: Call(0.5, None, "timetable", 0))
    north = TrainPosition("16512", 12, 75, "AAA", "CCC", False, 0, "northbound", datetime.now())
    south = TrainPosition("16511", 12.02, 75.00015, "CCC", "AAA", False, 0, "southbound", north.as_of)
    body = build_scene(Crossing("g", "Gate", 12.015, 75, "osm"), _parallel_tracks(), [north, south], {"AAA": (12, 75), "CCC": (12.02, 75)}, load_settings())
    assert {row["number"] for row in body["trains"]} == {"16512", "16511"}
    for row in body["trains"]:
        world = row["motion"]["direction"] * (body["rail"][-1][0] - body["rail"][0][0])
        assert (world > 0) == (row["number"] == "16512")
        track = row["track"]
        assert track["estimated"] is True
        expected_lon = 75 if row["number"] == "16512" else 75.00015
        assert all(abs(point[1] - expected_lon) < 1e-8 for point in track["rail"])
        assert (track["motion"]["direction"] * (track["rail"][-1][0] - track["rail"][0][0]) > 0) == (row["number"] == "16512")


def test_scene_includes_the_second_approaching_train_even_when_the_first_is_already_near(monkeypatch):
    from gateahead.timetable import Call

    monkeypatch.setattr("gateahead.scene.crossing_call", lambda position, *args: Call(0.5 if position.train_number == "16512" else 4, None, "timetable", 0))
    north = TrainPosition("16512", 11.995, 75, "AAA", "CCC", False, 0, "northbound", datetime.now())
    south = TrainPosition("16511", 12.036, 75, "CCC", "AAA", False, 0, "southbound", north.as_of)
    rails = [[(11.9, 75), (12, 75), (12.1, 75)]]
    body = build_scene(Crossing("g", "Gate", 12, 75, "osm"), rails, [north, south], {"AAA": (11.9, 75), "CCC": (12.1, 75)}, load_settings())
    assert len(body["trains"]) == 2
    assert all(row["position_on_segment"] for row in body["trains"])
    assert along_route(body["rail"], (12, 75))[0] > 4
