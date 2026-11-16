"""Run the real GateAhead service against the simulated corridor, one minute at a time."""

from __future__ import annotations

import asyncio
import contextlib
import math
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from types import SimpleNamespace

import gateahead.service as service
import gateahead.live as live
import gateahead.snapshots as snapshots
from gateahead.config import Settings
from gateahead.clock import INDIA
from gateahead.geo import km as geo_km
from gateahead.live import LiveLook, classify_live
from gateahead.models import Halt
from gateahead.service import GateAhead
from gateahead.store import Store

from gatesim.world import BY_CODE, GATE_BY_NODE, STATIONS, Feed, SimGate, SimTrain, erail_rows, live_payload, osm_around, point_at

DAY = datetime(2026, 11, 16, tzinfo=INDIA)  # a Monday on the railway's clock
# Minutes before the train that the barrier comes down.
LEAD_MIN = 5.0


def settings(data_dir: Path) -> Settings:
    return Settings(
        telegram_bot_token="",
        data_dir=data_dir,
        nearby_radius_m=10000,
        rail_max_m=40,
        duplicate_m=250,
        approaching_eta_min=30,
        closing_soon_eta_min=10,
        gate_lead_min=LEAD_MIN,
        max_ahead_km=80,
        default_speed_kmh=60,
        alert_poll_seconds=180,
        alert_cooldown_min=15,
        log_level="WARNING",
    )


def truth_status(eta: float | None) -> str:
    """What the gate is really doing for one train, from the true arrival minute."""
    if eta is None:
        return "-"
    if eta < -1:
        return "passed"
    if eta <= LEAD_MIN:
        return "closed"
    if eta <= 10:
        return "closing_soon"
    if eta <= 30:
        return "approaching"
    return "later"


@dataclass
class Look:
    minute: float
    where: tuple[float, float]
    true_eta: float | None
    true_status: str
    app_eta: float | None
    app_status: str | None
    scene_eta: float | None
    scene_status: str | None
    others: list[tuple[str, float, str]] = field(default_factory=list)
    sighting: dict | None = None
    train: str = ""
    near_gate: bool = False
    # True when this check also opened the tapped-gate view for the train.
    viewed: bool = False
    app_closes: float | None = None
    # Minutes ago the app says the train went over this gate, from its "passed" list.
    app_passed: float | None = None
    scene_passed: float | None = None
    # Independently derived from the simulated provider, not the app's labels.
    stopped_report_ahead: bool = False
    report_age: float = 0.0
    # The same check seen from every other train on the line: number -> Look for that train.
    rest: list["Look"] = field(default_factory=list)

    @property
    def clock(self) -> str:
        whole = int(round(self.minute)) % 1440
        return f"{whole // 60:02d}:{whole % 60:02d}"


@dataclass
class _Clock:
    minute: float = 0.0
    reports: dict[str, tuple[float, dict]] = field(default_factory=dict)


def _fake_datetime(clock: _Clock):
    class SimDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            moment = DAY + timedelta(minutes=clock.minute)
            return moment.astimezone(tz) if tz is not None else moment

    return SimDateTime


class _FakeOsm:
    async def around(self, lat: float, lon: float, radius_m: float):
        return osm_around(lat, lon, radius_m)


@contextlib.contextmanager
def simulated(trains: list[SimTrain], feeds: dict[str, Feed], *, cache_live: bool = False):
    """Swap the network calls in gateahead.service for the simulated corridor."""
    clock = _Clock()
    by_number = {train.number: train for train in trains}

    async def fake_live(_session, number: str, _cache=None) -> LiveLook:
        train = by_number.get(number)
        if train is None:
            return LiveLook(None, "", False)
        today = live_payload(train, clock.minute, feeds.get(number, Feed()), DAY)
        clock.reports[number] = (clock.minute, today)
        return classify_live(today, None)

    async def fake_timetable(_session, number: str):
        train = by_number.get(number)
        if train is None:
            return [], ""
        return train.timetable_rows(), train.run_days

    async def fake_rows(_session, origin: str, dest: str):
        return erail_rows(trains, origin, dest)

    async def fake_station_rows(_session, code: str, _name: str, **_kwargs):
        return [(train.number, call.depart % 1440)
                for train in trains for call in train.calls if call.code == code and call.halts]

    saved = {
        "datetime": service.datetime,
        "fetch_live": service.fetch_live,
        "fetch_timetable": service.fetch_timetable,
        "train_rows": service.train_rows,
        "station_rows": service.station_rows,
    }
    service.datetime = _fake_datetime(clock)  # type: ignore[assignment]
    service.fetch_live = fake_live  # type: ignore[assignment]
    service.fetch_timetable = fake_timetable  # type: ignore[assignment]
    service.train_rows = fake_rows  # type: ignore[assignment]
    service.station_rows = fake_station_rows  # type: ignore[assignment]
    cache_saved = (live._fetch_live_data, live.time, snapshots.time)
    if cache_live:
        async def fake_live_data(session, number):
            return await fake_live(session, number)

        simulated_time = SimpleNamespace(
            time=lambda: (DAY + timedelta(minutes=clock.minute)).timestamp(),
            monotonic=lambda: clock.minute * 60,
        )
        service.fetch_live = live.fetch_live
        live._fetch_live_data = fake_live_data
        live.time = simulated_time
        snapshots.time = simulated_time
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / "sim.db")
        store.upsert_halts(
            [Halt(s.code, s.name, s.coords[0], s.coords[1], s.kind, "", "sim") for s in STATIONS]
        )
        app = GateAhead(settings(Path(folder)), store, None)  # type: ignore[arg-type]
        app.osm = _FakeOsm()  # type: ignore[assignment]
        try:
            yield app, clock
        finally:
            for name, value in saved.items():
                setattr(service, name, value)
            live._fetch_live_data, live.time, snapshots.time = cache_saved
            app.snapshots.close()
            store._conn.close()


Where = Callable[[float], tuple[float, float]]


def at_gate(gate: SimGate, offset_m: float = 15.0) -> Where:
    lat, lon = gate.coords
    return lambda _minute: (lat, lon + offset_m / 1000.0 / 109.0)


def riding(train: SimTrain) -> Where:
    def where(minute: float) -> tuple[float, float]:
        placed = train.covered_at(minute)
        assert placed is not None
        return point_at(train.s_of_km(placed[0]))

    return where


def pinned(where: Where, first_minute: float) -> Where:
    spot = where(first_minute)
    return lambda _minute: spot


def run(
    trains: list[SimTrain],
    feeds: dict[str, Feed],
    watch: str,
    gate_node: int,
    where: Where,
    minutes: list[float],
    with_scene: bool = True,
    cache_live: bool = False,
) -> list[Look]:
    gate = GATE_BY_NODE[gate_node]
    watched = next(train for train in trains if train.number == watch)
    looks: list[Look] = []

    async def go() -> None:
        with simulated(trains, feeds, cache_live=cache_live) as (app, clock):
            for minute in minutes:
                clock.minute = minute
                lat, lon = where(minute)
                body = await app.predict_here(lat, lon)
                rows = [row for row in body.get("approaching", []) if gate.id in row.get("ids", [])]
                mine = next((row for row in rows if row["train"] == watch), None)
                waiting = {row["number"] for row in body.get("awaiting_update", []) if gate.id in row.get("ids", [])}
                others = [(row["train"], row["eta"], row["status"]) for row in rows if row["train"] != watch]
                gone = {row["train"]: row["ago"] for row in body.get("passed", []) if gate.id in row.get("ids", [])}
                scene_eta = scene_status = scene_passed = None
                scene_trains = {}
                scene_waiting = set()
                scene_gone = {}
                if with_scene:
                    scene = await app.scene_here(lat, lon, gate.id)
                    scene_trains = {row["number"]: row for row in scene.get("trains", [])}
                    scene_waiting = {row["number"] for row in scene.get("awaiting_update", [])}
                    scene_gone = {row["number"]: row["ago_min"] for row in scene.get("passed", [])}
                    hit = scene_trains.get(watch)
                    if hit:
                        scene_eta, scene_status = hit["eta_min"], hit["status"]
                        assert abs(hit["closes_in_min"] - (hit["eta_min"] - LEAD_MIN)) < 0.15
                    elif watch in scene_waiting:
                        scene_status = "awaiting_update"
                    scene_passed = scene_gone.get(watch)
                for row in rows:
                    assert abs(row["closes_in"] - (row["eta"] - LEAD_MIN)) < 0.15, row
                gate_time = watched.gate_time(gate)
                true_eta = None if gate_time is None else gate_time - minute
                sighting = next((s for s in body.get("sightings", []) if s["number"] == watch), None)
                rest = []
                for other in trains:
                    if other.number == watch:
                        continue
                    row = next((item for item in rows if item["train"] == other.number), None)
                    scene_row = scene_trains.get(other.number)
                    when = other.gate_time(gate)
                    eta = None if when is None else when - minute
                    rest.append(
                        Look(minute, (lat, lon), eta, truth_status(eta), row["eta"] if row else None,
                             row["status"] if row else "awaiting_update" if other.number in waiting else None,
                             scene_row["eta_min"] if scene_row else None,
                             scene_row["status"] if scene_row else "awaiting_update" if other.number in scene_waiting else None,
                             train=other.number, viewed=with_scene,
                             near_gate=geo_km((lat, lon), gate.coords) <= 2.0,
                             app_closes=row["closes_in"] if row else None, app_passed=gone.get(other.number),
                             scene_passed=scene_gone.get(other.number),
                             **_report_state(other, feeds.get(other.number, Feed()), minute, gate,
                                             clock.reports.get(other.number)))
                    )
                looks.append(
                    Look(
                        minute=minute,
                        where=(lat, lon),
                        true_eta=true_eta,
                        true_status=truth_status(true_eta),
                        app_eta=mine["eta"] if mine else None,
                        app_status=mine["status"] if mine else "awaiting_update" if watch in waiting else None,
                        scene_eta=scene_eta,
                        scene_status=scene_status,
                        others=others,
                        sighting=sighting,
                        train=watch,
                        rest=rest,
                        near_gate=geo_km((lat, lon), gate.coords) <= 2.0,
                        viewed=with_scene,
                        app_closes=mine["closes_in"] if mine else None,
                        app_passed=gone.get(watch),
                        scene_passed=scene_passed,
                        **_report_state(watched, feeds.get(watch, Feed()), minute, gate, clock.reports.get(watch)),
                    )
                )

    asyncio.run(go())
    return looks


def _report_state(train: SimTrain, feed: Feed, minute: float, gate: SimGate,
                  fetched: tuple[float, dict] | None = None) -> dict:
    fetched_minute, report = fetched if fetched is not None else (minute, live_payload(train, minute, feed, DAY))
    seen = feed.report_minute(fetched_minute)
    covered, state, _index = train.covered_at(seen)
    return {
        "stopped_report_ahead": state in {"before", "at"}
        and report.get("status") == "A"
        and train.gate_time(gate) is not None
        and train.km_of_s(gate.s) > covered,
        "report_age": report.get("status_as_of_min", 0) + max(0, minute - fetched_minute),
    }


def minutes_between(start: str, end: str, step: float = 1.0) -> list[float]:
    if not math.isfinite(step) or step <= 0:
        raise ValueError("Simulation step must be finite and positive")

    def parse(text: str) -> float:
        day = 0
        if text.startswith("+"):
            day, text = 1, text[1:]
        hour, minute = text.split(":")
        return day * 1440 + int(hour) * 60 + int(minute)

    first, last = parse(start), parse(end)
    out = []
    value = first
    while value <= last + 1e-9:
        out.append(value)
        value += step
    return out


__all__ = [
    "BY_CODE",
    "DAY",
    "LEAD_MIN",
    "Look",
    "at_gate",
    "minutes_between",
    "pinned",
    "riding",
    "run",
    "simulated",
    "truth_status",
]
