import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from gateahead.models import Station, TrainPosition
from gateahead.service import (
    _clock_rank,
    _cluster_is_shown,
    _discover_trains_from,
    _order_due,
    _pair_plan,
    _passed_local,
)


def test_wide_pair_uses_junctions_when_the_nearest_halt_is_unknown():
    stations = [
        Station("KUY", "Koolipalayam", 11.14, 77.39),
        Station("UKL", "Uttukuli", 11.16, 77.41),
        Station("TUP", "Tiruppur", 11.11, 77.35),
        Station("PTJ", "Podanur Junction", 10.99, 76.97),
        Station("ED", "Erode Junction", 11.34, 77.73),
    ]
    pairs = _pair_plan(stations)
    assert ("PTJ", "ED") in pairs
    assert ("ED", "PTJ") in pairs
    assert pairs[0] == ("PTJ", "ED")


def test_empty_halt_pair_still_finds_the_mail_on_the_junction_pair(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake(session: object, origin: str, dest: str) -> list[tuple[str, int | None]]:
        del session
        calls.append((origin, dest))
        if {origin, dest} == {"PTJ", "ED"} and origin == "PTJ":
            return [("12624", 23 * 60 + 45), ("06028", 8 * 60 + 30)]
        return []

    stations = [
        Station("KUY", "Koolipalayam", 11.14, 77.39),
        Station("UKL", "Uttukuli", 11.16, 77.41),
        Station("TUP", "Tiruppur", 11.11, 77.35),
        Station("PTJ", "Podanur Junction", 10.99, 76.97),
        Station("ED", "Erode Junction", 11.34, 77.73),
    ]
    import gateahead.service as service
    monkeypatch.setattr(service, "station_rows", AsyncMock(return_value=[]))

    original = service.train_rows
    service.train_rows = fake
    try:
        found = asyncio.run(
            _discover_trains_from(None, stations, now=datetime(2026, 10, 18, 1, 2))  # type: ignore[arg-type]
        )
    finally:
        service.train_rows = original
    assert found[0] == "12624"
    assert "06028" not in found or found.index("12624") < found.index("06028")
    assert ("PTJ", "ED") in calls


def test_payyanur_express_is_found_even_when_it_skips_both_neighbouring_stations(monkeypatch):
    import gateahead.service as service

    stations = [Station("PAY", "Payyanur", 12.0919, 75.1948), Station("ELM", "Elimala", 12.0687, 75.2309), Station("TKQ", "Trikaripur", 12.1429, 75.1755)]
    calls = []

    async def board(session, code, name):
        calls.append(code)
        return [("16512", 17 * 60 + 30)] if code == "PAY" else []

    monkeypatch.setattr(service, "station_rows", board)
    monkeypatch.setattr(service, "train_rows", AsyncMock(return_value=[]))
    found = asyncio.run(_discover_trains_from(None, stations, datetime(2026, 11, 16, 17, 25)))
    assert found == ["16512"]
    assert calls == ["PAY", "ELM"]  # Bounded station-board lookups, not a query for every halt.


def test_one_station_can_discover_its_trains_without_a_station_pair(monkeypatch):
    import gateahead.service as service

    monkeypatch.setattr(service, "station_rows", AsyncMock(return_value=[("16512", 1050)]))
    pairs = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "train_rows", pairs)
    found = asyncio.run(_discover_trains_from(None, [Station("PAY", "Payyanur", 12.0919, 75.1948)]))
    assert found == ["16512"]
    pairs.assert_not_awaited()


def test_cached_payyanur_route_survives_an_empty_provider_discovery(tmp_path, monkeypatch):
    from gateahead.config import load_settings
    from gateahead.live import LiveLook
    from gateahead.service import GateAhead
    from gateahead.store import Store
    from gateahead.timetable import parse_route_rows
    import gateahead.service as service_module

    moment = datetime(2026, 11, 16, 17, 25)

    class Clock(datetime):
        @classmethod
        def now(cls):
            return moment

    monkeypatch.setattr(service_module, "datetime", Clock)
    stops = parse_route_rows([
        {"station_code": "CAN", "station_name": "Kannur", "distance_from_source": "90", "sta_min": 1020, "std_min": 1025, "day": 1, "lat": 11.868, "lng": 75.363},
        {"station_code": "PAY", "station_name": "Payyanur", "distance_from_source": "124", "sta_min": 1049, "std_min": 1050, "day": 1, "lat": 12.0919, "lng": 75.1948},
        {"station_code": "NLE", "station_name": "Nileshwar", "distance_from_source": "144", "sta_min": 1069, "std_min": 1070, "day": 1, "lat": 12.259, "lng": 75.134},
    ])
    service = GateAhead.__new__(GateAhead)
    service.settings = load_settings()
    service.store = Store(tmp_path / "discovery.db")
    service.store.save_route("16512", stops)
    service._discover_trains = AsyncMock(return_value=[])
    service.session = None
    service.snapshots = None
    look = LiveLook({"success": True, "current_station_code": "CAN", "cur_stn_lat": 11.868, "cur_stn_lng": 75.363,
                     "status": "T", "train_name": "CLT SBC EXPRESS", "status_as_of_min": 0}, "", False)
    fetch = AsyncMock(return_value=look)
    monkeypatch.setattr(service_module, "fetch_live", fetch)
    numbers, positions, _ = asyncio.run(service._positions([], 12.0919, 75.1948))
    assert numbers == ["16512"]
    assert [position.train_number for position in positions] == ["16512"]
    fetch.assert_awaited_once()
    # An old route or a route in a different region must not become a fallback candidate.
    assert service.store.route_numbers_near(20, 80, 10, 24 * 7) == []
    service.store._conn.execute("UPDATE train_routes SET fetched_at='2020-01-01T00:00:00'")
    assert service.store.route_numbers_near(12.0919, 75.1948, 10, 24 * 7) == []


def test_a_gate_just_past_two_kilometres_still_shows_when_it_is_closing():
    assert _cluster_is_shown("closing_soon", False) is True
    assert _cluster_is_shown("closed", False) is True
    assert _cluster_is_shown("approaching", False) is True
    assert _cluster_is_shown("later", False) is False
    assert _cluster_is_shown("later", True) is True


def test_trains_due_now_come_before_trains_due_later():
    now_min = 1 * 60 + 2
    ordered = _order_due(
        [
            ("06028", 8 * 60 + 30),
            ("12624", 23 * 60 + 45),
            ("66601", 0 * 60 + 40),
        ],
        now_min,
    )
    assert ordered == ["66601", "12624", "06028"]
    assert _clock_rank(23 * 60 + 45, now_min) < _clock_rank(8 * 60 + 30, now_min)


def test_a_train_at_a_local_station_is_not_marked_passed():
    now = datetime(2026, 10, 18, 0, 42)
    here = TrainPosition(
        train_number="12624",
        lat=11.144,
        lon=77.39,
        last_station="KUY",
        next_station="TUP",
        at_station=False,
        delay_minutes=20,
        direction="unknown",
        as_of=now,
        station_etas=(("TUP", now - timedelta(minutes=5)),),
    )
    assert _passed_local(here, 11.144, 77.39, {"KUY", "TUP"}) is False


def test_a_distant_train_with_a_past_local_clock_is_marked_passed():
    now = datetime(2026, 10, 18, 0, 42)
    gone = TrainPosition(
        train_number="12951",
        lat=23.3,
        lon=75.0,
        last_station="RTD",
        next_station="TUP",
        at_station=False,
        delay_minutes=6,
        direction="unknown",
        as_of=now,
        station_etas=(("TUP", now - timedelta(minutes=30)),),
    )
    assert _passed_local(gone, 11.144, 77.39, {"KUY", "TUP"}) is True
