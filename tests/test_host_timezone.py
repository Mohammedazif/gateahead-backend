"""Indian railway clocks must not depend on the deployment host's timezone."""

import asyncio
import os
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import gateahead.live as live_module
import gateahead.service as service_module
from gateahead.clock import INDIA
from gateahead.config import load_settings
from gateahead.live import LiveLook, position_from_lts
from gateahead.models import Crossing, Station
from gateahead.osm import OsmAround
from gateahead.service import GateAhead, _discover_trains_from
from gateahead.store import Store
from gateahead.timetable import parse_route_rows


@pytest.fixture(params=["UTC", "Asia/Kolkata"])
def host_timezone(request, monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("Paired host timezone changes require tzset")
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    try:
        yield request.param
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def frozen_clock(instant):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(instant.timestamp(), tz)

    return FrozenDateTime


@pytest.mark.parametrize("night", [False, True], ids=["afternoon", "indian-midnight-weekday"])
def test_prediction_eligibility_eta_and_scene_epoch_match_on_both_hosts(host_timezone, tmp_path, monkeypatch, night):
    # The second instant is Monday in UTC and Tuesday in India. The Monday
    # train is still running after midnight on its origin operating day.
    instant = datetime(2026, 11, 16, 18, 35, tzinfo=timezone.utc) if night else datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(service_module, "datetime", frozen_clock(instant))
    origin, arrival = (1435, 25) if night else (1050, 1080)
    stops = parse_route_rows([
        {"station_code": "AAA", "distance_from_source": 0, "std_min": origin,
         "day": 1, "lat": 12, "lng": 75.2},
        {"station_code": "BBB", "distance_from_source": 30, "sta_min": arrival,
         "std_min": arrival + 1, "day": 2 if night else 1, "lat": 12.27, "lng": 75.2},
    ])
    store = Store(tmp_path / "host.db")
    store.save_route("16512", stops, "MON")
    service = GateAhead(load_settings(), store, None)
    service._discover_trains = AsyncMock(return_value=["16512"])
    service.map_around = AsyncMock(return_value=OsmAround(
        [Crossing("g", "Gate", 12.135, 75.2, "osm")],
        [Station("AAA", "Origin", 12, 75.2), Station("BBB", "Destination", 12.27, 75.2)],
        [[(12, 75.2), (12.27, 75.2)]], True))
    monkeypatch.setattr(service_module, "cached_live", lambda *args: None)
    report = {"success": True, "current_station_code": "AAA", "next_station_code": "BBB", "status": "D",
              "cur_stn_lat": 12.09, "cur_stn_lng": 75.2, "distance_from_source": 10,
              "status_as_of_min": 0, "travelling_towards": "North"} if night else None
    fetch = AsyncMock(return_value=LiveLook(report, "not started" if report is None else "", False))
    monkeypatch.setattr(service_module, "fetch_live", fetch)
    try:
        prediction = asyncio.run(service.predict_here(12.135, 75.2))
        assert prediction["coverage"]["live_candidates"] == 1
        assert prediction["coverage"]["live_checked"] == 1
        row = prediction["gate_predictions"][0]
        assert row["eta"] == pytest.approx(5 if night else 15)
        assert row["status"] == ("closed" if night else "approaching")
        generated = datetime.fromisoformat(prediction["generated_at"])
        assert generated.timestamp() == instant.timestamp()
        assert generated.utcoffset() == INDIA.utcoffset(None)
        if night:
            assert generated.weekday() == 1 and generated.hour == 0 and generated.minute == 5
        scene = asyncio.run(service.scene_here(12.135, 75.2, "g"))
        assert scene["trains"][0]["eta_min"] == row["eta"]
        assert scene["trains"][0]["snapshot_at"] == instant.timestamp()
    finally:
        service.snapshots.close()
        store._conn.close()


def test_default_live_report_and_upcoming_clocks_are_indian_and_epoch_correct(host_timezone, monkeypatch):
    instant = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(live_module, "datetime", frozen_clock(instant))
    position = position_from_lts("16512", {
        "current_station_code": "AAA", "status": "D", "cur_stn_lat": 12, "cur_stn_lng": 75,
        "upcoming_stations": [{"station_code": "BBB", "a_min": 1080}], "status_as_of_min": 1,
    })
    assert position.as_of.hour == 17 and position.as_of.minute == 30
    assert position.as_of.timestamp() == instant.timestamp()
    assert (position.station_etas[0][1] - position.as_of).total_seconds() == 1800


def test_discovery_priority_uses_indian_local_clock_after_midnight(host_timezone, monkeypatch):
    instant = datetime(2026, 11, 16, 18, 35, tzinfo=timezone.utc)
    monkeypatch.setattr(service_module, "datetime", frozen_clock(instant))
    monkeypatch.setattr(service_module, "station_rows", AsyncMock(return_value=[("10000", 4), ("10001", 1120)]))
    monkeypatch.setattr(service_module, "train_rows", AsyncMock(return_value=[]))
    result = asyncio.run(_discover_trains_from(None, [Station("AAA", "Origin", 12, 75)]))
    assert result == ["10000", "10001"]


def test_explicit_naive_live_clock_is_still_supported():
    explicit = datetime(2026, 11, 16, 17, 30)
    position = position_from_lts("16512", {"current_station_code": "AAA", "status": "A"}, now=explicit)
    assert position.as_of == explicit and position.as_of.tzinfo is None
