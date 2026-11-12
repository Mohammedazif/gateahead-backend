from __future__ import annotations

from smartrail.config import Settings
from smartrail.geo import along_route, km
from smartrail.models import Crossing, TrainPosition

_ON_RAIL_KM = 0.2
_TRAIN_ON_RAIL_KM = 8.0
_END_SLACK_KM = 0.4
_MOVED_KM = 0.4
_BRACKET_KM = 15.0


def crossing_is_ahead(position: TrainPosition, crossing: Crossing) -> bool:
    code = (crossing.nearest_station or "").upper()
    if not code:
        return False
    dist = km(position.coords, crossing.coords)
    last = (position.last_station or "").upper()
    nxt = (position.next_station or "").upper()
    upcoming = {c.upper() for c in position.upcoming_codes}
    if dist <= 0.7:
        return True
    if dist > 80:
        return False
    if code == last and (position.at_station or dist <= 1.0):
        return True
    if nxt and code == nxt:
        return True
    if upcoming:
        return code in upcoming
    return False


def eta_minutes(position: TrainPosition, crossing: Crossing, settings: Settings) -> float | None:
    if not crossing_is_ahead(position, crossing):
        return None
    dist = km(position.coords, crossing.coords)
    if position.at_station and dist > 0.6:
        if crossing.nearest_station and crossing.nearest_station == position.last_station:
            return (dist / settings.default_speed_kmh) * 60.0
        upcoming = set(position.upcoming_codes)
        if crossing.nearest_station in upcoming:
            return (dist / settings.default_speed_kmh) * 60.0
        if position.last_station and crossing.nearest_station == position.last_station:
            return (dist / settings.default_speed_kmh) * 60.0
        etas = dict(position.station_etas)
        if crossing.nearest_station in etas:
            return (etas[crossing.nearest_station] - position.as_of).total_seconds() / 60.0
        return (dist / settings.default_speed_kmh) * 60.0
    if dist <= 0.6:
        etas = dict(position.station_etas)
        if crossing.nearest_station in etas:
            clock = (etas[crossing.nearest_station] - position.as_of).total_seconds() / 60.0
            if abs(clock) < 15:
                return clock
        return 0.0 if position.at_station else (dist / settings.default_speed_kmh) * 60.0
    etas = dict(position.station_etas)
    if crossing.nearest_station in etas:
        clock = (etas[crossing.nearest_station] - position.as_of).total_seconds() / 60.0
        if crossing.nearest_station == position.next_station or crossing.nearest_station in position.upcoming_codes[:2]:
            return clock
    travelled = (settings.default_speed_kmh * max(0.0, position.age_minutes)) / 60.0
    remaining = dist - travelled
    return (remaining / settings.default_speed_kmh) * 60.0 if settings.default_speed_kmh else None


def route_eta(
    position: TrainPosition,
    crossing: Crossing,
    route: list[tuple[float, float]],
    stations: dict[str, tuple[float, float]],
    settings: Settings,
) -> tuple[bool, float | None, str | None]:
    cross_km = _along(route, crossing.coords, _ON_RAIL_KM)
    if cross_km is None:
        return False, None, None
    train_km = _along(route, position.coords, _TRAIN_ON_RAIL_KM)
    if train_km is None:
        return False, None, None
    last_km = _station_km(route, stations, position.last_station)
    next_km = _station_km(route, stations, position.next_station)
    forward = _forward(train_km, last_km, next_km)
    if forward is None:
        return False, None, None
    ahead_km = (cross_km - train_km) * forward
    if ahead_km < -0.15:
        return True, None, None
    ahead_km = max(0.0, ahead_km)
    bracket = _bracket(position, last_km, next_km)
    clock = _clock_eta(position, train_km, cross_km, next_km, forward)
    if clock is not None:
        return True, clock, bracket
    if not settings.default_speed_kmh:
        return True, None, bracket
    travelled = (settings.default_speed_kmh * max(0.0, position.age_minutes)) / 60.0
    remaining = ahead_km - travelled
    return True, (remaining / settings.default_speed_kmh) * 60.0, bracket


def _along(route: list[tuple[float, float]], point: tuple[float, float], max_off_km: float) -> float | None:
    along, offset = along_route(route, point)
    if offset > max_off_km:
        return None
    # Distance to an endpoint is how far the point is past the fetched rail, not how far it sits beside it.
    if offset > _END_SLACK_KM and (along <= 0.05 or along >= _route_km(route) - 0.05):
        return None
    return along


def _route_km(route: list[tuple[float, float]]) -> float:
    length, _offset = along_route(route, route[-1])
    return length


def _station_km(
    route: list[tuple[float, float]],
    stations: dict[str, tuple[float, float]],
    code: str | None,
) -> float | None:
    if not code:
        return None
    point = stations.get(code.upper())
    if point is None:
        return None
    return _along(route, point, _TRAIN_ON_RAIL_KM)


def _forward(train_km: float, last_km: float | None, next_km: float | None) -> float | None:
    if next_km is not None and abs(next_km - train_km) > 0.05:
        return 1.0 if next_km > train_km else -1.0
    # A station pin a short distance from the reported coordinate is noise, not a direction.
    if last_km is not None and abs(train_km - last_km) > _MOVED_KM:
        return 1.0 if train_km > last_km else -1.0
    return None


def _bracket(position: TrainPosition, last_km: float | None, next_km: float | None) -> str | None:
    if last_km is None or next_km is None or abs(next_km - last_km) < _BRACKET_KM:
        return None
    if not position.last_station or not position.next_station:
        return None
    return f"between {position.last_station} and {position.next_station}"


def _clock_eta(
    position: TrainPosition,
    train_km: float,
    cross_km: float,
    next_km: float | None,
    forward: float,
) -> float | None:
    if next_km is None or not position.next_station:
        return None
    etas = {code.upper(): stamp for code, stamp in position.station_etas}
    nxt = etas.get(position.next_station.upper())
    if nxt is None:
        return None
    span = (next_km - train_km) * forward
    cross_span = (cross_km - train_km) * forward
    if span <= 0.3 or cross_span < 0 or cross_span > span + 0.2:
        return None
    minutes = (nxt - position.as_of).total_seconds() / 60.0
    if minutes <= 0.5:
        return None
    return minutes * (cross_span / span)


def classify(eta: float | None, settings: Settings) -> str:
    if eta is None:
        return "open"
    if eta < -1:
        return "passed"
    if eta <= settings.closed_eta_min:
        return "closed"
    if eta <= settings.closing_soon_eta_min:
        return "closing_soon"
    if eta <= settings.approaching_eta_min:
        return "approaching"
    return "open"
