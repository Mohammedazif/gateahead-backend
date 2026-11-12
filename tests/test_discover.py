import asyncio
from datetime import datetime, timedelta

from smartrail.models import Station, TrainPosition
from smartrail.service import (
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


def test_empty_halt_pair_still_finds_the_mail_on_the_junction_pair():
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
    import smartrail.service as service

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
