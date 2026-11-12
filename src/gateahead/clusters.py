from __future__ import annotations

from dataclasses import dataclass

from gateahead.geo import km
from gateahead.models import Crossing

RADIUS_KM = 0.35


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    crossings: tuple[Crossing, ...]
    lat: float
    lon: float


def cluster_crossings(crossings: list[Crossing], radius_km: float = RADIUS_KM) -> list[Cluster]:
    remaining = list(crossings)
    clusters: list[Cluster] = []
    while remaining:
        members = [remaining.pop(0)]
        grew = True
        while grew:
            grew = False
            kept: list[Crossing] = []
            for item in remaining:
                if any(km(item.coords, other.coords) <= radius_km for other in members):
                    members.append(item)
                    grew = True
                else:
                    kept.append(item)
            remaining = kept
        lat = sum(c.lat for c in members) / len(members)
        lon = sum(c.lon for c in members) / len(members)
        ids = tuple(sorted(c.id for c in members))
        name = _cluster_label(members)
        clusters.append(Cluster(id="cluster:" + ids[0], name=name, crossings=tuple(members), lat=lat, lon=lon))
    return clusters


def maps_link(lat: float, lon: float) -> str:
    return f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"


def place_label(name: str, lat: float, lon: float) -> str:
    return f"{name} ({lat:.4f}, {lon:.4f})"


def gate_label(crossing: Crossing) -> str:
    own = (crossing.name or "").strip()
    generic = own in {"", "Level crossing", "Railway crossing"} or own.startswith("Crossing near ")
    area = (crossing.area or "").strip()
    road = (crossing.road or "").strip()
    if not generic:
        if area and area.lower() not in own.lower():
            return f"{own} · {area}"
        return own
    if road and area:
        return f"{road} · {area}"
    if road:
        return road
    if area:
        return f"Near {area}"
    return "Level crossing"


def _cluster_label(members: list[Crossing]) -> str:
    roads = [c.road for c in members if c.road]
    areas = [c.area or c.nearest_station for c in members if c.area or c.nearest_station]
    area = max(set(areas), key=areas.count) if areas else ""
    if roads:
        road = max(set(roads), key=roads.count)
        return f"{road} · {area}" if area else road
    named = next(
        (
            c.name
            for c in members
            if c.name and c.name != "Level crossing" and not c.name.startswith("Crossing near ")
        ),
        "",
    )
    if named:
        return f"{named} · {area}" if area else named
    if area:
        return f"Near {area}"
    return "Railway crossing"
