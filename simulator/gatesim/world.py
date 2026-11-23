"""A made-up rail corridor with trains whose true motion is known.

The fake feeds below copy the shapes the real services return (Overpass, erail,
RailYatri live status and timetable), so the real GateAhead code can run on them.
Because the true train position is known at every minute, each answer the app
gives can be checked against what is really happening at the gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from gateahead.geo import km as geo_km
from gateahead.models import RouteStop
from gateahead.osm import parse_overpass

START = (12.40, 75.10)
LENGTH_KM = 130.0
STEP_KM = 0.25
WAY_KM = 2.0


def _build_line() -> list[tuple[float, float]]:
    # A gently winding line heading south, so the rail is not a straight ruler line.
    points = [START]
    lat, lon = START
    steps = int(LENGTH_KM / STEP_KM)
    for index in range(steps):
        s = index * STEP_KM
        heading = math.radians(180 + 22 * math.sin(s / 9.0))
        dlat = STEP_KM * math.cos(heading) / 110.57
        dlon = STEP_KM * math.sin(heading) / (111.32 * math.cos(math.radians(lat)))
        lat, lon = lat + dlat, lon + dlon
        points.append((lat, lon))
    return points


LINE = _build_line()


def point_at(s_km: float) -> tuple[float, float]:
    s_km = max(0.0, min(LENGTH_KM, s_km))
    index = int(s_km / STEP_KM)
    if index >= len(LINE) - 1:
        return LINE[-1]
    frac = (s_km - index * STEP_KM) / STEP_KM
    a, b = LINE[index], LINE[index + 1]
    return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


@dataclass(frozen=True)
class SimStation:
    code: str
    name: str
    s: float
    kind: str = "station"

    @property
    def coords(self) -> tuple[float, float]:
        return point_at(self.s)


@dataclass(frozen=True)
class SimGate:
    node: int
    s: float
    road: str

    @property
    def id(self) -> str:
        return f"osm:node:{self.node}"

    @property
    def coords(self) -> tuple[float, float]:
        return point_at(self.s)


STATIONS = [
    SimStation("NPJ", "Northpur Junction", 0.0),
    SimStation("KDV", "Kadavu", 8.6, "halt"),
    SimStation("PZH", "Puzhakkal", 17.2),
    SimStation("ELM", "Elamkulam", 26.0),
    SimStation("CRK", "Cherukara", 33.4, "halt"),
    SimStation("VLR", "Vellur", 41.0),
    SimStation("MWJ", "Midway Junction", 50.0),
    SimStation("ATK", "Attukal", 58.2, "halt"),
    SimStation("PNM", "Ponnam", 66.0),
    SimStation("TKR", "Thekkara", 75.3),
    SimStation("KLM", "Kalam", 83.0, "halt"),
    SimStation("MNR", "Manur", 92.4),
    SimStation("CHV", "Chavara", 100.1),
    SimStation("ARK", "Arakkal", 110.0, "halt"),
    SimStation("SGJ", "Southgate Junction", 120.0),
]
BY_CODE = {station.code: station for station in STATIONS}

GATES = [
    SimGate(9001, 3.1, "Harbour Road"),
    SimGate(9002, 12.4, "Temple Road"),
    SimGate(9003, 21.7, "Market Road"),
    SimGate(9004, 29.4, "School Road"),  # the gate most scenarios watch
    SimGate(9005, 30.1, "Canal Road"),
    SimGate(9006, 37.8, "Mill Road"),
    SimGate(9007, 41.3, "Station Road"),  # 300 m past Vellur platform
    SimGate(9008, 45.6, "Church Road"),
    SimGate(9009, 54.0, "Bypass"),
    SimGate(9010, 62.5, "Lake Road"),
    SimGate(9011, 71.0, "Hospital Road"),
    SimGate(9012, 88.0, "Beach Road"),
]
GATE_BY_NODE = {gate.node: gate for gate in GATES}


@dataclass(frozen=True)
class Call:
    """One station a train lists in its timetable. Clock values are minutes after midnight of the run day."""

    code: str
    arrive: int
    depart: int
    halts: bool = True


@dataclass
class SimTrain:
    number: str
    name: str
    calls: list[Call]
    southbound: bool = True
    run_days: str = "MON,TUE,WED,THU,FRI,SAT,SUN"
    # Minutes late on leaving each station; stations not listed keep the last value.
    delays: dict[str, float] = field(default_factory=dict)
    start_delay: float = 0.0
    start_offset_days: int = 0

    def origin_date(self, day: date) -> date:
        return day + timedelta(days=self.start_offset_days)

    def run_id(self, day: date) -> str:
        return f"{self.number}:{self.origin_date(day).isoformat()}"

    def s_of(self, code: str) -> float:
        return BY_CODE[code].s

    def km_of(self, code: str) -> float:
        origin = self.s_of(self.calls[0].code)
        return abs(self.s_of(code) - origin)

    def km_of_s(self, s: float) -> float:
        origin = self.s_of(self.calls[0].code)
        return (s - origin) if self.southbound else (origin - s)

    def s_of_km(self, covered: float) -> float:
        origin = self.s_of(self.calls[0].code)
        return origin + covered if self.southbound else origin - covered

    def actual(self) -> list[tuple[Call, float, float, float]]:
        """(call, km, actual arrive, actual depart) using the delay profile."""
        rows = []
        delay = self.start_delay
        origin_shift = self.start_offset_days * 1440
        for index, call in enumerate(self.calls):
            arrive_delay = delay
            delay = self.delays.get(call.code, delay)
            arrive = call.arrive + origin_shift + (arrive_delay if index else delay)
            depart = call.depart + origin_shift + delay
            if call.halts and index:
                depart = max(depart, arrive + max(0.5, call.depart - call.arrive))
            else:
                depart = max(depart, arrive)
            rows.append((call, self.km_of(call.code), arrive, depart))
        return rows

    def covered_at(self, minute: float) -> tuple[float, str, int] | None:
        """True km from origin at a minute, its state (before/at/moving/done), and the index of the last call reached."""
        rows = self.actual()
        if minute < rows[0][3]:
            return rows[0][1], "before", 0
        if minute >= rows[-1][2]:
            return rows[-1][1], "done", len(rows) - 1
        for index, ((_c, km_a, _arr_a, dep_a), (_d, km_b, arr_b, _dep_b)) in enumerate(zip(rows, rows[1:])):
            if minute < dep_a:
                return km_a, "at", index
            if minute < arr_b:
                portion = (minute - dep_a) / max(1e-6, arr_b - dep_a)
                return km_a + portion * (km_b - km_a), "moving", index
        return rows[-1][1], "done", len(rows) - 1

    def time_at_km(self, target: float) -> float | None:
        """True minute the engine reaches a km of its own route."""
        rows = self.actual()
        for (_c, km_a, _arr_a, dep_a), (_d, km_b, arr_b, _dep_b) in zip(rows, rows[1:]):
            if km_a - 1e-6 <= target <= km_b + 1e-6:
                if km_b - km_a < 1e-6:
                    return dep_a
                return dep_a + (target - km_a) / (km_b - km_a) * (arr_b - dep_a)
        return None

    def gate_time(self, gate: SimGate) -> float | None:
        return self.time_at_km(self.km_of_s(gate.s))

    def delay_at(self, index: int) -> float:
        rows = self.actual()
        call, _km, _arrive, depart = rows[index]
        return depart - call.depart - self.start_offset_days * 1440

    def timetable_rows(self) -> list[RouteStop]:
        stops = []
        for seq, call in enumerate(self.calls):
            station = BY_CODE[call.code]
            lat, lon = station.coords
            stops.append(
                RouteStop(
                    seq=seq,
                    code=call.code,
                    name=station.name,
                    km=self.km_of(call.code),
                    arrive_min=call.arrive,
                    depart_min=call.depart,
                    halt_min=call.depart - call.arrive if call.halts else 0,
                    stops=call.halts,
                    lat=lat,
                    lon=lon,
                )
            )
        return stops


@dataclass(frozen=True)
class Feed:
    """How the live status lags behind the train.

    lag: minutes between a real event and the feed showing it.
    refresh: the feed only moves on at this interval.
    stall_from/stall_to: minutes (of the sim day) during which the feed stops updating.
    """

    lag: float = 2.0
    refresh: float = 3.0
    stall_from: float | None = None
    stall_to: float | None = None

    def report_minute(self, now_minute: float) -> float:
        seen = now_minute - self.lag
        seen = math.floor(seen / self.refresh) * self.refresh
        if self.stall_from is not None and self.stall_to is not None:
            if self.stall_from <= seen < self.stall_to:
                seen = self.stall_from
        return seen


def _clock(minute: float) -> str:
    whole = int(round(minute)) % 1440
    return f"{whole // 60:02d}:{whole % 60:02d}"


def live_payload(train: SimTrain, now_minute: float, feed: Feed, day: datetime) -> dict:
    """A RailYatri train_eta_data body for this train as the feed would show it now."""
    seen = feed.report_minute(now_minute)
    age = max(0, int(round(now_minute - seen)))
    rows = train.actual()
    placed = train.covered_at(seen)
    assert placed is not None
    covered, state, index = placed
    origin = BY_CODE[train.calls[0].code]
    dest = BY_CODE[train.calls[-1].code]
    base = {
        "success": True,
        "train_number": train.number,
        "train_start_date": train.origin_date(day.date()).isoformat(),
        "train_name": train.name,
        "source": origin.code,
        "source_stn_name": origin.name.upper(),
        "destination": dest.code,
        "dest_stn_name": dest.name.upper(),
        "total_distance": int(round(rows[-1][1])),
    }
    if state == "before" and rows[0][3] - seen > 30:
        return {**base, "at_src": True, "at_dstn": False, "new_message": "Train hasn't started yet."}
    if state == "done":
        return {**base, "at_src": False, "at_dstn": True, "current_station_code": dest.code}
    current_call, current_km, arrive, depart = rows[index]
    station = BY_CODE[current_call.code]
    ahead = covered - current_km
    if state in {"before", "at"}:
        status = "A"
        ahead = 0.0
    elif current_call.halts:
        status = "D"
    else:
        status = "T"
    upcoming_rows: list[dict] = [{"si_no": 0, "station_code": "", "station_name": "", "non_stops": []}]
    origin_shift = train.start_offset_days * 1440
    delay_now = round(depart - current_call.depart - origin_shift) if status != "A" else round(arrive - current_call.arrive - origin_shift)
    if state == "before":
        delay_now = 0
    next_stop = None
    for call, call_km, _arr, _dep in rows[index + 1 :]:
        eta = call.arrive + origin_shift + delay_now
        row = {
            "station_code": call.code,
            "station_name": BY_CODE[call.code].name.upper(),
            "distance_from_source": int(round(call_km)),
            "distance_from_current_station": int(round(call_km - covered)),
            "sta": _clock(call.arrive),
            "eta": _clock(eta),
            "a_min": int(round(eta)) % 1440,
            "a_day": int(eta // 1440) - int(seen // 1440),
            "halt": call.depart - call.arrive,
        }
        if call.halts:
            upcoming_rows.append(row)
            if next_stop is None:
                next_stop = call
        else:
            upcoming_rows[-1].setdefault("non_stops", []).append(
                {"station_code": call.code, "distance_from_source": int(round(call_km))}
            )
    to_station = BY_CODE[next_stop.code] if next_stop else station
    lat, lon = station.coords
    report = {
        **base,
        "at_src": state == "before" or (index == 0 and status == "A"),
        "at_dstn": False,
        "current_station_code": station.code,
        "current_station_name": station.name.upper() + "~",
        "status": status,
        "delay": int(delay_now),
        "eta": _clock(arrive),
        "etd": _clock(depart),
        "cur_stn_sta": _clock(current_call.arrive),
        "cur_stn_std": _clock(current_call.depart),
        "ahead_distance": int(round(ahead)),
        "ahead_distance_text": "At station" if status == "A" else f"{int(round(ahead))} kms ahead",
        "status_as_of": f"As of {age} mins ago",
        "status_as_of_min": age,
        "travelling_towards": "Travelling South" if train.southbound else "Travelling North",
        "distance_from_source": int(round(covered)),
        "cur_stn_lat": f"{lat:.6f}",
        "cur_stn_lng": f"{lon:.6f}",
        "travelling_from_lat_lng": [f"{lat:.6f}", f"{lon:.6f}"],
        "travelling_to_lat_lng": [f"{to_station.coords[0]:.6f}", f"{to_station.coords[1]:.6f}"],
        "upcoming_stations": upcoming_rows,
        "update_time": (day + timedelta(minutes=seen)).strftime("%Y-%m-%d %H:%M:00 +0530"),
    }
    if next_stop:
        report["next_stoppage_info"] = {
            "next_stoppage": BY_CODE[next_stop.code].name.upper(),
            "next_stoppage_time_diff": f"in {max(0, int(next_stop.arrive + delay_now - now_minute))} mins",
        }
    return report


def overpass_payload(lat: float, lon: float, radius_m: float) -> dict:
    """What Overpass would return around a point on this corridor."""
    here = (lat, lon)
    radius = radius_m / 1000.0
    elements: list[dict] = []
    for gate in GATES:
        if geo_km(here, gate.coords) <= radius:
            elements.append(
                {"type": "node", "id": gate.node, "lat": gate.coords[0], "lon": gate.coords[1], "tags": {"railway": "level_crossing"}}
            )
            y, x = gate.coords
            elements.append(
                {
                    "type": "way",
                    "id": gate.node + 50000,
                    "tags": {"highway": "tertiary", "name": gate.road},
                    "nodes": [gate.node + 100_000, gate.node, gate.node + 200_000],
                    "geometry": [{"lat": y, "lon": x - 0.004}, {"lat": y, "lon": x}, {"lat": y, "lon": x + 0.004}],
                }
            )
    for station in STATIONS:
        if geo_km(here, station.coords) <= 60:
            elements.append(
                {
                    "type": "node",
                    "id": 7000 + int(station.s * 10),
                    "lat": station.coords[0],
                    "lon": station.coords[1],
                    "tags": {"railway": station.kind, "ref": station.code, "name": station.name},
                }
            )
    start = 0.0
    way_id = 1
    while start < LENGTH_KM:
        end = min(LENGTH_KM, start + WAY_KM)
        steps = max(2, int((end - start) / STEP_KM) + 1)
        points = [point_at(start + (end - start) * i / (steps - 1)) for i in range(steps)]
        if any(geo_km(here, point) <= radius for point in points):
            elements.append(
                {
                    "type": "way",
                    "id": way_id,
                    "tags": {"railway": "rail"},
                    "geometry": [{"lat": p[0], "lon": p[1]} for p in points],
                }
            )
        way_id += 1
        start = end
    return {"elements": elements}


def osm_around(lat: float, lon: float, radius_m: float):
    from gateahead.osm import OsmAround

    crossings, stations, rails, has_highway = parse_overpass(overpass_payload(lat, lon, radius_m), (lat, lon))
    return OsmAround(crossings, stations, rails, has_highway)


def erail_rows(trains: list[SimTrain], origin: str, dest: str) -> list[tuple[str, int | None]]:
    """erail lists trains that stop at both stations, in that order, with the departure clock at the origin."""
    found: list[tuple[str, int | None]] = []
    for train in trains:
        codes = [call.code for call in train.calls if call.halts]
        if origin in codes and dest in codes and codes.index(origin) < codes.index(dest):
            call = next(call for call in train.calls if call.code == origin)
            found.append((train.number, call.depart % 1440))
    return found
