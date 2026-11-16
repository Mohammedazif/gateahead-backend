"""Public vehicle crossings and small, topology-backed display groups.

OSM has one crossing node per track. These groups describe a road crossing
several tracks; they do not replace the nodes used for train predictions.
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import replace

from gateahead.geo import km
from gateahead.models import Crossing

_GROUP_KM = 0.08
_RESTRICTED = {"no", "private", "customers", "delivery", "agricultural", "forestry", "permit"}
_FOOTWAYS = {"footway", "path", "cycleway", "bridleway", "steps", "pedestrian", "platform"}
_NON_ROADS = {"construction", "proposed", "abandoned", "razed", "raceway", "corridor"}
_MOTOR_ACCESS = {"yes", "designated", "permissive", "destination"}


def _access(tags: dict) -> str:
    for key in ("motor_vehicle", "vehicle", "access"):
        if tags.get(key):
            return tags[key].strip().lower()
    return ""


def _public_road(tags: dict) -> bool:
    highway = tags.get("highway", "")
    if not highway or highway in _NON_ROADS or _access(tags) in _RESTRICTED:
        return False
    if highway in _FOOTWAYS:
        return tags.get("motor_vehicle", "").lower() in _MOTOR_ACCESS
    return True


def road_crossings(candidates: list[Crossing], elements: list[dict]) -> list[Crossing]:
    nodes = {c.osm_id.removeprefix("osm:node:"): c for c in candidates}
    node_tags = {
        str(el.get("id")): el.get("tags") or {}
        for el in elements if el.get("type") == "node"
    }
    # A nearby road is not evidence that a crossing belongs to that road.
    # OSM way node IDs are authoritative; exact geometry vertices support
    # geometry-only map responses without snapping to unrelated roads.
    coords = {c.coords: key for key, c in nodes.items()}
    connected: dict[str, list[dict]] = defaultdict(list)
    graph: dict[str, dict[str, float]] = defaultdict(dict)
    rails: dict[str, set[str]] = defaultdict(set)
    for el in elements:
        tags = el.get("tags") or {}
        if el.get("type") == "way" and tags.get("railway"):
            for key in el.get("nodes") or []:
                if str(key) in nodes:
                    rails[str(key)].add(str(el.get("id")))
        if el.get("type") != "way" or not tags.get("highway"):
            continue
        raw_ids = el.get("nodes") or []
        geometry = el.get("geometry") or []
        vertices: list[tuple[str, tuple[float, float] | None]] = []
        for i in range(max(len(raw_ids), len(geometry))):
            point = geometry[i] if i < len(geometry) else None
            position = None
            if isinstance(point, dict) and point.get("lat") is not None and point.get("lon") is not None:
                position = (float(point["lat"]), float(point["lon"]))
            key = str(raw_ids[i]) if i < len(raw_ids) else (
                coords.get(position) or (f"point:{position}" if position else "")
            )
            if not key:
                continue
            if position is None and key in nodes:
                position = nodes[key].coords
            vertices.append((key, position))
            if key in nodes:
                connected[key].append(tags)
        if not _public_road(tags):
            continue
        for (a, a_pos), (b, b_pos) in zip(vertices, vertices[1:]):
            if a == b or a_pos is None or b_pos is None:
                continue
            distance = km(a_pos, b_pos)
            graph[a][b] = min(distance, graph[a].get(b, float("inf")))
            graph[b][a] = graph[a][b]

    retained: dict[str, Crossing] = {}
    for key, crossing in nodes.items():
        tags = node_tags.get(key, {})
        if _access(tags) in _RESTRICTED:
            continue
        roads = connected.get(key, [])
        public = [road for road in roads if _public_road(road)]
        if roads and not public:
            continue
        # railway=crossing normally denotes a pedestrian crossing. Retain it
        # when the connected road explicitly supplies vehicle access evidence.
        if not roads and tags.get("railway") == "crossing":
            continue
        names = sorted({r.get("name:en") or r.get("name") or r.get("ref") or "" for r in public} - {""})
        road = names[0] if names else ""
        retained[key] = replace(crossing, road=road,
                                name=road if road and crossing.name == "Level crossing" else crossing.name)

    # Search along public road edges, stopping at junctions. A short distance
    # alone, shared road name, rail service tag, or nearby yard never groups.
    pairs: set[tuple[str, str]] = set()
    for start in retained:
        queue = [(0.0, start)]
        visited: dict[str, float] = {}
        while queue:
            distance, node = heapq.heappop(queue)
            if distance > _GROUP_KM or node in visited:
                continue
            visited[node] = distance
            if node != start and node in retained:
                # A road looping across the same track twice describes two
                # separate crossings, even when the road path is very short.
                if not rails[start].intersection(rails[node]):
                    pairs.add(tuple(sorted((start, node))))
            if node != start and len(graph.get(node, {})) > 2:
                continue
            for other, length in graph.get(node, {}).items():
                if other not in visited and distance + length <= _GROUP_KM:
                    heapq.heappush(queue, (distance + length, other))

    groups = {key: {key} for key in retained}
    for a, b in sorted(pairs):
        combined = groups[a] | groups[b]
        # Prevent a chain of nearby gates from growing into a large cluster.
        if any(tuple(sorted((x, y))) not in pairs
               for x in combined for y in combined if x != y):
            continue
        for key in combined:
            groups[key] = combined
    result = []
    for key, crossing in retained.items():
        members = groups[key]
        if len(members) > 1:
            canonical = retained[min(members, key=lambda member: retained[member].id)]
            crossing = replace(crossing, group_id=f"road:{canonical.id}",
                               group_name=canonical.road or canonical.name, group_size=len(members))
        result.append(crossing)
    return result
