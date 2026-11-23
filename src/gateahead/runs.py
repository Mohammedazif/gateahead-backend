"""Origin departure dates for services that can still reach a location."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil

from gateahead.models import SavedRoute
from gateahead.timetable import clock_at, project_km, route_for_start, runs_on


@dataclass(frozen=True)
class RunRequest:
    number: str
    start: date | None = None
    delta: float | None = None
    cold: bool = False

    @property
    def key(self) -> str:
        return f"{self.number}:{self.start.isoformat()}" if self.start else self.number


def requests_for(number: str, route: SavedRoute | None, lat: float, lon: float, now: datetime,
                 max_delay_hours: float = 48) -> list[RunRequest]:
    if route is None:
        return [RunRequest(number)]
    minute = now.hour * 60 + now.minute + now.second / 60
    requests = []
    unlocated = False
    for variant in (route, *route.variants):
        point = project_km(variant.stops, lat, lon, 25)
        clock = clock_at(variant.stops, point, "move") if point is not None else None
        if clock is None:
            unlocated |= point is not None or sum(s.lat is not None and s.lon is not None for s in variant.stops) < 2
            continue
        for offset in range(-int(clock // 1440) - ceil(max_delay_hours / 24) - 1, 2):
            start = now.date() + timedelta(days=offset)
            if not runs_on(variant.run_days, start) or route_for_start(route, start) is not variant:
                continue
            delta = clock + offset * 1440 - minute
            if -max_delay_hours * 60 <= delta <= 480:
                requests.append(RunRequest(number, start, delta, delta > 150))
    if not requests and unlocated:
        return [RunRequest(number)]
    return sorted(requests, key=lambda request: request.start)
