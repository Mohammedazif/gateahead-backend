import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from gateahead import live
from gateahead.clock import INDIA
from gateahead.config import load_settings
from gateahead.database import consolidate
from gateahead.gatecall import crossing_call, passed_ago
from gateahead.models import Crossing, SavedRoute
from gateahead.osm import OsmAround
from gateahead.runs import requests_for
from gateahead.scene import build_scene
from gateahead.service import GateAhead, PositionBatch
from gateahead.store import Store
from gateahead.timetable import fetch_timetable, parse_route_rows, route_for_start, scheduled_position


NOW = datetime(2026, 11, 16, 10, 8, tzinfo=INDIA)
RAIL = [(12, 75.2), (12.15, 75.2), (12.3, 75.2)]
GATE = Crossing("sample", "Sample crossing", *RAIL[1], "osm")
STATIONS = {"STH": RAIL[0], "NTH": RAIL[-1]}
ROWS = [
    {"station_code": "STH", "distance_from_source": 0, "std_min": 600, "lat": 12, "lng": 75.2},
    {"station_code": "NTH", "distance_from_source": 30, "sta_min": 630, "lat": 12.3, "lng": 75.2},
]
ROUTE = parse_route_rows(ROWS)


def _report(**changes) -> dict:
    return {
        "train_number": "16512", "train_start_date": NOW.date().isoformat(), "success": True,
        "current_station_code": "STH", "next_station_code": "NTH", "status": "T",
        "cur_stn_lat": 12.08, "cur_stn_lng": 75.2, "distance_from_source": 8,
        "status_as_of_min": 0, **changes,
    }


@pytest.mark.parametrize("offset", [0.0001, 0.012])
@pytest.mark.parametrize("with_timetable", [False, True])
def test_a_gate_does_not_accept_a_report_from_a_disconnected_parallel_rail(offset, with_timetable):
    side = [(lat, lon + offset) for lat, lon in RAIL]
    train = live.position_from_lts("16512", _report(cur_stn_lng=side[0][1]), NOW)
    settings = load_settings()
    normal = crossing_call(replace(train, lon=75.2), GATE, [RAIL, side], STATIONS, settings, ROUTE)
    assert normal.eta == pytest.approx(7)
    timetable = parse_route_rows([{**row, "lng": side[0][1]} for row in ROWS]) if with_timetable else None
    other = crossing_call(train, GATE, [RAIL, side], STATIONS, settings, timetable)
    assert other.eta is None and other.ago is None and not other.awaiting_update


@pytest.mark.parametrize("age", [8, 9, 10, 30])
def test_a_frozen_moving_report_is_unresolved_with_no_passage(age):
    train = live.position_from_lts("16512", _report(status_as_of_min=age), NOW)
    scene = build_scene(GATE, [RAIL], [train], STATIONS, load_settings(), {"16512": ROUTE})
    assert scene["trains"] == [] and scene["passed"] == []
    assert scene["awaiting_update"][0]["reason"] == "stale_report"
    assert scene["awaiting_update"][0]["age_min"] == age

    fresh = replace(train, age_minutes=1)
    updated = build_scene(GATE, [RAIL], [fresh], STATIONS, load_settings(), {"16512": ROUTE})
    assert updated["awaiting_update"] == []
    assert updated["trains"][0]["eta_min"] == pytest.approx(6)


@pytest.mark.parametrize("age", [None, "", "nan", -1, True])
def test_unavailable_provider_age_neither_animates_nor_predicts(age):
    train = live.position_from_lts("16512", _report(status_as_of_min=age), NOW)
    scene = build_scene(GATE, [RAIL], [train], STATIONS, load_settings(), {"16512": ROUTE})
    assert scene["trains"] == [] and scene["passed"] == []
    assert scene["awaiting_update"][0]["reason"] == "unknown_report_age"


def test_absolute_observation_time_must_not_be_in_the_future():
    earlier = live.position_from_lts("16512", _report(status_as_of_min=None, status_as_of=(NOW - timedelta(minutes=2)).isoformat()), NOW)
    later = live.position_from_lts("16512", _report(status_as_of_min=None, status_as_of=(NOW + timedelta(seconds=1)).isoformat()), NOW)
    assert earlier.report_age_known and earlier.age_minutes == 2
    assert not later.report_age_known


def test_elapsed_forecast_from_a_still_fresh_upstream_position_does_not_confirm_passage():
    train = live.position_from_lts("16512", _report(cur_stn_lat=12.14, distance_from_source=14, status_as_of_min=5), NOW)
    scene = build_scene(GATE, [RAIL], [train], STATIONS, load_settings(), {"16512": ROUTE})
    assert scene["trains"] == [] and scene["passed"] == []
    assert scene["awaiting_update"][0]["reason"] == "estimate_elapsed"


@pytest.mark.parametrize("ago", [1, 2, 2.5])
def test_observed_passage_keeps_its_clearance_window(ago):
    train = live.position_from_lts("16512", _report(cur_stn_lat=12.15 + ago / 100, distance_from_source=15 + ago), NOW)
    call = crossing_call(train, GATE, [RAIL], STATIONS, load_settings(), ROUTE)
    if ago <= 2:
        assert call.eta == pytest.approx(-ago)
        assert passed_ago(call) is None
    else:
        assert call.eta is None
        assert passed_ago(call) == pytest.approx(ago)


def test_a_schedule_can_estimate_approach_without_an_observed_position():
    train = scheduled_position("16512", SavedRoute(ROUTE), *GATE.coords, NOW.replace(minute=13), NOW.date())
    scene = build_scene(GATE, [RAIL], [train], STATIONS, load_settings(), {"16512": ROUTE})
    row = scene["trains"][0]
    assert row["status"] == "approaching"
    assert row["observed"] is False and row["position_on_segment"] is False
    assert "motion" not in row
    after_passage = scheduled_position("16512", SavedRoute(ROUTE), *GATE.coords, NOW.replace(minute=19), NOW.date())
    assert build_scene(GATE, [RAIL], [after_passage], STATIONS, load_settings(), {"16512": ROUTE})["passed"] == []


@pytest.mark.parametrize("primary", [
    _report(train_number="16513"),
    _report(train_number="16513", success=False, message="Cancelled"),
    _report(train_start_date="2026-11-15"),
])
def test_mismatched_primary_report_tries_matching_fallback_before_classification(monkeypatch, primary):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(live, "datetime", Clock)
    monkeypatch.setattr(live, "_fetch_lts_api", AsyncMock(return_value=primary))
    page = AsyncMock(return_value=_report())
    monkeypatch.setattr(live, "_fetch_lts_page", page)
    accepted = asyncio.run(live.fetch_run(None, "16512", NOW.date()))
    page.assert_awaited_once_with(None, "16512", NOW.date())
    assert accepted.moving == _report()
    assert accepted.operating_status == "running"
    page.return_value = primary
    unmatched = asyncio.run(live.fetch_run(None, "16512", NOW.date()))
    assert unmatched.moving is None and unmatched.operating_status == "unknown"


def test_html_report_keeps_feedback_train_and_date():
    report = live._lts_from_html('<a href="/live-train-status-user-feedback?train_no=16512&amp;start_date=2026-11-16%2010:05&amp;current_station=STH">', "16512")
    assert live.report_start(report) == "2026-11-16"
    assert report["train_no"] == "16512"
    assert live._lts_from_html('<a href="/live-train-status-user-feedback?train_no=16513&amp;current_station=STH">', "16512") is None


def test_provider_upcoming_clock_uses_the_report_day_across_cached_midnight():
    fetched = NOW.replace(hour=23, minute=50)
    rows = [{"station_code": "VLR", "a_day": 1, "a_min": 9}]
    report = _report(upcoming_stations=rows)
    fresh = live.position_from_lts("16512", report, fetched)
    cached = live.position_from_lts("16512", report, fetched + timedelta(minutes=11), fetched_at=fetched.timestamp())
    eta = fetched + timedelta(minutes=19)
    assert dict(fresh.station_etas)["VLR"] == dict(cached.station_etas)["VLR"] == eta
    assert cached.age_minutes == 11


def test_weekday_route_variants_survive_catalog_storage_and_backup(tmp_path):
    monday = [ROWS[0], {"station_code": "MID", "distance_from_source": 15, "sta_min": 615, "lat": 12.15, "lng": 75.2}, ROWS[1]]
    tuesday = [{**row, "lng": 75.9} for row in ROWS]
    table = {"timeTableDaysGroup": [{"run_days": "MON", "items": monday}, {"run_days": "TUE", "items": tuesday}]}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self):
            payload = {"props": {"pageProps": {"trainTimeTable": table}}}
            return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    schedule = asyncio.run(fetch_timetable(Session(), "16512"))
    source = Store(tmp_path / "source.db")
    source.save_route("16512", schedule.stops, schedule.run_days, variants=schedule.variants)
    target = tmp_path / "target.db"
    consolidate(target, source.path)
    migrated = Store(target)
    try:
        loaded = migrated.load_route("16512", None)
        assert loaded == schedule
        first_day = date(2026, 11, 16)
        second_day = date(2026, 11, 17)
        assert route_for_start(loaded, first_day).run_days == "MON"
        assert route_for_start(loaded, second_day).run_days == "TUE"
        assert migrated.route_numbers_near(12.15, 75.9, 1, None) == ["16512"]
        east_runs = requests_for("16512", loaded, 12.15, 75.9, NOW + timedelta(days=1))
        assert [(item.start, item.delta) for item in east_runs] == [(second_day, pytest.approx(7))]
    finally:
        migrated._conn.close()
        source._conn.close()


def test_area_and_scene_use_the_timetable_for_each_overlapping_origin_date(tmp_path):
    monday = NOW.date().isoformat()
    tuesday = (NOW.date() + timedelta(days=1)).isoformat()
    slow_stops = parse_route_rows([ROWS[0], {**ROWS[1], "sta_min": 645}])
    schedule = SavedRoute(ROUTE, "MON", (SavedRoute(slow_stops, "TUE"),))
    positions = [live.position_from_lts("16512", _report(train_start_date=start), NOW) for start in (monday, tuesday)]
    store = Store(tmp_path / "tracking.db")
    service = GateAhead(load_settings(), store, None)
    service.map_around = AsyncMock(return_value=OsmAround([GATE], [], [RAIL], False))
    service._positions = AsyncMock(return_value=PositionBatch(["16512"], positions, {"16512": schedule}, {}))

    async def check():
        area = await service.predict_here(*GATE.coords)
        scene = await service.scene_here(*GATE.coords, GATE.id)
        area_etas = {row["start_date"]: row["eta"] for row in area["gate_predictions"]}
        scene_etas = {row["start_date"]: row["eta_min"] for row in scene["trains"]}
        assert area_etas == scene_etas == {monday: 7, tuesday: 10.5}

    try:
        asyncio.run(check())
    finally:
        service.snapshots.close()
        store._conn.close()
