from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import aiohttp

from smartrail.config import Settings
from smartrail.geo import along_route, closest_on_segment
from smartrail.halts import clean_code
from smartrail.models import Crossing, RouteStop, SavedRoute, TrainPosition
from smartrail.predict import classify

_URL = "https://www.railyatri.in/time-table/{number}"
_NEXT = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html",
}
_GATE_OFF_KM = 2.0
_TRAIN_OFF_KM = 8.0
_STOP_OFF_KM = 8.0
_ON_RAIL_KM = 0.2
_FAR_MIN = 8 * 60
_LEAD_MIN = 8 * 60
_GRACE_MIN = 20.0
_REACH_KM = 25.0
_LIVE_PAST_MIN = 360.0
_LIVE_AHEAD_MIN = 150.0
_CLOSE_TRUST_MIN = 30.0
_DISAGREE_MIN = 20.0
BEND_HALF_KM = 15.0
_WEEK = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@dataclass(frozen=True)
class TimetableCall:
    eta: float | None
    bracket: str | None
    slack: float
    placed: bool


@dataclass(frozen=True)
class Call:
    eta: float | None
    bracket: str | None
    source: str
    slack: float


def parse_timetable_html(html: str) -> list[RouteStop]:
    return parse_schedule(html)[0]


def parse_schedule(html: str) -> tuple[list[RouteStop], str]:
    match = _NEXT.search(html)
    if not match:
        return [], ""
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return [], ""
    table = payload.get("props", {}).get("pageProps", {}).get("trainTimeTable")
    if not isinstance(table, dict):
        return [], ""
    rows, run_days = _schedule(table)
    return parse_route_rows(rows), run_days


def runs_on(run_days: str, when: datetime) -> bool:
    days = {part.strip().upper()[:3] for part in run_days.split(",") if part.strip()}
    if not days:
        return True
    return _WEEK[when.weekday()] in days


def classify_passage(
    stops: list[RouteStop], run_days: str, lat: float, lon: float, now: datetime
) -> str:
    located = [stop for stop in stops if stop.lat is not None and stop.lon is not None]
    if len(located) < 2:
        return "unknown"
    gate_km = project_km(stops, lat, lon, _REACH_KM)
    if gate_km is None:
        return "skip"
    clock = clock_at(stops, gate_km, "move")
    if clock is None:
        return "unknown"
    found = _best_delta(clock, run_days, now)
    if found is None:
        return "skip"
    delta, _frame = found
    if -_LIVE_PAST_MIN <= delta <= _LIVE_AHEAD_MIN:
        return "hot"
    if _LIVE_AHEAD_MIN < delta <= _FAR_MIN:
        return "cold"
    return "skip"


def scheduled_position(
    number: str, route: SavedRoute, lat: float, lon: float, now: datetime
) -> TrainPosition | None:
    stops = route.stops
    if len(stops) < 2:
        return None
    gate_km = project_km(stops, lat, lon, _REACH_KM)
    clock = clock_at(stops, gate_km, "move") if gate_km is not None else None
    if clock is None:
        clock = float(stops[0].depart_min)
    found = _best_delta(clock, route.run_days, now)
    if found is None:
        return None
    _delta, frame = found
    placed = _km_at_frame(stops, frame)
    if placed is None:
        return None
    covered, run = placed
    last, nxt = _around(stops, covered)
    lat, lon = _coords_at(last, nxt, covered)
    return TrainPosition(
        train_number=number,
        lat=lat,
        lon=lon,
        last_station=last.code,
        next_station=nxt.code if nxt else None,
        at_station=run == "at",
        delay_minutes=0,
        direction="unknown",
        as_of=now,
        current_name=last.name,
        run=run,
        covered_km=covered,
        origin_name=stops[0].name,
        dest_name=stops[-1].name,
    )


def _best_delta(clock: float, run_days: str, now: datetime) -> tuple[float, float] | None:
    minute = now.hour * 60 + now.minute + now.second / 60.0
    best: tuple[float, float] | None = None
    for shift in range(-1, 3):
        if not runs_on(run_days, now + timedelta(days=shift)):
            continue
        delta = clock + shift * 1440 - minute
        frame = minute - shift * 1440
        if best is None or abs(delta) < abs(best[0]):
            best = (delta, frame)
    return best


def _km_at_frame(stops: list[RouteStop], frame: float) -> tuple[float, str] | None:
    first = stops[0]
    if frame <= first.depart_min:
        if first.depart_min - frame > _LEAD_MIN:
            return None
        return first.km, "at"
    if frame >= stops[-1].arrive_min:
        return None
    for origin, dest in zip(stops, stops[1:]):
        if frame < origin.depart_min:
            return origin.km, "at"
        if frame <= dest.arrive_min:
            span = dest.arrive_min - origin.depart_min
            portion = 0.0 if span <= 0 else (frame - origin.depart_min) / span
            covered = origin.km + min(1.0, max(0.0, portion)) * (dest.km - origin.km)
            dwelling = abs(covered - origin.km) <= 0.4 and frame <= origin.depart_min
            return covered, "at" if dwelling else "moving"
    return None


def _around(stops: list[RouteStop], covered: float) -> tuple[RouteStop, RouteStop | None]:
    for origin, dest in zip(stops, stops[1:]):
        if covered <= dest.km + 0.05:
            return origin, dest
    return stops[-1], None


def _coords_at(origin: RouteStop, dest: RouteStop | None, covered: float) -> tuple[float, float]:
    if origin.lat is None or origin.lon is None:
        return 0.0, 0.0
    if dest is None or dest.lat is None or dest.lon is None or dest.km - origin.km < 0.05:
        return origin.lat, origin.lon
    portion = min(1.0, max(0.0, (covered - origin.km) / (dest.km - origin.km)))
    return (
        origin.lat + portion * (dest.lat - origin.lat),
        origin.lon + portion * (dest.lon - origin.lon),
    )


def waiting_position(number: str, route: SavedRoute, now: datetime) -> TrainPosition | None:
    stops = route.stops
    if len(stops) < 2:
        return None
    origin = stops[0]
    wait = _next_wait(origin.depart_min, route.run_days, now)
    if wait is None:
        return None
    clock = origin.depart_min % 1440
    return TrainPosition(
        train_number=number,
        lat=origin.lat if origin.lat is not None else 0.0,
        lon=origin.lon if origin.lon is not None else 0.0,
        last_station=origin.code,
        next_station=stops[1].code,
        at_station=True,
        delay_minutes=0 if wait >= 0 else int(round(-wait)),
        direction="unknown",
        as_of=now,
        current_name=origin.name,
        run="at",
        covered_km=origin.km,
        origin_name=origin.name,
        scheduled_at=f"{clock // 60:02d}:{clock % 60:02d}",
    )


def parse_route_rows(rows: object) -> list[RouteStop]:
    if not isinstance(rows, list):
        return []
    stops: list[RouteStop] = []
    previous: int | None = None
    last_km = -1.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = clean_code(row.get("station_code"))
        km = _float(row.get("distance_from_source"))
        if not code or km is None or km + 0.05 < last_km:
            continue
        arrive, depart = _absolute_minutes(row.get("sta_min"), row.get("std_min"), row.get("day"), previous)
        previous = depart
        last_km = km
        halt = _int(row.get("halt"))
        lat, lon = _india_point(row.get("lat"), row.get("lng"))
        stops.append(
            RouteStop(
                seq=len(stops),
                code=code,
                name=str(row.get("station_name") or code).strip() or code,
                km=km,
                arrive_min=arrive,
                depart_min=depart,
                halt_min=max(0, halt),
                stops=bool(row.get("stop")) or halt > 0,
                lat=lat,
                lon=lon,
            )
        )
    return stops if len(stops) >= 2 else []


def locate_stops(stops: list[RouteStop], coords: dict[str, tuple[float, float]]) -> list[RouteStop]:
    located: list[RouteStop] = []
    for stop in stops:
        if stop.lat is not None and stop.lon is not None:
            located.append(stop)
            continue
        point = coords.get(stop.code)
        if point is None:
            located.append(stop)
            continue
        located.append(replace(stop, lat=point[0], lon=point[1]))
    return located


def timetable_call(
    stops: list[RouteStop],
    position: TrainPosition,
    crossing: Crossing,
    rail: list[tuple[float, float]] | None = None,
) -> TimetableCall:
    placed = train_place(stops, position)
    gate_km = project_km(stops, crossing.lat, crossing.lon, _GATE_OFF_KM)
    if gate_km is None and rail:
        gate_km = _km_on_rail(stops, crossing, rail)
    if placed is None or gate_km is None:
        return TimetableCall(None, None, 0, False)
    train_km, mode = placed
    train_clock = clock_at(stops, train_km, mode)
    gate_clock = clock_at(stops, gate_km, "move")
    if train_clock is None or gate_clock is None:
        return TimetableCall(None, None, 0, False)
    if train_km > gate_km + 0.3:
        return TimetableCall(None, None, 0, True)
    eta = _eta_from_clock(position, train_clock, gate_clock)
    # The clock can say the gate time has passed while the train is still kilometres short of it.
    if eta < 0 and gate_km - train_km > 0.5:
        eta = gate_clock - train_clock
        if position.run != "at":
            eta -= max(0.0, position.age_minutes)
    if eta < -1:
        return TimetableCall(None, None, 0, True)
    span = _span(stops, gate_km)
    bracket = None
    if span and (span[1].km - span[0].km >= 15 or eta > 30):
        bracket = f"between {span[0].code} and {span[1].code}"
    return TimetableCall(eta, bracket, float(halt_slack(stops, train_km, gate_km)), True)


def merge_call(
    on_route: bool,
    eta: float | None,
    bracket: str | None,
    timed: TimetableCall | None,
) -> Call:
    local = Call(eta if on_route else None, bracket if on_route else None, "rail" if on_route else "", 0.0)
    if timed is None:
        return local
    if (
        timed.placed
        and timed.eta is not None
        and local.eta is not None
        and local.eta <= _CLOSE_TRUST_MIN
        and abs(local.eta - timed.eta) > _DISAGREE_MIN
    ):
        return local
    if timed.placed:
        return Call(timed.eta, timed.bracket or local.bracket, "timetable", timed.slack)
    return local


def list_status(eta: float | None, slack: float, settings: Settings, sourced: bool) -> str | None:
    if eta is None or eta < -1:
        return None
    if sourced and eta > _FAR_MIN:
        return None
    status = classify(eta, settings)
    if status == "passed":
        return None
    if sourced and slack > 3 and status in {"closed", "closing_soon"}:
        status = "approaching"
    if not sourced and status == "open":
        return None
    if sourced and status == "open":
        return "later"
    return status


def _eta_from_clock(position: TrainPosition, train_clock: float, gate_clock: float) -> float:
    eta = (gate_clock + position.delay_minutes) - _run_now(position.as_of, train_clock, position.delay_minutes)
    if position.run != "at":
        eta -= max(0.0, position.age_minutes)
    return eta


def _run_now(as_of: datetime, train_clock: float, delay: int) -> float:
    minute = as_of.hour * 60 + as_of.minute + as_of.second / 60.0
    target = train_clock - delay
    return min((minute + 1440 * shift for shift in range(-1, 3)), key=lambda item: abs(item - target))


def project_km(
    stops: list[RouteStop], lat: float, lon: float, max_off_km: float
) -> float | None:
    best: float | None = None
    best_off = max_off_km
    usable = [stop for stop in stops if stop.lat is not None and stop.lon is not None]
    for origin, dest in zip(usable, usable[1:]):
        if dest.km - origin.km < 0.05:
            continue
        _point, portion, offset = closest_on_segment((lat, lon), (origin.lat, origin.lon), (dest.lat, dest.lon))
        if offset < best_off:
            best_off = offset
            best = origin.km + portion * (dest.km - origin.km)
    return best


def train_place(stops: list[RouteStop], position: TrainPosition) -> tuple[float, str] | None:
    if len(stops) < 2:
        return None
    mode = "move"
    if position.run == "departed":
        mode = "depart"
    elif position.at_station or position.run == "at":
        mode = "arrive"
    covered = position.covered_km
    if covered is not None and stops[0].km - 1 <= covered <= stops[-1].km + 1:
        return covered, _origin_clock(stops, covered, mode)
    code = (position.last_station or "").upper()
    for stop in stops:
        if stop.code == code:
            return stop.km, _origin_clock(stops, stop.km, mode)
    if abs(position.lat) > 1 and abs(position.lon) > 1:
        projected = project_km(stops, position.lat, position.lon, _TRAIN_OFF_KM)
        if projected is not None:
            return projected, "move"
    return None


def clock_at(stops: list[RouteStop], km: float, mode: str) -> float | None:
    if len(stops) < 2 or km < stops[0].km - 0.4 or km > stops[-1].km + 0.4:
        return None
    for origin, dest in zip(stops, stops[1:]):
        if km > dest.km + 0.05:
            continue
        if km < origin.km - 0.05:
            break
        if mode in {"arrive", "depart"} and abs(km - origin.km) <= 0.4:
            return float(origin.depart_min if mode == "depart" else origin.arrive_min)
        span = dest.km - origin.km
        if span <= 0.05:
            return float(origin.depart_min)
        portion = min(1.0, max(0.0, (km - origin.km) / span))
        return origin.depart_min + portion * (dest.arrive_min - origin.depart_min)
    if abs(km - stops[-1].km) <= 0.4:
        return float(stops[-1].arrive_min)
    return None


def halt_slack(stops: list[RouteStop], train_km: float, gate_km: float) -> int:
    total = 0
    for stop in stops:
        if train_km + 0.3 < stop.km < gate_km - 0.05 and stop.stops:
            total += stop.halt_min
    return total


def _origin_clock(stops: list[RouteStop], km: float, mode: str) -> str:
    # A stored origin arrival of 0 is midnight, not the departure the train is waiting for.
    if mode == "arrive" and abs(km - stops[0].km) <= 0.4:
        return "depart"
    return mode


def _km_on_rail(
    stops: list[RouteStop], crossing: Crossing, rail: list[tuple[float, float]]
) -> float | None:
    if len(rail) < 2:
        return None
    gate_along, gate_off = along_route(rail, (crossing.lat, crossing.lon))
    if gate_off > _ON_RAIL_KM:
        return None
    placed: list[tuple[float, float]] = []
    for stop in stops:
        if stop.lat is None or stop.lon is None:
            continue
        along, offset = along_route(rail, (stop.lat, stop.lon))
        if offset <= _STOP_OFF_KM:
            placed.append((along, stop.km))
    placed.sort()
    for (start, start_km), (end, end_km) in zip(placed, placed[1:]):
        if end - start < 0.05:
            continue
        if start - 0.05 <= gate_along <= end + 0.05:
            portion = min(1.0, max(0.0, (gate_along - start) / (end - start)))
            return start_km + portion * (end_km - start_km)
    return None


def _next_wait(depart_min: int, run_days: str, now: datetime) -> float | None:
    clock = depart_min % 1440
    minute = now.hour * 60 + now.minute + now.second / 60.0
    best: float | None = None
    for shift in (-1, 0, 1):
        if not runs_on(run_days, now + timedelta(days=shift)):
            continue
        wait = clock + shift * 1440 - minute
        if wait < -_GRACE_MIN or wait > _LEAD_MIN:
            continue
        if best is None or _closer_departure(wait, best):
            best = wait
    return best


def _closer_departure(wait: float, best: float) -> bool:
    if (wait >= 0) != (best >= 0):
        return wait >= 0
    return abs(wait) < abs(best)


def _span(stops: list[RouteStop], km: float) -> tuple[RouteStop, RouteStop] | None:
    for origin, dest in zip(stops, stops[1:]):
        if origin.km - 0.05 <= km <= dest.km + 0.05:
            return origin, dest
    return None


def _schedule(table: dict) -> tuple[list, str]:
    best: list = []
    days = ""
    details = table.get("trainScheduleDetail") or []
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            route = detail.get("route")
            if isinstance(route, list) and len(route) > len(best):
                best = route
                days = _run_days(detail.get("run_days"))
    groups = table.get("timeTableDaysGroup") or []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            items = group.get("items")
            if isinstance(items, list) and len(items) > len(best):
                best = items
                found = _run_days(group.get("run_days"))
                if found:
                    days = found
    return best, days


def _run_days(value: object) -> str:
    if not isinstance(value, str):
        return ""
    seen: list[str] = []
    for token in value.replace("/", ",").split(","):
        key = token.strip().upper()[:3]
        if key in _WEEK and key not in seen:
            seen.append(key)
    return ",".join(seen)


def _absolute_minutes(sta: object, std: object, day: object, previous: int | None) -> tuple[int, int]:
    arrive = _int(sta)
    depart = _int(std) if std not in (None, "") else arrive
    day_no = _int(day) or 1
    if day_no > 1 and arrive < 1440:
        arrive += (day_no - 1) * 1440
        depart += (day_no - 1) * 1440
    while previous is not None and arrive < previous - 30:
        arrive += 1440
        depart += 1440
    if depart < arrive:
        depart += 1440
    return arrive, depart


def _india_point(lat: object, lon: object) -> tuple[float | None, float | None]:
    lat_f, lon_f = _float(lat), _float(lon)
    if lat_f is None or lon_f is None:
        return None, None
    if not (6.0 <= lat_f <= 37.5 and 68.0 <= lon_f <= 98.0):
        return None, None
    return lat_f, lon_f


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


async def fetch_timetable(session: aiohttp.ClientSession, train_number: str) -> tuple[list[RouteStop], str]:
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with session.get(
            _URL.format(number=train_number),
            headers=_HEADERS,
            timeout=timeout,
        ) as response:
            if response.status != 200:
                return [], ""
            html = await response.text()
    except (aiohttp.ClientError, TimeoutError):
        return [], ""
    return parse_schedule(html)
