from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from math import cos, hypot, radians

import aiohttp

from gateahead.clusters import cluster_crossings, gate_label
from gateahead.clock import INDIA
from gateahead.config import Settings
from gateahead.crossing_details import CrossingDetails
from gateahead.erail import TrainRows, known_train_names, station_rows, train_rows
from gateahead.geo import km, stitch_rail
from gateahead.live import LiveLook, cached_live, fetch_live, position_from_lts
from gateahead.models import Crossing, Halt, PinCheck, SavedRoute, Station, TrainPosition, User
from gateahead.osm import OsmAround, OsmClient
from gateahead.gatecall import RailTopology, crossing_call as _crossing_eta
from gateahead.gatecall import passed_ago
from gateahead.gatecall import awaiting_report, standing
from gateahead.predict import JUST_PASSED_MIN, REPORT_FRESH_MIN, closes_in
from gateahead.scene import build_scene
from gateahead.store import Store, new_crossing_id
from gateahead.snapshots import SnapshotCache
from gateahead.runs import RunRequest, requests_for
from gateahead.timetable import (
    BEND_HALF_KM,
    classify_passage,
    fetch_timetable,
    list_status,
    locate_route,
    passage_delta,
    route_for_start,
    scheduled_position,
    waiting_position,
)
from gateahead.verify import check_pin

_FETCH = 16
_DISCOVER_BATCH = 10
_DISCOVERY_RADIUS_KM = 150
_ROUTE_FETCH = 4
_NEAR_KM = 2.0
_SAME_GATE_M = 40.0
_TRACKING_BUDGET = 84.0
_log = logging.getLogger(__name__)


@dataclass
class PositionBatch:
    numbers: list[str]
    positions: list[TrainPosition]
    routes: dict[str, SavedRoute]
    coverage: dict
    service_updates: list[dict] = field(default_factory=list)

    def __iter__(self):
        return iter((self.numbers, self.positions, self.routes))


class TrainDiscovery(list[str]):
    def __init__(self, numbers=(), *, unavailable: int = 0):
        super().__init__(numbers)
        self.unavailable = unavailable


def merge_saved_crossings(
    osm: list[Crossing], saved: list[Crossing], excluded_ids: set[str] | None = None,
    excluded_points: set[tuple[float, float]] | None = None,
) -> list[Crossing]:
    merged = list(osm)
    known = {crossing.id for crossing in osm}
    for extra in saved:
        if extra.source != "user" and excluded_ids and (extra.id in excluded_ids or extra.osm_id in excluded_ids):
            continue
        if extra.source == "irgeoportal" and excluded_points and any(
            abs(extra.lat - lat) <= 0.000001 and abs(extra.lon - lon) <= 0.000001 for lat, lon in excluded_points
        ):
            continue
        if extra.id in known:
            continue
        if extra.source != "user" and _same_gate(extra, osm):
            continue
        merged.append(extra)
        known.add(extra.id)
    return merged


def _same_gate(extra: Crossing, osm: list[Crossing]) -> bool:
    for crossing in osm:
        if km(extra.coords, crossing.coords) * 1000 <= _SAME_GATE_M:
            return True
    return False


class GateAhead:
    def __init__(self, settings: Settings, store: Store, session: aiohttp.ClientSession):
        self.settings = settings
        self.store = store
        self.session = session
        self.osm = OsmClient(session)
        self.snapshots = SnapshotCache(store.path)
        self.crossing_details = CrossingDetails(store.path.parent)

    async def map_around(self, lat: float, lon: float) -> OsmAround:
        result = await self.osm.around(lat, lon, self.settings.nearby_radius_m)
        if not result.error and not result.map_warning and not result.cached:
            for delay in (0.025, 0.075, None):
                try:
                    self._remember_fresh_map(result)
                    break
                except sqlite3.OperationalError as exc:
                    error_code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                    if error_code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    if delay is None:
                        _log.warning("SQLite was busy while recording current map evidence")
                    else:
                        await asyncio.sleep(delay)
        saved = self.store.crossings_near(lat, lon, self.settings.nearby_radius_m)
        if result.error:
            if not saved:
                return result
            return OsmAround(saved, [], [], False, map_warning=result.error, cached=True)
        crossings = merge_saved_crossings(result.crossings, saved, result.excluded_crossing_ids,
                                           result.excluded_crossing_points)
        stations = list(result.stations)
        if stations:
            tagged = []
            for crossing in crossings:
                nearest = min(stations, key=lambda s: km(crossing.coords, s.coords))
                tagged.append(
                    replace(crossing, nearest_station=nearest.code, area=nearest.name)
                )
            crossings = tagged
        group_names = {}
        for crossing in sorted(crossings, key=lambda c: c.id):
            if crossing.group_id:
                group_names.setdefault(crossing.group_id, gate_label(crossing))
        crossings = [replace(c, group_name=group_names[c.group_id]) if c.group_id else c for c in crossings]
        return replace(result, crossings=crossings, stations=stations)

    def _remember_fresh_map(self, result: OsmAround) -> None:
        if result.crossings or result.excluded_crossing_ids or result.excluded_crossing_points:
            self.store.reconcile_crossing_access(
                {c.id for c in result.crossings},
                result.excluded_crossing_ids,
                public_points={c.coords for c in result.crossings},
                excluded_points=result.excluded_crossing_points,
            )
        _remember_osm(self.store, result.stations)

    def set_location(self, user_id: int, lat: float, lon: float) -> User:
        return self.store.set_location(user_id, lat, lon)

    async def nearby(self, lat: float, lon: float) -> tuple[list[tuple[Crossing, float]], str | None]:
        result = await self.map_around(lat, lon)
        return crossing_distances(result, lat, lon), result.error or result.map_warning

    async def mark_gate(self, user_id: int, lat: float, lon: float) -> PinCheck:
        mapped = await self.map_around(lat, lon)
        if mapped.error or mapped.map_warning:
            return PinCheck("reject", mapped.error or mapped.map_warning or "map_unavailable", None)
        result = check_pin(
            lat,
            lon,
            crossings=mapped.crossings,
            rails=mapped.rails,
            has_highway=mapped.has_highway,
            rail_max_m=self.settings.rail_max_m,
            duplicate_m=self.settings.duplicate_m,
        )
        if result.verdict == "reject":
            return result
        if result.verdict == "existing" and result.match:
            return result
        nearest = mapped.stations[0].code if mapped.stations else ""
        crossing = Crossing(
            id=new_crossing_id(),
            name=f"Crossing near {nearest or 'track'}",
            lat=lat,
            lon=lon,
            source="user",
            status="pending",
            nearest_station=nearest,
        )
        self.store.save_crossing(crossing, added_by=user_id)
        return PinCheck(result.verdict, result.reason, result.rail_m, crossing)

    async def subscribe(self, user_id: int) -> User:
        user = self.store.get_user(user_id)
        if user.lat is None or user.lon is None:
            raise ValueError("Share your location first with /setlocation")
        nearby, error = await self.nearby(user.lat, user.lon)
        if error:
            raise ValueError(error)
        return self.store.set_subscribed(user_id, True, [c.id for c, _d in nearby])

    def unsubscribe(self, user_id: int) -> User:
        return self.store.set_subscribed(user_id, False, [])

    async def scene_here(self, lat: float, lon: float, crossing_id: str) -> dict:
        deadline = asyncio.get_running_loop().time() + _TRACKING_BUDGET
        mapped = await self.map_around(lat, lon)
        if mapped.error:
            return {"available": False, "error": mapped.error, "rail": [], "trains": [], "passed": []}
        canonical_id = self.store.canonical_crossing_id(crossing_id)
        crossing = next((item for item in mapped.crossings if item.id == canonical_id), None)
        if crossing is None:
            return {"available": False, "error": mapped.map_warning or "crossing_not_found",
                    "rail": [], "trains": [], "passed": [], **mapped.freshness()}
        batch = await self._positions(mapped.stations, lat, lon, deadline=deadline)
        _numbers, positions, routes = batch
        stations = known_points(mapped.stations, self.store.halts_near(lat, lon, 80, 500))
        stops_by_run = {}
        for position in positions:
            route = route_for_start(routes.get(position.train_number), position.start_date)
            if route is not None:
                stops_by_run[position.run_id] = route.stops
        body = build_scene(
            crossing,
            mapped.rails,
            positions,
            stations,
            self.settings,
            stops_by_run,
        )
        if not body.get("available") and mapped.map_warning:
            body["error"] = mapped.map_warning
        if crossing_id != canonical_id:
            body["legacy_alias_resolved"] = True
        body.update(mapped.freshness())
        body["coverage"] = _coverage(batch, mapped)
        body["service_updates"] = getattr(batch, "service_updates", [])
        body["crossing_access"] = self.crossing_details.for_crossing(crossing)
        return body

    async def _positions(
        self, stations: list[Station], lat: float, lon: float, *, deadline: float | None = None,
    ) -> PositionBatch:
        loop = asyncio.get_running_loop()
        remaining = _TRACKING_BUDGET if deadline is None else max(0, deadline - loop.time())
        deadline = loop.time() + remaining
        now = datetime.now(INDIA)
        regional = self.store.halts_near(lat, lon, _DISCOVERY_RADIUS_KM, 800, "station")
        discovery_stations = _discovery_stations(stations, regional, lat, lon)
        timed_out = False
        try:
            async with asyncio.timeout(min(20, remaining * 0.4)):
                numbers = await self._discover_trains(discovery_stations)
        except TimeoutError:
            timed_out = True
            numbers = TrainDiscovery(unavailable=1)
        unavailable_discovery = getattr(numbers, "unavailable", 0)
        # Pad the coarse route-leg lookup for bends between published points.
        # Crossing placement still checks the nearby rail and the full route.
        nearby_routes = self.store.route_numbers_near(lat, lon, self.settings.nearby_radius_m / 1000 + 25, None)
        numbers = list(dict.fromkeys(nearby_routes + numbers))
        self.store.remember_train_candidates(numbers)
        cached = self._cached_routes(numbers)
        self.store.remember_train_names(known_train_names(numbers))
        requests: dict[str, RunRequest] = {}
        hot, cold = [], []
        looks = {}
        for number in numbers:
            route = cached.get(number)
            plan = requests_for(number, route, lat, lon, now, self.settings.max_delay_hours)
            if not plan:
                known = cached_live(number, getattr(self, "snapshots", None))
                if known is not None and known.moving:
                    plan = [RunRequest(number, date.fromisoformat(known.start_date) if known.start_date else None)]
            for request in plan:
                requests[request.key] = request
                look = cached_live(number, getattr(self, "snapshots", None), request.start)
                if request.cold and look is None:
                    cold.append(request.key)
                else:
                    hot.append(request.key)
                    if look is not None:
                        looks[request.key] = replace(look, start_date=request.start.isoformat()) if request.start else look
        semaphore = asyncio.Semaphore(_FETCH)

        async def one(key: str) -> tuple[str, LiveLook]:
            request = requests[key]
            async with semaphore:
                try:
                    look = await fetch_live(self.session, request.number, self.snapshots, request.start) if request.start else await fetch_live(self.session, request.number, self.snapshots)
                    if request.start:
                        look = replace(look, start_date=request.start.isoformat())
                    self.store.record_provider("railyatri:live", look.operating_status != "unknown" and (look.moving is not None or look.finished or bool(look.notice)),
                                               "journey_report_unavailable" if look.operating_status == "unknown" else "")
                    return key, look
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    _log.warning("Live report unavailable for %s", key, exc_info=True)
                    self.store.record_provider("railyatri:live", False, "network_or_invalid_response")
                    return key, LiveLook(None, "", False, operating_status="unknown")

        pending = [number for number in hot if number not in looks]
        check_times = self.store.live_check_times(pending)

        def priority(number: str) -> tuple[float, float]:
            delta = requests[number].delta
            return check_times.get(number, 0), abs(delta) if delta is not None else float("inf")

        pending.sort(key=priority)
        selected = pending[:self.settings.live_check_budget]
        if selected:
            tasks = [asyncio.create_task(one(number)) for number in selected]
            try:
                done, waiting = await asyncio.wait(tasks, timeout=max(0, deadline - loop.time() - 2))
                timed_out |= bool(waiting)
                looks.update(task.result() for task in tasks if task in done)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        completed = [key for key in selected if key in looks]
        self.store.record_live_checks(completed)
        wanted = [
            number
            for number in hot
            if number in looks and (looks[number].moving is not None or not looks[number].finished)
            and looks[number].operating_status == "running"
        ]
        routes = {requests[key].number: cached[requests[key].number] for key in cold}
        wanted_numbers = list(dict.fromkeys(requests[key].number for key in wanted))
        try:
            async with asyncio.timeout_at(deadline):
                routes.update(await self._routes(wanted_numbers))
        except TimeoutError:
            timed_out = True
            routes.update(self._cached_routes(wanted_numbers))
        now = datetime.now(INDIA)
        positions = positions_from_looks(wanted, looks, routes, now)
        for key in cold:
            request = requests[key]
            route = routes.get(request.number)
            if route is None:
                continue
            placed = scheduled_position(request.number, route, lat, lon, now, request.start)
            if placed is not None:
                positions.append(placed)
        # Live names also help future timetable-only estimates after a restart.
        self.store.remember_train_names({position.train_number: position.name for position in positions if position.name.strip()})
        names = self.store.train_names(numbers)
        positions = [name_position(position, names.get(position.train_number, ""),
                                   route_for_start(routes.get(position.train_number), position.start_date))
                     for position in positions]
        updates = [{"number": requests[key].number, "start_date": look.start_date,
                    "run_id": f"{requests[key].number}:{look.start_date}" if look.start_date else key,
                    "status": look.operating_status, "notice": look.notice} for key, look in looks.items()
                   if look.operating_status in {"cancelled", "diverted", "partially_cancelled"}]
        missing = sum(number not in cached and number not in routes for number in numbers)
        unfinished = len(pending) - len(completed)
        unavailable = sum(look.moving is None and not look.finished and
                          look.operating_status in {"unknown", "running"} for look in looks.values())
        stale = sum(position.observed and
                    (not position.report_age_known or position.age_minutes >= REPORT_FRESH_MIN) for position in positions)
        incomplete = timed_out or unavailable_discovery or missing or unfinished or unavailable or stale
        return PositionBatch(numbers, positions, routes, {
            "status": "partial" if incomplete else "complete",
            "national_coverage_verified": False,
            "candidates": len(numbers),
            "run_candidates": len(requests),
            "live_candidates": len(hot),
            "live_checked": len(looks),
            "live_reports": sum(look.moving is not None for look in looks.values()),
            "unavailable_live": unavailable,
            "stale_live": stale,
            "unavailable_discovery": unavailable_discovery,
            "pending_live": unfinished,
            "missing_or_stale_routes": missing,
            "scheduled_candidates": len(cold),
            "limited": bool(unfinished),
            "deadline_exhausted": timed_out,
        }, updates)

    async def predict_here(
        self, lat: float, lon: float, *, budget_seconds: float = _TRACKING_BUDGET,
        alert_gate_ids: frozenset[str] = frozenset(),
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + min(_TRACKING_BUDGET, budget_seconds)
        mapped = await self.map_around(lat, lon)
        if mapped.error:
            return {"error": mapped.error, "crossings": 0, "stations": [], "checked": 0, "running": 0, "approaching": [], "passed": [], "sightings": []}
        crossings, stations = mapped.crossings, mapped.stations
        clusters = cluster_crossings(crossings)
        focus = {
            cluster.id
            for cluster in clusters
            if km((lat, lon), (cluster.lat, cluster.lon)) <= _NEAR_KM
        }
        if not focus:
            nearest = sorted(clusters, key=lambda cluster: km((lat, lon), (cluster.lat, cluster.lon)))
            focus = {cluster.id for cluster in nearest[:3]}
        batch = await self._positions(stations, lat, lon, deadline=deadline)
        numbers, positions, routes = batch
        approaching: list[dict] = []
        gate_predictions: list[dict] = []
        just_passed: list[dict] = []
        awaiting: list[dict] = []
        sightings: list[dict] = []
        local_codes = {s.code.upper() for s in stations}
        station_xy = known_points(stations, self.store.halts_near(lat, lon, 80, 500))
        rails = mapped.rails
        topology = RailTopology(rails)
        chains = {
            crossing.id: stitch_rail(rails, crossing.coords, BEND_HALF_KM) if rails else None
            for cluster in clusters
            for crossing in cluster.crossings
        }
        for position in positions:
            route = route_for_start(routes.get(position.train_number), position.start_date)
            stops = route.stops if route else []
            best: tuple[float, object, str | None] | None = None
            hits = []
            gone = []
            waiting_ids = []
            for cluster in clusters:
                etas = []
                gate_ids = []
                for crossing in cluster.crossings:
                    call = _crossing_eta(
                        position, crossing, rails, station_xy, self.settings, stops, chains.get(crossing.id), topology
                    )
                    if call.awaiting_update:
                        waiting_ids.append(crossing.id)
                        continue
                    ago = passed_ago(call)
                    if ago is not None and cluster.id in focus and ago <= JUST_PASSED_MIN:
                        gone.append((crossing, ago))
                    status = list_status(call.eta, call.slack, self.settings, call.source == "timetable")
                    if status is None or call.eta is None:
                        continue
                    etas.append((call.eta, call.bracket, status, call.source))
                    gate_ids.append(crossing.id)
                    if crossing.id in alert_gate_ids or _cluster_is_shown(status, cluster.id in focus):
                        gate_predictions.append({
                            "id": crossing.id, "train": position.train_number, "name": position.name,
                            "run_id": position.run_id, "start_date": position.start_date,
                            "eta": round(call.eta, 1), "closes_in": round(closes_in(call.eta, self.settings), 1),
                            "status": status, "basis": call.source, "age_min": round(position.age_minutes, 1),
                            "at_station": standing(position), "observed": position.observed,
                            "timing_confident": call.slack <= 3 and position.report_age_known,
                            "timing_as_of": position.as_of.timestamp(),
                            "alert_closes_at": position.as_of.timestamp() + closes_in(call.eta, self.settings) * 60,
                            "alert_fresh_until": position.as_of.timestamp() + (REPORT_FRESH_MIN - position.age_minutes) * 60,
                        })
                if not etas:
                    continue
                soon, bracket, status, basis = min(etas, key=lambda item: item[0])
                if not _cluster_is_shown(status, cluster.id in focus):
                    continue
                hits.append((cluster, soon, bracket, status, basis, gate_ids))
                if best is None or soon < best[0]:
                    best = (soon, cluster, bracket)
            if waiting_ids:
                awaiting.append(awaiting_report(position, waiting_ids))
            passed_local = False if hits or waiting_ids else bool(gone) or _passed_local(position, lat, lon, local_codes)
            if hits or _on_this_stretch(position, lat, lon, local_codes):
                sightings.append(_sighting(position, best[0] if best else None, passed_local))
            for cluster, soon, bracket, status, basis, gate_ids in hits:
                row = {
                    "train": position.train_number,
                    "name": position.name,
                    "cluster": cluster.name,
                    "count": len(gate_ids),
                    "eta": round(soon, 1),
                    "closes_in": round(closes_in(soon, self.settings), 1),
                    "status": status,
                    "basis": basis,
                    "observed": position.observed,
                    "age_min": round(position.age_minutes, 1),
                    "at_station": standing(position),
                    "current": position.current_name,
                    "from_you_km": round(km((lat, lon), (cluster.lat, cluster.lon)), 2),
                    "lat": cluster.lat,
                    "lon": cluster.lon,
                    "ids": gate_ids,
                }
                if bracket:
                    row["bracket"] = bracket
                row.update(_run_fields(position))
                approaching.append(row)
            for crossing, ago in gone:
                just_passed.append(
                    {
                        "train": position.train_number,
                        "run_id": position.run_id, "start_date": position.start_date,
                        "name": position.name,
                        "cluster": gate_label(crossing),
                        "ago": round(ago, 1),
                        "from_you_km": round(km((lat, lon), crossing.coords), 2),
                        "lat": crossing.lat,
                        "lon": crossing.lon,
                        "ids": [crossing.id],
                    }
                )
        approaching.sort(key=lambda row: row["eta"])
        just_passed.sort(key=lambda row: row["ago"])
        return {
            "generated_at": datetime.now(INDIA).isoformat(),
            "refresh_after_seconds": 120,
            "crossings": len(crossings),
            "stations": [s.code for s in stations[:4]],
            "checked": getattr(batch, "coverage", {}).get("live_checked", len(numbers)),
            "running": len(positions),
            "gate_predictions": sorted(gate_predictions, key=lambda row: row["eta"]),
            **({"unmapped_gate_count": len(alert_gate_ids - {crossing.id for crossing in crossings})}
               if alert_gate_ids else {}),
            "approaching": approaching,
            "passed": just_passed,
            "awaiting_update": awaiting,
            "sightings": sightings,
            "coverage": _coverage(batch, mapped),
            "service_updates": getattr(batch, "service_updates", []),
            **mapped.freshness(),
        }

    async def _discover_trains(self, stations: list[Station]) -> list[str]:
        return await _discover_trains_from(self.session, stations)

    def _cached_routes(self, numbers: list[str]) -> dict[str, SavedRoute]:
        found: dict[str, SavedRoute] = {}
        for number in numbers:
            if not number or number in found:
                continue
            cached = self.store.load_route(number, self.settings.route_max_age_hours)
            if cached is not None:
                found[number] = locate_route(cached, self.store.halt_coords)
        return found

    async def _routes(self, numbers: list[str]) -> dict[str, SavedRoute]:
        self.store.remember_train_candidates(numbers)
        found = self._cached_routes(numbers)
        missing: list[str] = []
        seen = set(found)
        for number in numbers:
            if number and number not in seen and self.store.route_refresh_allowed(number):
                seen.add(number)
                missing.append(number)
        semaphore = asyncio.Semaphore(_ROUTE_FETCH)

        async def one(number: str) -> tuple[str, SavedRoute | None]:
            async with semaphore:
                try:
                    route = locate_route(await fetch_timetable(self.session, number), self.store.halt_coords)
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    _log.warning("Timetable unavailable for %s", number, exc_info=True)
                    self.store.record_route_failure(number)
                    return number, None
                if len(route.stops) < 2:
                    self.store.record_route_failure(number)
                    return number, None
                self.store.save_route(number, route.stops, route.run_days, variants=route.variants)
                return number, route

        for number, route in await asyncio.gather(*[one(number) for number in missing]):
            if route is not None:
                found[number] = route
        return found

def crossing_distances(mapped: OsmAround, lat: float, lon: float) -> list[tuple[Crossing, float]]:
    return sorted(((crossing, km((lat, lon), crossing.coords)) for crossing in mapped.crossings),
                  key=lambda item: item[1])


def _coverage(batch: PositionBatch, mapped: OsmAround) -> dict:
    coverage = dict(getattr(batch, "coverage", {"status": "unknown"}))
    if mapped.map_warning:
        coverage.update(status="partial", map_warning=mapped.map_warning)
    return coverage


def name_position(position: TrainPosition, saved_name: str, route: SavedRoute | None) -> TrainPosition:
    if position.name.strip() and position.name.strip() != position.train_number:
        return position
    name = saved_name.strip()
    if name == position.train_number:
        name = ""
    if not name:
        origin, destination = position.origin_name, position.dest_name
        if route is not None and len(route.stops) >= 2:
            origin = origin or route.stops[0].name
            destination = destination or route.stops[-1].name
        if origin and destination:
            name = f"{origin} → {destination}"
    return replace(position, name=name) if name else position


def known_points(
    stations: list[Station], extra: list[tuple[Halt, float]]
) -> dict[str, tuple[float, float]]:
    points = {station.code.upper(): station.coords for station in stations if station.code}
    for halt, _distance in extra:
        points.setdefault(halt.code.upper(), halt.coords)
    return points


def _remember_osm(store: Store, stations: list[Station]) -> None:
    from gateahead.halts import clean_code

    rows: list[Halt] = []
    for station in stations:
        code = clean_code(station.code)
        if not code:
            continue
        rows.append(Halt(code, station.name, station.lat, station.lon, station.kind or "station", "", "osm"))
    if rows:
        store.remember_map_halts(rows)


def _cluster_is_shown(status: str, in_focus: bool) -> bool:
    if in_focus:
        return True
    return status in {"closed", "closing_soon", "approaching"}


def resolve_position(
    number: str, look: LiveLook, route: SavedRoute | None, now: datetime
) -> TrainPosition | None:
    if look.operating_status != "running":
        return None
    if look.moving:
        position = position_from_lts(number, look.moving, now, fetched_at=look.fetched_at)
        return replace(position, start_date=look.start_date or position.start_date) if position is not None else None
    if look.finished or route is None:
        return None
    return waiting_position(number, route, now, date.fromisoformat(look.start_date) if look.start_date else None)


def positions_from_looks(
    numbers: list[str],
    looks: dict[str, LiveLook],
    routes: dict[str, SavedRoute],
    now: datetime,
) -> list[TrainPosition]:
    found: list[TrainPosition] = []
    for number in numbers:
        service_number = number.split(":", 1)[0]
        position = resolve_position(
            service_number,
            looks.get(number, LiveLook(None, "", False)),
            routes.get(service_number),
            now,
        )
        if position is not None:
            found.append(position)
    return found


def _station_codes(stations: list[Station]) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for station in stations:
        code = station.code.strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def _major_station(station: Station) -> bool:
    name = f" {station.name.lower().replace('.', ' ')} "
    return any(token in name for token in (" junction ", " central ", " terminus ", " terminal ", " jn "))


def _discovery_stations(local: list[Station], regional: list[tuple[Halt, float]], lat: float, lon: float) -> list[Station]:
    by_code = {station.code.upper(): station for station in local if station.code}
    for halt, _distance in regional:
        by_code.setdefault(halt.code.upper(), Station(halt.code, halt.name, halt.lat, halt.lon, halt.kind))
    return sorted(by_code.values(), key=lambda station: km((lat, lon), station.coords))


def _board_stations(stations: list[Station]) -> list[Station]:
    """A local board plus regional anchors on either side, at most three requests."""
    regular = list({station.code.upper(): station for station in stations if station.code and station.kind != "halt"}.values())
    if not regular:
        return []
    home = regular[0]
    far = [station for station in regular[1:] if km(home.coords, station.coords) >= 10]
    if not far:
        return regular[:2]
    # Use the wider geographic axis so this also works on east/west stretches.
    north_span = max(station.lat for station in regular) - min(station.lat for station in regular)
    east_span = max(station.lon for station in regular) - min(station.lon for station in regular)
    coordinate = (lambda station: station.lat) if north_span >= east_span else (lambda station: station.lon)
    regional_hubs = [station for station in far if _major_station(station)]
    anchor = min(regional_hubs, key=lambda station: km(home.coords, station.coords)) if regional_hubs else None
    chosen = [home]
    for side in (-1, 1):
        pool = [station for station in far if (coordinate(station) - coordinate(home)) * side > 0]
        majors = [station for station in pool if _major_station(station)]
        if majors:
            chosen.append(min(majors, key=lambda station: km(home.coords, station.coords)))
        elif pool:
            if anchor is not None:
                # Avoid choosing a distant inland branch just because it falls
                # within the regional circle. This is candidate discovery;
                # each train's route still has to reach the gate.
                scale = cos(radians(home.lat))
                ax, ay = (anchor.lon - home.lon) * scale, anchor.lat - home.lat

                def offset(station: Station) -> float:
                    dx, dy = (station.lon - home.lon) * scale, station.lat - home.lat
                    return abs(ax * dy - ay * dx) / (hypot(ax, ay) * hypot(dx, dy))

                aligned = [station for station in pool if offset(station) <= 0.35]
                pool = aligned or [min(pool, key=offset)]
            chosen.append(max(pool, key=lambda station: km(home.coords, station.coords)))
    return chosen


def _pair_plan(stations: list[Station]) -> list[tuple[str, str]]:
    codes = _station_codes(stations)
    if len(codes) < 2:
        return []
    by_code = {station.code.strip().upper(): station for station in stations}
    majors = [by_code[code] for code in codes if _major_station(by_code[code])]
    pool = majors if len(majors) >= 2 else [by_code[code] for code in codes]
    by_lat = sorted(pool, key=lambda station: station.lat)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(origin: str, dest: str) -> None:
        key = (origin.strip().upper(), dest.strip().upper())
        if not key[0] or not key[1] or key[0] == key[1] or key in seen:
            return
        seen.add(key)
        pairs.append(key)

    add(by_lat[0].code, by_lat[-1].code)
    add(by_lat[-1].code, by_lat[0].code)
    # Junctions can lie on the same side of the gate. Also search across the
    # whole region rather than requiring the train to stop at a local halt.
    whole = sorted(by_code.values(), key=lambda station: station.lat)
    add(whole[0].code, whole[-1].code)
    add(whole[-1].code, whole[0].code)
    for other in codes[1:5]:
        add(codes[0], other)
        add(other, codes[0])
    return pairs


def _clock_rank(dep: int | None, now_min: int) -> int:
    if dep is None:
        return 10_000
    day = 24 * 60
    since = (now_min - dep) % day
    if since <= 8 * 60:
        return since
    until = (dep - now_min) % day
    if until <= 6 * 60:
        return 500 + until
    return 5_000 + min(since, until)


def _order_due(rows: list[tuple[str, int | None]], now_min: int) -> list[str]:
    best: dict[str, tuple[int, int]] = {}
    for index, (number, dep) in enumerate(rows):
        rank = _clock_rank(dep, now_min)
        held = best.get(number)
        if held is None or rank < held[0]:
            best[number] = (rank, index if held is None else held[1])
    return [number for number, _rank in sorted(best.items(), key=lambda item: item[1])]


async def _discover_trains_from(
    session: aiohttp.ClientSession,
    stations: list[Station],
    now: datetime | None = None,
) -> list[str]:
    found: list[tuple[str, int | None]] = []
    unavailable = 0
    # A service may stop at only one of the nearby stations. Between-station
    # queries alone omit it (for example 16512 at PAY, skipping ELM and TKQ).
    boards = _board_stations(stations)
    pairs = _pair_plan(stations)
    lookups = [(True, station.code, station.name) for station in boards] + [(False, origin, dest) for origin, dest in pairs]

    async def lookup(board: bool, origin: str, dest: str):
        try:
            return await (station_rows(session, origin, dest) if board else train_rows(session, origin, dest))
        except (aiohttp.ClientError, TimeoutError, ValueError):
            _log.warning("Train discovery unavailable for %s", origin, exc_info=True)
            return TrainRows(unavailable=True)

    for start in range(0, len(lookups), _DISCOVER_BATCH):
        chunk = lookups[start : start + _DISCOVER_BATCH]
        lists = await asyncio.gather(*[lookup(board, origin, dest) for board, origin, dest in chunk])
        for rows in lists:
            unavailable += bool(getattr(rows, "unavailable", False))
            found.extend(rows)
    moment = now or datetime.now(INDIA)
    ordered = _order_due(found, moment.hour * 60 + moment.minute)
    return TrainDiscovery(ordered, unavailable=unavailable)


def _train_is_here(position: TrainPosition, lat: float, lon: float, local_codes: set[str]) -> bool:
    last = (position.last_station or "").upper()
    if last and last in local_codes:
        return True
    if abs(position.lat) > 1 and abs(position.lon) > 1:
        return km((lat, lon), position.coords) <= 80
    return False


def _passed_local(position: TrainPosition, lat: float, lon: float, local_codes: set[str]) -> bool:
    if _train_is_here(position, lat, lon, local_codes):
        return False
    return any(code in local_codes and stamp < position.as_of for code, stamp in position.station_etas)


def _on_this_stretch(position: TrainPosition, lat: float, lon: float, local_codes: set[str]) -> bool:
    here = {position.last_station, position.next_station}
    here = {c.upper() for c in here if c}
    if local_codes & here:
        return True
    if abs(position.lat) > 1 and abs(position.lon) > 1:
        return km((lat, lon), position.coords) <= 80
    return False


def _sighting(position: TrainPosition, eta: float | None, passed_local: bool) -> dict:
    row = {
        "number": position.train_number,
        "name": position.name,
        "current": position.current_name,
        "delay": position.delay_minutes,
        "age": position.age_minutes,
        "eta": eta,
        "passed_local": passed_local,
        "at_station": position.at_station,
        "observed": position.observed,
    }
    row.update(_run_fields(position))
    return row


def _run_fields(position: TrainPosition) -> dict:
    fields: dict = {"delay": position.delay_minutes, "run_id": position.run_id, "start_date": position.start_date}
    if position.origin_name:
        fields["origin"] = position.origin_name
    if position.dest_name:
        fields["dest"] = position.dest_name
    if position.next_name:
        fields["next"] = position.next_name
    if position.next_in:
        fields["next_in"] = position.next_in
    if position.departed_at:
        fields["departed_at"] = position.departed_at
    if position.scheduled_at:
        fields["scheduled_at"] = position.scheduled_at
    if position.platform:
        fields["platform"] = position.platform
    if position.left_km is not None:
        fields["left_km"] = position.left_km
    if position.run:
        fields["run"] = position.run
    return fields
