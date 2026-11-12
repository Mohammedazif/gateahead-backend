import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from gateahead.config import load_settings
from gateahead.live import classify_live
from gateahead.models import Crossing, Halt, SavedRoute, TrainPosition
from gateahead.service import positions_from_looks, resolve_position
from gateahead.store import Store
from gateahead.timetable import (
    Call,
    list_status,
    merge_call,
    classify_passage,
    parse_route_rows,
    parse_schedule,
    parse_timetable_html,
    runs_on,
    scheduled_position,
    timetable_call,
)


def _rows() -> list[dict]:
    return [
        {"station_code": "MAQ", "station_name": "MANGALORE CNTL", "distance_from_source": "0", "sta_min": 0, "std_min": 300, "halt": 0, "stop": True, "day": 1, "lat": 12.87, "lng": 74.88},
        {"station_code": "CHV", "station_name": "CHARVATTUR", "distance_from_source": "84", "sta_min": 377, "std_min": 378, "halt": 1, "stop": True, "day": 1, "lat": 12.21, "lng": 75.15},
        {"station_code": "PAY", "station_name": "PAYYANUR", "distance_from_source": "98", "sta_min": 390, "std_min": 391, "halt": 1, "stop": True, "day": 1, "lat": 12.09, "lng": 75.20},
    ]


def _train(**kwargs) -> TrainPosition:
    data = dict(
        train_number="16649",
        lat=12.87,
        lon=74.88,
        last_station="MAQ",
        next_station="CLT",
        at_station=False,
        delay_minutes=20,
        direction="southbound",
        as_of=datetime(2026, 11, 12, 6, 0),
        run="departed",
        covered_km=0,
        age_minutes=0,
    )
    data.update(kwargs)
    return TrainPosition(**data)


def test_route_keeps_pass_throughs_and_rolls_the_day():
    rows = _rows() + [
        {"station_code": "PPNS", "station_name": "PAPPINISSERI", "distance_from_source": "124", "sta_min": 415, "std_min": 415, "halt": 0, "stop": False, "day": 1},
        {"station_code": "CLT", "station_name": "KOZHIKODE", "distance_from_source": "221", "sta_min": 60, "std_min": 63, "halt": 3, "stop": True, "day": 2},
    ]
    stops = parse_route_rows(rows)
    assert [stop.code for stop in stops] == ["MAQ", "CHV", "PAY", "PPNS", "CLT"]
    assert stops[3].stops is False
    assert stops[4].arrive_min == 1440 + 60


def test_lateness_is_measured_from_the_clock_not_added_twice():
    stops = parse_route_rows(_rows())
    gate = Crossing("g", "Gate", 12.15, 75.175, "osm")
    on_time = timetable_call(
        stops,
        _train(delay_minutes=0, as_of=datetime(2026, 11, 12, 5, 0)),
        gate,
    )
    just_left_late = timetable_call(
        stops,
        _train(delay_minutes=20, as_of=datetime(2026, 11, 12, 5, 20)),
        gate,
    )
    still_waiting = timetable_call(
        stops,
        _train(delay_minutes=20, as_of=datetime(2026, 11, 12, 5, 0), run="at", at_station=True),
        gate,
    )
    early = timetable_call(
        stops,
        _train(delay_minutes=-10, as_of=datetime(2026, 11, 12, 5, 0)),
        gate,
    )
    stale = timetable_call(
        stops,
        _train(delay_minutes=0, as_of=datetime(2026, 11, 12, 5, 0), age_minutes=6),
        gate,
    )
    standing = timetable_call(
        stops,
        _train(delay_minutes=0, as_of=datetime(2026, 11, 12, 5, 0), run="at", at_station=True, age_minutes=6),
        gate,
    )
    assert on_time.placed and on_time.bracket == "between CHV and PAY"
    assert on_time.eta is not None and just_left_late.eta is not None
    assert abs(just_left_late.eta - on_time.eta) < 1
    assert still_waiting.eta is not None and abs(still_waiting.eta - (on_time.eta + 20)) < 1
    assert early.eta is not None and abs(early.eta - (on_time.eta - 10)) < 1
    assert stale.eta is not None and abs(stale.eta - (on_time.eta - 6)) < 1
    assert standing.eta is not None and abs(standing.eta - on_time.eta) < 1
    overdue = timetable_call(
        stops,
        _train(delay_minutes=0, as_of=datetime(2026, 11, 12, 7, 0)),
        gate,
    )
    assert overdue.eta is not None and abs(overdue.eta - on_time.eta) < 1
    assert list_status(on_time.eta, on_time.slack, load_settings(), True) == "later"


def test_a_run_that_crossed_midnight_uses_yesterdays_clock():
    rows = _rows() + [
        {
            "station_code": "NIGHT",
            "station_name": "NIGHT",
            "distance_from_source": "400",
            "sta_min": 1400,
            "std_min": 1402,
            "halt": 2,
            "stop": True,
            "day": 1,
            "lat": 13.0,
            "lng": 76.0,
        },
        {
            "station_code": "DAWN",
            "station_name": "DAWN",
            "distance_from_source": "430",
            "sta_min": 60,
            "std_min": 62,
            "halt": 2,
            "stop": True,
            "day": 2,
            "lat": 13.2,
            "lng": 76.0,
        },
    ]
    stops = parse_route_rows(rows)
    assert stops[-1].arrive_min == 1440 + 60
    call = timetable_call(
        stops,
        _train(
            covered_km=400,
            last_station="NIGHT",
            delay_minutes=10,
            run="departed",
            as_of=datetime(2026, 11, 13, 0, 30),
        ),
        Crossing("g", "Gate", 13.18, 76.0, "osm"),
    )
    assert call.placed is True
    assert call.bracket == "between NIGHT and DAWN"
    assert call.eta is not None
    assert 25 < call.eta < 40


def test_before_departure_the_wait_is_part_of_the_gate_time():
    stops = parse_route_rows(_rows())
    gate = Crossing("g", "Gate", 12.15, 75.175, "osm")
    waiting = timetable_call(
        stops,
        _train(delay_minutes=0, run="at", at_station=True, covered_km=0, as_of=datetime(2026, 11, 12, 4, 0)),
        gate,
    )
    at_departure = timetable_call(
        stops,
        _train(delay_minutes=0, run="departed", covered_km=0, as_of=datetime(2026, 11, 12, 5, 0)),
        gate,
    )
    assert waiting.eta is not None and at_departure.eta is not None
    assert abs(waiting.eta - (at_departure.eta + 60)) < 1
    assert list_status(waiting.eta, waiting.slack, load_settings(), True) == "later"
    too_early = timetable_call(
        stops,
        _train(delay_minutes=0, run="at", at_station=True, covered_km=0, as_of=datetime(2026, 11, 11, 20, 0)),
        gate,
    )
    assert too_early.eta is not None
    assert list_status(too_early.eta, too_early.slack, load_settings(), True) is None


def test_a_gate_off_the_route_is_not_timed():
    stops = parse_route_rows(_rows())
    call = timetable_call(stops, _train(), Crossing("g", "Inland", 12.15, 75.40, "osm"))
    assert call.placed is False
    assert call.eta is None


def test_a_late_clock_does_not_close_a_gate_the_train_has_not_reached():
    stops = parse_route_rows(_rows())
    gate = Crossing("g", "Gate", 12.15, 75.175, "osm")
    on_time = timetable_call(
        stops,
        _train(delay_minutes=0, as_of=datetime(2026, 11, 12, 5, 0)),
        gate,
    )
    assert on_time.eta is not None and on_time.eta > 30
    call = timetable_call(
        stops,
        _train(
            delay_minutes=0,
            as_of=datetime(2026, 11, 12, 5, 0) + timedelta(minutes=on_time.eta + 0.5),
        ),
        gate,
    )
    assert call.eta is not None
    assert call.eta > 30


def test_a_gate_behind_the_train_is_not_given_a_time():
    stops = parse_route_rows(_rows())
    call = timetable_call(stops, _train(covered_km=97, delay_minutes=0), Crossing("g", "Gate", 12.15, 75.175, "osm"))
    assert call.placed is True
    assert call.eta is None


def test_a_near_rail_fix_wins_when_the_timetable_km_is_stale():
    local = Call(10, None, "rail", 0)
    far = timetable_call(
        parse_route_rows(_rows()),
        _train(covered_km=0, delay_minutes=0, as_of=datetime(2026, 11, 12, 5, 0)),
        Crossing("g", "Gate", 12.15, 75.175, "osm"),
    )
    chosen = merge_call(True, local.eta, None, far)
    assert chosen.source == "rail"
    assert chosen.eta == 10


def test_halt_slack_keeps_a_short_hop_from_saying_closed():
    settings = load_settings()
    assert list_status(8, 5, settings, True) == "approaching"
    assert list_status(8, 1, settings, True) == "closing_soon"
    assert list_status(500, 0, settings, True) is None


def test_html_route_is_read_from_the_schedule_block():
    html = """
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"trainTimeTable":{"trainScheduleDetail":[{"route":%s}]}}}}</script>
    """ % __import__("json").dumps(_rows())
    stops = parse_timetable_html(html)
    assert stops[0].code == "MAQ"
    assert stops[2].km == 98


def test_route_roundtrip(tmp_path: Path):
    store = Store(tmp_path / "rail.db")
    stops = parse_route_rows(_rows())
    store.save_route("16649", stops, "SAT")
    loaded = store.load_route("16649", 24)
    assert loaded is not None
    assert loaded.run_days == "SAT"
    assert [stop.code for stop in loaded.stops] == ["MAQ", "CHV", "PAY"]
    assert store.halt("PAY").name == "PAYYANUR"
    assert store.halt("PAY").source == "timetable"
    store.upsert_halts([Halt("CHV", "Charvattur", 12.2, 75.2, "halt", "SR", "datameet")])
    store.save_route("16649", stops, "")
    assert store.load_route("16649", 24).run_days == "SAT"
    assert store.halt("CHV").source == "datameet"
    assert store.halt("CHV").zone == "SR"


def test_an_old_route_table_learns_run_days(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE train_routes (train_number TEXT PRIMARY KEY, fetched_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    store = Store(path)
    store.save_route("16649", parse_route_rows(_rows()), "MON,TUE,WED,THU,FRI")
    loaded = store.load_route("16649", 24)
    assert loaded is not None
    assert loaded.run_days == "MON,TUE,WED,THU,FRI"


def _evening_route(run_days: str = "MON,TUE,WED,THU,FRI,SAT,SUN") -> SavedRoute:
    rows = _rows()
    rows[0]["std_min"] = 18 * 60 + 15
    shift = 18 * 60 + 15 - 300
    for row in rows[1:]:
        row["sta_min"] += shift
        row["std_min"] += shift
    return SavedRoute(parse_route_rows(rows), run_days)


_GATE = Crossing("g", "Gate", 12.15, 75.175, "osm")
_THU = datetime(2026, 11, 12, 17, 45)
_EXPIRED = {"success": False, "new_message": "This train has already expired"}


def test_expired_before_departure_still_gets_a_gate_time():
    look = classify_live(_EXPIRED, None)
    route = _evening_route()
    position = resolve_position("16229", look, route, _THU)
    assert position is not None
    assert position.run == "at"
    assert position.delay_minutes == 0
    assert position.scheduled_at == "18:15"
    call = timetable_call(route.stops, position, _GATE)
    assert call.placed is True
    assert call.eta is not None and call.eta < 8 * 60
    assert list_status(call.eta, call.slack, load_settings(), True) == "later"


def test_expired_long_after_departure_stays_hidden():
    morning = SavedRoute(parse_route_rows(_rows()), "")
    assert resolve_position("16314", classify_live(_EXPIRED, None), morning, _THU) is None


def test_a_saturday_train_waits_until_saturday():
    route = _evening_route("SAT")
    assert runs_on("SAT", _THU) is False
    assert runs_on("SAT", datetime(2026, 11, 14)) is True
    assert runs_on("", _THU) is True
    assert resolve_position("11097", classify_live(_EXPIRED, None), route, _THU) is None
    kept = resolve_position("11097", classify_live(_EXPIRED, None), route, datetime(2026, 11, 14, 17, 45))
    assert kept is not None and kept.run == "at"
    assert resolve_position("16314", classify_live(_EXPIRED, None), _evening_route(""), _THU) is not None


def test_a_live_train_is_kept_on_a_day_it_does_not_start():
    moving = {
        "success": True,
        "at_dstn": False,
        "current_station_code": "MAQ",
        "current_station_name": "MANGALORE CNTL",
        "status": "T",
        "delay": 5,
        "distance_from_source": 40,
        "cur_stn_lat": 12.5,
        "cur_stn_lng": 75.0,
    }
    daily = _evening_route()
    looks = {
        "11097": classify_live(moving, None),
        "16229": classify_live(_EXPIRED, None),
        "16649": classify_live(
            {"success": True, "at_dstn": True, "current_station_code": "CAPE"}, None
        ),
    }
    routes = {"11097": _evening_route("SAT"), "16229": daily, "16649": daily}
    found = positions_from_looks(["16649", "11097", "16229"], looks, routes, _THU)
    assert [item.train_number for item in found] == ["11097", "16229"]
    assert found[0].last_station == "MAQ"
    assert found[0].covered_km == 40


def test_just_after_departure_without_a_report_stays_at_the_origin():
    position = resolve_position(
        "16229",
        classify_live(_EXPIRED, None),
        _evening_route(),
        datetime(2026, 11, 12, 18, 25),
    )
    assert position is not None
    assert position.run == "at"
    assert position.delay_minutes == 10
    assert position.covered_km == 0


def test_a_gate_on_a_bend_uses_the_rail_between_the_halts():
    stops = parse_route_rows(
        [
            {
                "station_code": "WEST",
                "station_name": "WEST",
                "distance_from_source": "0",
                "sta_min": 0,
                "std_min": 600,
                "halt": 0,
                "stop": True,
                "day": 1,
                "lat": 12.0,
                "lng": 75.0,
            },
            {
                "station_code": "EAST",
                "station_name": "EAST",
                "distance_from_source": "22",
                "sta_min": 620,
                "std_min": 622,
                "halt": 2,
                "stop": True,
                "day": 1,
                "lat": 12.0,
                "lng": 75.2,
            },
        ]
    )
    rail = [(12.0, 75.0), (12.045, 75.1), (12.0, 75.2)]
    train = _train(
        delay_minutes=0,
        run="departed",
        covered_km=0,
        last_station="WEST",
        lat=12.0,
        lon=75.0,
        as_of=datetime(2026, 11, 12, 10, 0),
    )
    gate = Crossing("g", "Bend", 12.045, 75.1, "osm")
    missed = timetable_call(stops, train, gate)
    placed = timetable_call(stops, train, gate, rail)
    assert missed.placed is False
    assert placed.placed is True
    assert placed.bracket == "between WEST and EAST"
    assert placed.eta is not None and 8 < placed.eta < 12
    inland = timetable_call(stops, train, Crossing("g", "Inland", 12.20, 75.10, "osm"), rail)
    assert inland.placed is False
    assert inland.eta is None


def test_run_days_come_from_the_schedule_block():
    import json

    html = """
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"trainTimeTable":{"trainScheduleDetail":[{"run_days":"SAT","route":%s}]}}}}</script>
    """ % json.dumps(_rows())
    stops, days = parse_schedule(html)
    assert days == "SAT"
    assert stops[0].code == "MAQ"
    assert runs_on(days, datetime(2026, 11, 12)) is False
    assert runs_on(days, datetime(2026, 11, 14)) is True


def test_only_a_train_near_this_stretch_is_fetched_live():
    stops = parse_route_rows(_rows())
    pay = (12.09, 75.20)
    assert classify_passage(stops, "", *pay, datetime(2026, 11, 12, 5, 40)) == "hot"
    assert classify_passage(stops, "", *pay, datetime(2026, 11, 12, 2, 0)) == "cold"
    assert classify_passage(stops, "", 20.0, 80.0, datetime(2026, 11, 12, 5, 40)) == "skip"
    assert classify_passage(stops, "SAT", *pay, datetime(2026, 11, 12, 5, 40)) == "skip"
    assert classify_passage(stops, "SAT", *pay, datetime(2026, 11, 13, 23, 0)) == "cold"


def test_a_distant_train_keeps_an_on_time_estimate_without_a_live_report():
    route = SavedRoute(parse_route_rows(_rows()), "")
    now = datetime(2026, 11, 12, 2, 0)
    position = scheduled_position("16649", route, 12.09, 75.20, now)
    assert position is not None
    assert position.delay_minutes == 0
    assert position.covered_km == 0
    call = timetable_call(route.stops, position, Crossing("g", "Payyanur", 12.09, 75.20, "osm"))
    assert call.eta is not None and 250 < call.eta < 290
