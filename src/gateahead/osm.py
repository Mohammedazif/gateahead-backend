from __future__ import annotations

import asyncio
import time
import math
from dataclasses import dataclass, field, replace

import aiohttp

from gateahead.geo import km
from gateahead.models import Crossing, Station
from gateahead.crossing_roads import FREIGHT_NAME, FREIGHT_OPERATOR, road_crossings

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_URLS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_HEADERS = {
    "User-Agent": "GateAhead/0.3 (https://github.com/Mohammedazif/gateahead-tele)",
    "Accept": "application/json",
}
_CACHE_TTL = 900.0
_MAX_STALE_AGE = 24 * 60 * 60
_CACHE_CAPACITY = 256
_LOOKUP_BUDGET = 75.0
_ATTEMPT_TIMEOUT = 30.0
_cache: dict[tuple[float, float, int], tuple[float, "OsmAround"]] = {}


@dataclass
class OsmAround:
    crossings: list[Crossing]
    stations: list[Station]
    rails: list[list[tuple[float, float]]]
    has_highway: bool
    error: str | None = None
    excluded_crossing_ids: set[str] = field(default_factory=set)
    excluded_crossing_points: set[tuple[float, float]] = field(default_factory=set)
    map_warning: str | None = None
    map_age_seconds: int | None = None
    cached: bool = field(default=False, compare=False)

    def freshness(self) -> dict:
        data = {}
        if self.map_warning:
            data["map_warning"] = self.map_warning
        if self.map_age_seconds is not None:
            data["map_age_seconds"] = self.map_age_seconds
        return data


def _around_query(lat: float, lon: float, radius_m: float) -> str:
    r = int(radius_m)
    road = max(120, int(radius_m / 5))
    facility = '[~"^(landuse|railway|building)$"~"^(industrial|railway|yard|warehouse)$"]'
    return f"""
    [out:json][timeout:25];
    (
      node["railway"="level_crossing"](around:{r},{lat},{lon});
      node["railway"="crossing"](around:{r},{lat},{lon});
    )->.crossings;
    (
      .crossings;
      way(bn.crossings)["highway"];
      way(bn.crossings)["railway"];
      way{facility}["name"~"{FREIGHT_NAME}",i](around:{r},{lat},{lon});
      way{facility}["name:en"~"{FREIGHT_NAME}",i](around:{r},{lat},{lon});
      way{facility}["operator"~"{FREIGHT_OPERATOR}",i](around:{r},{lat},{lon});
      node["railway"="halt"](around:60000,{lat},{lon});
      node["railway"="station"](around:60000,{lat},{lon});
      way["railway"="station"](around:60000,{lat},{lon});
      way["railway"="halt"](around:60000,{lat},{lon});
      way["railway"="rail"](around:{r},{lat},{lon});
      way["highway"](around:{road},{lat},{lon});
    );
    out body geom;
    """


def parse_overpass(payload: dict, origin: tuple[float, float]) -> tuple[list[Crossing], list[Station], list[list[tuple[float, float]]], bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list) or payload.get("remark"):
        raise ValueError("Map provider returned an incomplete response")
    elements = payload["elements"]
    crossings: list[Crossing] = []
    stations: list[Station] = []
    rails: list[list[tuple[float, float]]] = []
    has_highway = False
    for el in elements:
        if not isinstance(el, dict):
            raise ValueError("Map provider returned an invalid element")
        if (type(el.get("id")) is not int or el.get("type") not in ("node", "way", "relation")
                or any(el.get(key) is not None and not isinstance(el[key], list)
                       for key in ("nodes", "geometry"))
                or any(type(node) is not int for node in el.get("nodes") or [])
                or any(point is not None and not isinstance(point, dict) for point in el.get("geometry") or [])):
            raise ValueError("Map provider returned invalid element geometry")
        if el.get("tags") is not None and not isinstance(el["tags"], dict):
            raise ValueError("Map provider returned invalid tags")
        tags = el.get("tags") or {}
        if any(tags.get(key) is not None and not isinstance(tags[key], str)
               for key in ("railway", "highway", "ref", "name", "name:en", "access", "vehicle", "motor_vehicle",
                           "operator", "landuse", "building", "usage", "service")):
            raise ValueError("Map provider returned invalid tags")
        kind = el.get("type")
        railway = tags.get("railway", "")
        coords = _element_coords(el)
        if coords is None:
            if kind == "way" and tags.get("highway"):
                has_highway = True
            continue
        lat, lon = coords
        if kind == "node" and railway in {"level_crossing", "crossing"}:
            osm_id = f"osm:node:{el.get('id')}"
            name = tags.get("name:en") or tags.get("name") or tags.get("ref") or "Level crossing"
            crossings.append(
                Crossing(
                    id=osm_id,
                    name=name,
                    lat=lat,
                    lon=lon,
                    source="osm",
                    osm_id=osm_id,
                )
            )
        elif railway in {"station", "halt"}:
            ref = str(tags.get("ref") or "").upper().strip()
            if not ref:
                continue
            stations.append(
                Station(
                    code=ref.split(";")[0].strip(),
                    name=tags.get("name:en") or tags.get("name") or ref,
                    lat=lat,
                    lon=lon,
                    kind="halt" if railway == "halt" else "station",
                )
            )
        elif kind == "way" and railway == "rail":
            line = _geom_points(el.get("geometry"))
            if len(line) >= 2:
                rails.append(line)
        elif kind == "way" and tags.get("highway"):
            has_highway = True
    crossings = road_crossings(crossings, elements)
    crossings.sort(key=lambda c: km(origin, c.coords))
    stations.sort(key=lambda s: km(origin, s.coords))
    return crossings, stations, rails, has_highway


def _element_coords(el: dict) -> tuple[float, float] | None:
    if el.get("lat") is not None and el.get("lon") is not None:
        return _coordinates(el["lat"], el["lon"])
    center = el.get("center")
    if isinstance(center, dict) and center.get("lat") is not None and center.get("lon") is not None:
        return _coordinates(center["lat"], center["lon"])
    points = _geom_points(el.get("geometry"))
    if not points:
        return None
    return (
        sum(lat for lat, _lon in points) / len(points),
        sum(lon for _lat, lon in points) / len(points),
    )


def _coordinates(lat: object, lon: object) -> tuple[float, float]:
    try:
        latitude, longitude = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Map provider returned invalid coordinates") from exc
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Map provider returned invalid coordinates")
    return latitude, longitude


def _geom_points(geom: object) -> list[tuple[float, float]]:
    if not isinstance(geom, list):
        return []
    points: list[tuple[float, float]] = []
    for point in geom:
        if not isinstance(point, dict) or point.get("lat") is None or point.get("lon") is None:
            continue
        points.append(_coordinates(point["lat"], point["lon"]))
    return points


class OsmClient:
    def __init__(self, session: aiohttp.ClientSession, url: str = OVERPASS_URL):
        self.session = session
        self.url = url

    async def around(self, lat: float, lon: float, radius_m: float) -> OsmAround:
        key = (round(lat, 3), round(lon, 3), int(radius_m))
        cached = _cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < _CACHE_TTL:
            return replace(cached[1], cached=True)
        query = _around_query(lat, lon, radius_m)
        deadline = now + _LOOKUP_BUDGET
        last_error = "map_busy"
        for url in OVERPASS_URLS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempt = min(_ATTEMPT_TIMEOUT, remaining)
            timeout = aiohttp.ClientTimeout(total=attempt, sock_read=attempt)
            try:
                # All mirrors share one deadline below the browser's 90-second
                # request window. The outer timeout also bounds aiohttp's
                # rounding of per-attempt deadlines.
                async with asyncio.timeout(remaining):
                    async with self.session.post(
                        url,
                        data={"data": query},
                        headers=OVERPASS_HEADERS,
                        timeout=timeout,
                    ) as response:
                        if response.status in {429, 504, 502}:
                            last_error = "map_busy"
                            continue
                        if response.status != 200:
                            last_error = "map_unavailable"
                            continue
                        payload = await response.json(content_type=None)
                        crossings, stations, rails, has_highway = parse_overpass(payload, (lat, lon))
            except (aiohttp.ClientError, TimeoutError):
                last_error = "map_busy"
                continue
            except ValueError:
                last_error = "map_unavailable"
                continue
            candidates = {f"osm:node:{el.get('id')}": coords for el in payload["elements"]
                          if el.get("type") == "node"
                          and (el.get("tags") or {}).get("railway") in {"level_crossing", "crossing"}
                          and (coords := _element_coords(el)) is not None}
            excluded = candidates.keys() - {c.id for c in crossings}
            excluded_points = {candidates[key] for key in excluded} - {c.coords for c in crossings}
            result = OsmAround(crossings, stations, rails, has_highway,
                               excluded_crossing_ids=excluded, excluded_crossing_points=excluded_points)
            _cache.pop(key, None)
            _cache[key] = (time.monotonic(), result)
            if len(_cache) > _CACHE_CAPACITY:
                _cache.pop(next(iter(_cache)))
            return result
        if cached:
            age = time.monotonic() - cached[0]
            if age <= _MAX_STALE_AGE:
                return replace(cached[1], map_warning=last_error, map_age_seconds=int(age), cached=True)
            _cache.pop(key, None)
        return OsmAround([], [], [], False, last_error)
