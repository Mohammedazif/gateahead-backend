from __future__ import annotations

import math

_EARTH_KM = 6371.0088


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1 = math.radians(a[0])
    lon1 = math.radians(a[1])
    lat2 = math.radians(b[0])
    lon2 = math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    half = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return _EARTH_KM * 2 * math.asin(min(1.0, math.sqrt(half)))


def meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    return km(a, b) * 1000.0


def interpolate(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    t = max(0.0, min(1.0, t))
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _dot(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * bx + ay * by


def closest_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], float, float]:
    seg_km = km(start, end)
    if seg_km < 1e-6:
        return start, 0.0, km(point, start)
    lat0 = start[0]
    scale_x = 111.32 * max(0.2, abs(math.cos(math.radians(lat0))))
    scale_y = 110.57
    px = (point[1] - start[1]) * scale_x
    py = (point[0] - start[0]) * scale_y
    ex = (end[1] - start[1]) * scale_x
    ey = (end[0] - start[0]) * scale_y
    t = _dot(px, py, ex, ey) / _dot(ex, ey, ex, ey)
    t = max(0.0, min(1.0, t))
    proj = interpolate(start, end, t)
    return proj, t, km(point, proj)


def nearest_rail(
    point: tuple[float, float],
    rails: list[list[tuple[float, float]]],
    max_km: float = 0.2,
) -> list[tuple[float, float]] | None:
    best: list[tuple[float, float]] | None = None
    best_d = max_km
    for line in rails:
        offset = distance_to_polyline(point, line)
        if offset < best_d:
            best_d = offset
            best = line
    return best


def distance_to_polyline(point: tuple[float, float], line: list[tuple[float, float]]) -> float:
    if not line:
        return float("inf")
    if len(line) == 1:
        return km(point, line[0])
    return min(closest_on_segment(point, line[i], line[i + 1])[2] for i in range(len(line) - 1))


def along_route(
    route: list[tuple[float, float]], point: tuple[float, float]
) -> tuple[float, float]:
    if len(route) == 1:
        return 0.0, km(route[0], point)
    best_along = 0.0
    best_offset = float("inf")
    walked = 0.0
    for i in range(len(route) - 1):
        start, end = route[i], route[i + 1]
        _proj, t, offset = closest_on_segment(point, start, end)
        along = walked + km(start, end) * t
        if offset < best_offset:
            best_offset = offset
            best_along = along
        walked += km(start, end)
    return best_along, best_offset


_JOIN_KM = 0.04


def stitch_rail(
    rails: list[list[tuple[float, float]]],
    point: tuple[float, float],
    half_km: float = 1.5,
) -> list[tuple[float, float]] | None:
    if not rails:
        return None
    seed = nearest_rail(point, rails, max_km=_ON_RAIL_MAX)
    if seed is None:
        return None
    used: set[int] = set()
    for index, line in enumerate(rails):
        if line is seed:
            used.add(index)
            break
    chain = list(seed)
    _grow(chain, rails, used, at_end=True)
    _grow(chain, rails, used, at_end=False)
    return _clip(chain, point, half_km)


def _grow(
    chain: list[tuple[float, float]],
    rails: list[list[tuple[float, float]]],
    used: set[int],
    at_end: bool,
) -> None:
    grew = True
    while grew:
        grew = False
        tip = chain[-1] if at_end else chain[0]
        for index, line in enumerate(rails):
            if index in used or len(line) < 2:
                continue
            if km(tip, line[0]) <= _JOIN_KM:
                extra = list(line[1:]) if at_end else list(reversed(line[1:]))
            elif km(tip, line[-1]) <= _JOIN_KM:
                extra = list(reversed(line[:-1])) if at_end else list(line[:-1])
            else:
                continue
            if at_end:
                chain.extend(extra)
            else:
                chain[:0] = extra
            used.add(index)
            grew = True
            break


def _clip(
    route: list[tuple[float, float]], point: tuple[float, float], half_km: float
) -> list[tuple[float, float]] | None:
    if len(route) < 2:
        return None
    along, offset = along_route(route, point)
    if offset > _ON_RAIL_MAX:
        return None
    start = max(0.0, along - half_km)
    end = along + half_km
    points: list[tuple[float, float]] = []
    walked = 0.0
    started = False
    for index in range(len(route) - 1):
        first, second = route[index], route[index + 1]
        seg = km(first, second)
        nxt = walked + seg
        if not started and nxt + 1e-9 >= start:
            t = 0.0 if seg < 1e-6 else (start - walked) / seg
            points.append(interpolate(first, second, max(0.0, min(1.0, t))))
            started = True
        if started:
            if nxt >= end - 1e-9:
                t = 0.0 if seg < 1e-6 else (end - walked) / seg
                points.append(interpolate(first, second, max(0.0, min(1.0, t))))
                break
            points.append(second)
        walked = nxt
    if len(points) < 2:
        return None
    return points


_ON_RAIL_MAX = 0.2


def advance_along(
    route: list[tuple[float, float]], distance_km: float
) -> tuple[tuple[float, float], list[tuple[float, float]]]:
    if not route:
        raise ValueError("route is empty")
    if distance_km <= 0 or len(route) == 1:
        return route[0], list(route)
    remaining = distance_km
    for i in range(len(route) - 1):
        start, end = route[i], route[i + 1]
        seg = km(start, end)
        if seg < 1e-6:
            continue
        if remaining <= seg:
            point = interpolate(start, end, remaining / seg)
            return point, [point, *route[i + 1 :]]
        remaining -= seg
    return route[-1], [route[-1]]
