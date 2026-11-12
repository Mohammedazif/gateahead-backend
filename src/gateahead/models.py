from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

CrossingStatus = Literal["pending", "public", "rejected"]
Direction = Literal["southbound", "northbound", "unknown"]
PinVerdict = Literal["existing", "candidate", "reject"]


@dataclass(frozen=True)
class Crossing:
    id: str
    name: str
    lat: float
    lon: float
    source: str
    status: CrossingStatus = "public"
    nearest_station: str = ""
    osm_id: str = ""
    road: str = ""
    area: str = ""

    @property
    def coords(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True)
class Station:
    code: str
    name: str
    lat: float
    lon: float
    kind: str = "station"

    @property
    def coords(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True)
class Halt:
    code: str
    name: str
    lat: float
    lon: float
    kind: str = "station"
    zone: str = ""
    source: str = ""

    @property
    def coords(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True)
class RouteStop:
    seq: int
    code: str
    name: str
    km: float
    arrive_min: int
    depart_min: int
    halt_min: int
    stops: bool
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True)
class SavedRoute:
    stops: list[RouteStop]
    run_days: str = ""


@dataclass(frozen=True)
class TrainPosition:
    train_number: str
    lat: float
    lon: float
    last_station: str | None
    next_station: str | None
    at_station: bool
    delay_minutes: int
    direction: Direction
    as_of: datetime
    upcoming_codes: tuple[str, ...] = ()
    current_name: str | None = None
    age_minutes: float = 0.0
    station_etas: tuple[tuple[str, datetime], ...] = ()
    name: str = ""
    origin_name: str = ""
    dest_name: str = ""
    next_name: str = ""
    next_in: str = ""
    departed_at: str = ""
    scheduled_at: str = ""
    platform: str = ""
    left_km: int | None = None
    run: str = ""
    covered_km: float | None = None

    @property
    def coords(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True)
class PinCheck:
    verdict: PinVerdict
    reason: str
    rail_m: float | None
    match: Crossing | None = None


@dataclass
class User:
    user_id: int
    lat: float | None = None
    lon: float | None = None
    subscribed: bool = False
    crossing_ids: list[str] = field(default_factory=list)
