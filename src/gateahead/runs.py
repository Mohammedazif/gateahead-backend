"""Origin departure dates for services that can still reach a location."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil

from gateahead.models import SavedRoute
from gateahead.timetable import clock_at, project_km, runs_on


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
    point = project_km(route.stops, lat, lon, 25)
    clock = clock_at(route.stops, point, "move") if point is not None else None
    if clock is None:
        return [] if point is None and sum(s.lat is not None and s.lon is not None for s in route.stops) >= 2 else [RunRequest(number)]
    minute = now.hour * 60 + now.minute + now.second / 60
    requests = []
    for offset in range(-int(clock // 1440) - ceil(max_delay_hours / 24) - 1, 2):
        start = now.date() + timedelta(days=offset)
        if not runs_on(route.run_days, start):
            continue
        delta = clock + offset * 1440 - minute
        if -max_delay_hours * 60 <= delta <= 480:
            requests.append(RunRequest(number, start, delta, delta > 150))
    return requests
