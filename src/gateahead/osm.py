from __future__ import annotations

import time
from dataclasses import dataclass

import aiohttp

from gateahead.geo import distance_to_polyline, km
from gateahead.models import Crossing, Station

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
_cache: dict[tuple[float, float, int], tuple[float, "OsmAround"]] = {}


@dataclass
class OsmAround:
    crossings: list[Crossing]
    stations: list[Station]
    rails: list[list[tuple[float, float]]]
    has_highway: bool
    error: str | None = None


def _around_query(lat: float, lon: float, radius_m: float) -> str:
    r = int(radius_m)
    road = max(120, int(radius_m / 5))
    return f"""
    [out:json][timeout:25];
    (
      node["railway"="level_crossing"](around:{r},{lat},{lon});
      node["railway"="crossing"](around:{r},{lat},{lon});
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
    elements = payload.get("elements") or []
    crossings: list[Crossing] = []
    stations: list[Station] = []
    rails: list[list[tuple[float, float]]] = []
    roads: list[tuple[str, list[tuple[float, float]]]] = []
    has_highway = False
    for el in elements:
        tags = el.get("tags") or {}
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
            label = tags.get("name") or tags.get("ref") or ""
            line = _geom_points(el.get("geometry"))
            if label and len(line) >= 2:
                roads.append((label, line))
    if roads:
        named = []
        for crossing in crossings:
            road = _nearest_road(crossing.coords, roads)
            named.append(
                Crossing(
                    id=crossing.id,
                    name=crossing.name if crossing.name != "Level crossing" else (road or crossing.name),
                    lat=crossing.lat,
                    lon=crossing.lon,
                    source=crossing.source,
                    osm_id=crossing.osm_id,
                    road=road,
                )
            )
        crossings = named
    crossings.sort(key=lambda c: km(origin, c.coords))
    stations.sort(key=lambda s: km(origin, s.coords))
    return crossings, stations, rails, has_highway


def _nearest_road(
    point: tuple[float, float], roads: list[tuple[str, list[tuple[float, float]]]]
) -> str:
    best = ""
    best_d = 0.08
    for label, line in roads:
        d = distance_to_polyline(point, line)
        if d < best_d:
            best_d = d
            best = label
    return best


def _element_coords(el: dict) -> tuple[float, float] | None:
    if el.get("lat") is not None and el.get("lon") is not None:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center")
    if isinstance(center, dict) and center.get("lat") is not None and center.get("lon") is not None:
        return float(center["lat"]), float(center["lon"])
    points = _geom_points(el.get("geometry"))
    if not points:
        return None
    return (
        sum(lat for lat, _lon in points) / len(points),
        sum(lon for _lat, lon in points) / len(points),
    )


def _geom_points(geom: object) -> list[tuple[float, float]]:
    if not isinstance(geom, list):
        return []
    points: list[tuple[float, float]] = []
    for point in geom:
        if not isinstance(point, dict) or point.get("lat") is None or point.get("lon") is None:
            continue
        points.append((float(point["lat"]), float(point["lon"])))
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
            return cached[1]
        query = _around_query(lat, lon, radius_m)
        timeout = aiohttp.ClientTimeout(total=30, sock_read=30)
        last_error = "map_busy"
        for url in OVERPASS_URLS:
            try:
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
            except (aiohttp.ClientError, TimeoutError):
                last_error = "map_busy"
                continue
            except ValueError:
                last_error = "map_unavailable"
                continue
            crossings, stations, rails, has_highway = parse_overpass(payload, (lat, lon))
            result = OsmAround(crossings, stations, rails, has_highway)
            _cache[key] = (now, result)
            return result
        if cached:
            return cached[1]
        return OsmAround([], [], [], False, last_error)
