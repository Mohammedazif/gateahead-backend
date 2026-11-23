"""Persistence, discovery recovery and real journey instances under operating exceptions."""
import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateahead import config, erail, live, service as service_module
from gateahead.catalog import sync_catalog
from gateahead.clock import INDIA
from gateahead.database import consolidate
from gateahead.models import Crossing, Halt, SavedRoute, User
from gateahead.osm import OsmAround
from gateahead.runs import requests_for
from gateahead.service import GateAhead, resolve_position
from gateahead.snapshots import SnapshotCache
from gateahead.store import Store
from gateahead.timetable import parse_route_rows

NOW = datetime(2026, 11, 16, 10, 14, tzinfo=INDIA)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def stops(day=1):
    return parse_route_rows([
        {"station_code": "AAA", "station_name": "South", "distance_from_source": 0,
         "std_min": 600, "day": day, "lat": 12, "lng": 75.2},
        {"station_code": "BBB", "station_name": "North", "distance_from_source": 30,
         "sta_min": 630, "day": day, "lat": 12.3, "lng": 75.2},
    ])


def test_database_directory_is_independent_of_process_working_directory(tmp_path, monkeypatch):
    root = tmp_path / "backend"
    root.mkdir()
    elsewhere = tmp_path / "bot"
    elsewhere.mkdir()
    monkeypatch.setattr(config, "_PROJECT_ROOT", root)
    monkeypatch.delenv("GATEAHEAD_DATA", raising=False)
    monkeypatch.chdir(elsewhere)
    assert config.load_settings().db_path == root / "data/gateahead.db"
    monkeypatch.setenv("GATEAHEAD_DATA", "persistent")
    assert config.load_settings().db_path == root / "persistent/gateahead.db"
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path / "mounted"))
    assert config.load_settings().db_path == tmp_path / "mounted/gateahead.db"


def test_consolidation_retains_newest_routes_and_users_with_recoverable_backups(tmp_path):
    target, source = tmp_path / "persistent.db", tmp_path / "legacy.db"
    first, old = Store(target), Store(source)
    first.save_route("16512", stops(), "MON", fetched_at="2026-11-15T12:00:00")
    old.save_route("16512", stops(day=2), "SUN", fetched_at="2026-11-16T12:00:00")
    first.save_route("16650", stops(), "MON", fetched_at="2026-11-16T12:00:00")
    old.save_route("16650", stops(day=2), "SUN", fetched_at="2020-01-01T12:00:00")
    old.upsert_user(User(user_id=123, lat=12, lon=75, subscribed=True, crossing_ids=["gate"]))
    old.record_station_scan("AAA", False, "http_500", "erail")
    first._conn.close()
    old._conn.close()
    result = consolidate(target, source)
    assert result["imported"]["train_routes"] == 1
    backups = Path(result["backups"])
    assert (backups / "destination.db").is_file() and (backups / "source.db").is_file()
    restored = Store(target)
    assert restored.load_route("16512", None).run_days == "SUN"
    assert restored.load_route("16650", None).run_days == "MON"
    assert restored.get_user(123).subscribed
    assert restored._conn.execute("SELECT fetched_at FROM train_routes WHERE train_number='16512'").fetchone()[0] == "2026-11-16T12:00:00"
    original = Store(source)
    assert original.load_route("16650", None).run_days == "SUN"
    assert consolidate(target, source)["imported"]["train_routes"] == 0
    original._conn.close()
    restored._conn.close()


def test_api_bot_and_worker_connections_share_durable_identity_and_progress(tmp_path):
    path = tmp_path / "volume/gateahead.db"
    api, bot, worker = (Store(path) for _ in range(3))
    worker.save_route("16512", stops(), "MON")
    worker.record_provider("railyatri:timetable", True)
    bot.upsert_user(User(user_id=123, subscribed=True))
    assert api.get_user(123).subscribed
    assert bot.load_route("16512", None).run_days == "MON"
    assert len({s.catalog_status()["database_id"] for s in (api, bot, worker)}) == 1
    for s in (api, bot, worker):
        assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        s._conn.close()
    restarted = Store(path)
    assert restarted.provider_status()[0]["successes"] == 1
    restarted._conn.close()


RY_BOARD = '''<h1>Payyanur (PAY) Railway Station</h1><table>
<tr><th>Train Name/No.</th><th>Arrives</th><th>Departs</th><th>Days of Run</th></tr>
<tr><td>06478 Mangalore Special</td><td>19:14</td><td>19:15</td><td>Daily</td></tr>
<tr><td>16603 Maveli Express</td><td>18:54</td><td>18:55</td><td>Daily</td></tr>
</table>'''


class Response:
    def __init__(self, status, body=""):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return next(self.responses)


def test_failed_board_recovers_through_verified_station_table(monkeypatch):
    monkeypatch.setattr(erail, "_cache", {})
    session = Session(Response(429), Response(200, RY_BOARD))
    rows = asyncio.run(erail.station_rows(session, "PAY", "Payyanur"))
    assert rows == [("06478", 1155), ("16603", 1135)]
    assert rows.source == "railyatri"
    assert rows.attempts == [("erail", "http_429"), ("railyatri", "")]
    assert erail.known_train_names(["06478"])["06478"] == "Mangalore Special"
    assert erail.parse_railyatri_board(RY_BOARD, "CAN")[0] == []
    assert erail.parse_railyatri_board("<h1>Payyanur (PAY)</h1><table><td>16603 12:00</td></table>", "PAY")[0] == []


def test_refresh_health_tracks_real_success_separately_from_attempts(tmp_path, monkeypatch):
    store = Store(tmp_path / "health.db")
    store.upsert_halts([Halt("PAY", "Payyanur", 12, 75)])
    good = erail.TrainRows([("16603", 1135)], source="railyatri", attempts=[("erail", "http_500"), ("railyatri", "")])
    monkeypatch.setattr("gateahead.catalog.station_rows", AsyncMock(return_value=good))
    asyncio.run(sync_catalog(None, store, station_limit=1, route_limit=0, request_interval=0))
    success = store.catalog_status()["last_successful_sync"]
    assert success["stations_checked"] == 1
    assert {p["provider"]: p["state"] for p in store.provider_status()} == {"erail:board": "unavailable", "railyatri:board": "healthy"}
    assert store._conn.execute("SELECT source FROM station_scans").fetchone()[0] == "railyatri"
    store.remember_train_candidates(["16512"])
    monkeypatch.setattr("gateahead.catalog.fetch_timetable", AsyncMock(return_value=([], "")))
    result = asyncio.run(sync_catalog(None, store, station_limit=0, route_limit=1, request_interval=0))
    assert result["route_errors"] == 1
    assert store.catalog_status()["last_successful_sync"] == success
    assert store.catalog_status()["worker"]["state"] == "partial"
    store.record_provider("erail:board", True)
    assert next(p for p in store.provider_status() if p["provider"] == "erail:board")["consecutive_failures"] == 0
    store._conn.close()


def test_daily_overlap_and_day_three_origin_dates_survive_large_delays():
    requests = requests_for("16512", SavedRoute(stops()), 12.15, 75.2, NOW)
    assert {r.start for r in requests} == {date(2026, 11, d) for d in (14, 15, 16)}
    assert len({r.key for r in requests}) == 3
    assert all(not r.cold for r in requests)
    weekly = requests_for("16512", SavedRoute(stops(day=3), "SAT"), 12.15, 75.2, NOW)
    assert [r.start for r in weekly] == [date(2026, 11, 14)]
    assert requests_for("16512", SavedRoute(stops()), 20, 80, NOW) == []
    extended = requests_for("16512", SavedRoute(stops()), 12.15, 75.2, NOW, 72)
    assert date(2026, 11, 13) in {r.start for r in extended}
    future = requests_for("16512", SavedRoute(stops()), 12.15, 75.2, NOW.replace(hour=5))
    assert next(r for r in future if r.start == NOW.date()).cold


@pytest.mark.parametrize("payload, expected", [
    ({"is_cancelled": True}, "cancelled"), ({"message": "Train canceled"}, "cancelled"),
    ({"message": "Train is partially cancelled"}, "partially_cancelled"),
    ({"is_diverted": "true"}, "diverted"), ({"train_status": "Diverted"}, "diverted"),
    ({"message": "Train has not been cancelled"}, "running"),
    ({"message": "Train is not diverted"}, "running"),
    ({"current_station_code": "AAA", "delay": 1800}, "running"),
])
def test_operating_status_recognizes_exceptions_without_reinterpreting_large_delays(payload, expected):
    assert live.operating_status(payload) == expected


def test_requested_date_selects_its_exception_from_overlapping_provider_rows():
    raw = [{"success": True, "train_start_date": "2026-11-16", "current_station_code": "AAA"},
           {"success": False, "train_start_date": "2026-11-15", "message": "Cancelled"}]
    assert live._keep_notice(raw, date(2026, 11, 15))["message"] == "Cancelled"
    assert live._keep_notice(raw, date(2026, 11, 14)) is None


def test_wrong_date_report_cannot_be_relabelled_or_fall_back_to_timetable(monkeypatch):
    monkeypatch.setattr(live, "datetime", Clock)
    fetch = AsyncMock(return_value={"success": True, "train_start_date": "2026-11-16", "current_station_code": "AAA"})
    monkeypatch.setattr(live, "_fetch_lts_api", fetch)
    page = AsyncMock(return_value=None)
    monkeypatch.setattr(live, "_fetch_lts_page", page)
    look = asyncio.run(live.fetch_run(None, "16512", date(2026, 11, 15)))
    assert look.operating_status == "unknown"
    assert resolve_position("16512", look, SavedRoute(stops()), NOW) is None
    assert fetch.await_args.args[2] == 1
    page.assert_awaited_once_with(None, "16512", date(2026, 11, 15))


@pytest.mark.parametrize("state", ["cancelled", "diverted", "partially_cancelled"])
def test_dated_exceptions_are_persisted_without_normal_route_positions(tmp_path, monkeypatch, state):
    monkeypatch.setattr(live, "datetime", Clock)
    payload = {"success": False, "train_start_date": "2026-11-16", "message": state.replace("_", " ")}
    fetch = AsyncMock(return_value=payload)
    monkeypatch.setattr(live, "_fetch_lts_api", fetch)
    async def run():
        cache = SnapshotCache(tmp_path / "runs.db")
        try:
            look = await live.fetch_live(None, "16512", cache, NOW.date())
            assert look.operating_status == state
            assert resolve_position("16512", look, SavedRoute(stops()), NOW) is None
            assert live.cached_live("16512", cache, NOW.date()).operating_status == state
            assert live.cached_live("16512", cache, NOW.date() - timedelta(days=1)) is None
            again = await live.fetch_live(None, "16512", cache, NOW.date())
            assert again.fetched_at == look.fetched_at
            fetch.assert_awaited_once()
        finally:
            await cache.shutdown()
    asyncio.run(run())


def test_two_runs_remain_distinct_when_one_is_cancelled_and_another_is_30_hours_late(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "datetime", Clock)
    service = GateAhead(config.load_settings(), Store(tmp_path / "overlap.db"), None)
    service.store.save_route("16512", stops())
    service._discover_trains = AsyncMock(return_value=[])
    gate = Crossing("gate", "Gate", 12.15, 75.2, "osm")
    service.map_around = AsyncMock(return_value=OsmAround([gate], [], [[(12, 75.2), (12.3, 75.2)]], True))

    async def fetch(session, number, cache, start):
        if start == NOW.date():
            return live.LiveLook({"is_cancelled": True}, "Cancelled", True, start_date=start.isoformat(), operating_status="cancelled")
        if start == NOW.date() - timedelta(days=1):
            return live.LiveLook({"success": True, "current_station_code": "AAA", "status": "D", "cur_stn_lat": 12,
                                  "cur_stn_lng": 75.2, "status_as_of_min": 1, "delay": 1800}, "", False, start_date=start.isoformat())
        return live.LiveLook(None, "", True, start_date=start.isoformat())

    monkeypatch.setattr(service_module, "fetch_live", fetch)
    async def run():
        try:
            prediction = await service.predict_here(12.15, 75.2)
            assert {r["run_id"] for r in prediction["gate_predictions"]} == {"16512:2026-11-15"}
            assert prediction["service_updates"][0]["run_id"] == "16512:2026-11-16"
            assert prediction["service_updates"][0]["status"] == "cancelled"
            assert prediction["sightings"][0]["delay"] == 1800
            scene = await service.scene_here(12.15, 75.2, "gate")
            assert {r["run_id"] for r in scene["trains"]} == {"16512:2026-11-15"}
            assert scene["service_updates"][0]["start_date"] == "2026-11-16"
        finally:
            await service.snapshots.shutdown()
            service.store._conn.close()
    asyncio.run(run())


def test_stopped_run_delay_uses_its_origin_date_instead_of_wrapping_to_nearest_day():
    from gateahead.timetable import _eta_from_clock
    payload = {"success": True, "train_start_date": "2026-11-15", "current_station_code": "AAA", "status": "A",
               "delay": 1470, "status_as_of_min": 1}
    position = live.position_from_lts("16512", payload, NOW)
    assert _eta_from_clock(position, 600, 630, 600) == 46
    today = replace(position, start_date="2026-11-16", delay_minutes=30)
    assert _eta_from_clock(today, 600, 630, 600) == 46


def test_same_service_draws_two_confirmed_running_journeys_without_overwriting_track():
    from gateahead.scene import build_scene
    from gateahead.models import TrainPosition
    base = TrainPosition("16512", 12.01, 75.2, "AAA", "BBB", False, 1440, "northbound", NOW,
                         start_date="2026-11-15", run="moving")
    second = replace(base, lat=12.02, start_date="2026-11-16", delay_minutes=0)
    scene = build_scene(Crossing("gate", "Gate", 12.025, 75.2, "osm"), [[(12, 75.2), (12.3, 75.2)]],
                        [base, second], {"AAA": (12, 75.2), "BBB": (12.3, 75.2)}, config.load_settings(), {"16512": stops()})
    assert {r["run_id"] for r in scene["trains"]} == {"16512:2026-11-15", "16512:2026-11-16"}
    assert len({r["along_km"] for r in scene["trains"]}) == 2


def test_bot_does_not_hide_an_overlapping_running_journey_behind_a_passed_one():
    from gateahead.bot.formatters import predict_text
    body = {"passed": [{"train": "16512", "start_date": "2026-11-15", "ago": 2, "cluster": "Gate"}],
            "sightings": [{"number": "16512", "start_date": "2026-11-16", "age": 1, "name": "Regional", "current": "South"}],
            "service_updates": [{"number": "16650", "start_date": "2026-11-16", "status": "diverted"}]}
    text = predict_text(body)
    assert "16512 Regional (started 2026-11-16)" in text
    assert "16650 (started 2026-11-16): Diverted" in text
    assert "until this journey’s route is confirmed" in text


def test_monitor_distinguishes_missing_progress_stalled_worker_and_stale_success(tmp_path, monkeypatch):
    store = Store(tmp_path / "monitor.db")
    monkeypatch.setattr("gateahead.store.time.time", lambda: 200000.0)
    assert store.worker_status()["state"] == "never_refreshed"
    store.meta_set("train_catalog_last_success", json.dumps({"at": 199950}))
    store.meta_set("train_catalog_heartbeat", "199999")
    assert store.worker_status()["state"] == "healthy"
    store.meta_set("train_catalog_heartbeat", "199000")
    assert store.worker_status()["state"] == "stalled"
    store.meta_set("train_catalog_heartbeat", "199999")
    store.meta_set("train_catalog_last_success", json.dumps({"at": 1}))
    assert store.worker_status()["state"] == "stale"
    store._conn.close()


def test_worker_check_exit_code_requires_successful_progress(tmp_path, monkeypatch, capsys):
    from gateahead import catalog
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["catalog", "check"])
    with pytest.raises(SystemExit) as exit_status:
        catalog.main()
    assert exit_status.value.code == 1
    assert json.loads(capsys.readouterr().out)["worker"]["state"] == "never_refreshed"
    store = Store(tmp_path / "gateahead.db")
    import time
    store.meta_set("train_catalog_last_success", json.dumps({"at": time.time()}))
    store.meta_set("train_catalog_heartbeat", str(time.time()))
    store._conn.close()
    with pytest.raises(SystemExit) as exit_status:
        catalog.main()
    assert exit_status.value.code == 0
    assert json.loads(capsys.readouterr().out)["worker"]["state"] == "healthy"


def test_alert_cooldowns_and_display_keep_overlapping_journeys_separate(tmp_path):
    from types import SimpleNamespace
    from gateahead.alerts import poll_alerts
    store = Store(tmp_path / "alerts.db")
    store.upsert_user(User(user_id=123, lat=12, lon=75.2, subscribed=True))
    rows = [{"train": "16512", "run_id": f"16512:2026-11-{day}", "start_date": f"2026-11-{day}",
             "name": "Regional", "status": "closing_soon", "eta": 6, "closes_in": 1, "cluster": "Gate"}
            for day in (15, 16)]
    service = SimpleNamespace(store=store, settings=config.load_settings(), predict_here=AsyncMock(return_value={"approaching": rows}))
    sender = AsyncMock()
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}), bot=SimpleNamespace(send_message=sender))

    async def run():
        await poll_alerts(context)
        await poll_alerts(context)
        assert sender.await_count == 2
        messages = [call.kwargs["text"] for call in sender.await_args_list]
        assert any("started 2026-11-15" in text for text in messages)
        assert any("started 2026-11-16" in text for text in messages)
        assert all("16512:2026" not in text for text in messages)

    try:
        asyncio.run(run())
    finally:
        store._conn.close()


def test_targeted_refresh_checks_requested_station_and_train_despite_backoff(tmp_path, monkeypatch):
    store = Store(tmp_path / "target.db")
    store.upsert_halts([Halt("AAA", "South", 12, 75.2), Halt("PAY", "Payyanur", 12.09, 75.19)])
    store.record_station_scan("PAY", False, "http_500")
    store.save_route("16650", stops())
    store.record_route_failure("16650")
    board = AsyncMock(return_value=erail.TrainRows([("16650", 600)], source="erail", attempts=[("erail", "")]))
    timetable = AsyncMock(return_value=(stops(), "MON"))
    monkeypatch.setattr("gateahead.catalog.station_rows", board)
    monkeypatch.setattr("gateahead.catalog.fetch_timetable", timetable)
    result = asyncio.run(sync_catalog(None, store, station_limit=1, route_limit=1, request_interval=0,
                                     station_codes=["PAY"], train_numbers=["16650"]))
    assert result["status"] == "ok"
    assert result["stations_checked"] == result["routes_refreshed"] == 1
    assert board.await_args.args[1:] == ("PAY", "Payyanur")
    assert timetable.await_args.args[1] == "16650"
    assert store.load_route("16650", 1).run_days == "MON"
    assert store.worker_status()["state"] == "healthy"
    assert store._conn.execute("SELECT failures FROM station_scans WHERE code='PAY'").fetchone()[0] == 0
    store._conn.close()


def test_unknown_target_releases_its_worker_lease(tmp_path):
    store = Store(tmp_path / "unknown-target.db")
    with pytest.raises(ValueError, match="Target station"):
        asyncio.run(sync_catalog(None, store, station_codes=["MISSING"], route_limit=0, request_interval=0))
    assert store.claim_catalog("next-attempt")
    store._conn.close()


def test_failed_one_shot_refresh_returns_nonzero_and_watch_targets_are_rejected(tmp_path, monkeypatch, capsys):
    from gateahead import catalog
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path))
    store = Store(tmp_path / "gateahead.db")
    store.upsert_halts([Halt("PAY", "Payyanur", 12.09, 75.19)])
    store._conn.close()
    monkeypatch.setattr(catalog, "station_rows", AsyncMock(return_value=erail.TrainRows(unavailable=True, error="timeout")))
    monkeypatch.setattr("sys.argv", ["catalog", "sync", "--station", "PAY", "--station-limit", "1", "--route-limit", "0"])
    with pytest.raises(SystemExit) as failed:
        catalog.main()
    assert failed.value.code == 1
    assert json.loads(capsys.readouterr().out)["sync"]["station_errors"] == 1
    monkeypatch.setattr("sys.argv", ["catalog", "sync", "--watch", "--station", "PAY"])
    with pytest.raises(SystemExit) as invalid:
        catalog.main()
    assert invalid.value.code == 2
