from __future__ import annotations

import asyncio
from datetime import datetime

import aiohttp

from smartrail.clusters import cluster_crossings
from smartrail.config import Settings
from smartrail.erail import train_rows
from smartrail.geo import km, stitch_rail
from smartrail.live import LiveLook, fetch_live, position_from_lts
from smartrail.models import Crossing, Halt, PinCheck, SavedRoute, Station, TrainPosition, User
from smartrail.osm import OsmAround, OsmClient
from smartrail.predict import crossing_is_ahead, eta_minutes, route_eta
from smartrail.scene import build_scene
from smartrail.store import Store
from smartrail.timetable import (
    BEND_HALF_KM,
    Call,
    classify_passage,
    fetch_timetable,
    list_status,
    locate_stops,
    merge_call,
    scheduled_position,
    timetable_call,
    waiting_position,
)
from smartrail.verify import check_pin

_FETCH = 16
_DISCOVER_CAP = 30
_DISCOVER_BATCH = 10
_ROUTE_HOURS = 24 * 7
_ROUTE_FETCH = 4
_NEAR_KM = 2.0


class SmartRail:
    def __init__(self, settings: Settings, store: Store, session: aiohttp.ClientSession):
        self.settings = settings
        self.store = store
        self.session = session
        self.osm = OsmClient(session)

    async def map_around(self, lat: float, lon: float) -> OsmAround:
        result = await self.osm.around(lat, lon, self.settings.nearby_radius_m)
        if result.error:
            return result
        crossings = list(result.crossings)
        stations = list(result.stations)
        saved = self.store.public_crossings()
        known_ids = {c.id for c in crossings}
        for extra in saved:
            if extra.id not in known_ids and km((lat, lon), extra.coords) * 1000 <= self.settings.nearby_radius_m:
                crossings.append(extra)
        if stations:
            tagged = []
            for crossing in crossings:
                nearest = min(stations, key=lambda s: km(crossing.coords, s.coords))
                tagged.append(
                    Crossing(
                        id=crossing.id,
                        name=crossing.name,
                        lat=crossing.lat,
                        lon=crossing.lon,
                        source=crossing.source,
                        status=crossing.status,
                        nearest_station=nearest.code,
                        osm_id=crossing.osm_id,
                        road=crossing.road,
                        area=nearest.name,
                    )
                )
            crossings = tagged
        _remember_osm(self.store, stations)
        return OsmAround(crossings, stations, result.rails, result.has_highway)

    def set_location(self, user_id: int, lat: float, lon: float) -> User:
        return self.store.set_location(user_id, lat, lon)

    async def nearby(self, lat: float, lon: float) -> tuple[list[tuple[Crossing, float]], str | None]:
        result = await self.map_around(lat, lon)
        if result.error:
            return [], result.error
        items = [(c, km((lat, lon), c.coords)) for c in result.crossings]
        items.sort(key=lambda x: x[1])
        return items, None

    async def mark_gate(self, user_id: int, lat: float, lon: float) -> PinCheck:
        mapped = await self.map_around(lat, lon)
        if mapped.error:
            return PinCheck("reject", mapped.error, None)
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
            id=f"user:{user_id}:{int(datetime.now().timestamp())}",
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
        mapped = await self.map_around(lat, lon)
        if mapped.error:
            return {"available": False, "error": mapped.error, "rail": [], "trains": []}
        crossing = next((item for item in mapped.crossings if item.id == crossing_id), None)
        if crossing is None:
            return {"available": False, "error": "crossing_not_found", "rail": [], "trains": []}
        _numbers, positions, routes = await self._positions(mapped.stations, lat, lon)
        stations = known_points(mapped.stations, self.store.halts_near(lat, lon, 80, 500))
        return build_scene(
            crossing,
            mapped.rails,
            positions,
            stations,
            self.settings,
            {number: route.stops for number, route in routes.items()},
        )

    async def _positions(
        self, stations: list[Station], lat: float, lon: float
    ) -> tuple[list[str], list[TrainPosition], dict[str, SavedRoute]]:
        now = datetime.now()
        numbers = await self._discover_trains(stations)
        cached = self._cached_routes(numbers)
        hot: list[str] = []
        cold: list[str] = []
        for number in numbers:
            route = cached.get(number)
            if route is None:
                hot.append(number)
                continue
            kind = classify_passage(route.stops, route.run_days, lat, lon, now)
            if kind in {"hot", "unknown"}:
                hot.append(number)
            elif kind == "cold":
                cold.append(number)
        semaphore = asyncio.Semaphore(_FETCH)

        async def one(number: str) -> tuple[str, LiveLook]:
            async with semaphore:
                return number, await fetch_live(self.session, number)

        looks = dict(await asyncio.gather(*[one(number) for number in hot])) if hot else {}
        wanted = [
            number
            for number in hot
            if looks[number].moving is not None or not looks[number].finished
        ]
        routes = {number: cached[number] for number in cold if number in cached}
        routes.update(await self._routes(wanted))
        positions = positions_from_looks(wanted, looks, routes, now)
        for number in cold:
            route = routes.get(number)
            if route is None:
                continue
            placed = scheduled_position(number, route, lat, lon, now)
            if placed is not None:
                positions.append(placed)
        return numbers, positions, routes

    async def predict_here(self, lat: float, lon: float) -> dict:
        mapped = await self.map_around(lat, lon)
        if mapped.error:
            return {"error": mapped.error, "crossings": 0, "stations": [], "checked": 0, "running": 0, "approaching": [], "sightings": []}
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
        numbers, positions, routes = await self._positions(stations, lat, lon)
        approaching: list[dict] = []
        sightings: list[dict] = []
        local_codes = {s.code.upper() for s in stations}
        station_xy = known_points(stations, self.store.halts_near(lat, lon, 80, 500))
        rails = mapped.rails
        chains = {
            crossing.id: stitch_rail(rails, crossing.coords, BEND_HALF_KM) if rails else None
            for cluster in clusters
            for crossing in cluster.crossings
        }
        for position in positions:
            route = routes.get(position.train_number)
            stops = route.stops if route else []
            if position.age_minutes >= 5 and not position.upcoming_codes and not stops:
                sightings.append(_sighting(position, None, False))
                continue
            best: tuple[float, object, str | None] | None = None
            hits = []
            for cluster in clusters:
                etas = []
                for crossing in cluster.crossings:
                    call = _crossing_eta(
                        position, crossing, rails, station_xy, self.settings, stops, chains.get(crossing.id)
                    )
                    status = list_status(call.eta, call.slack, self.settings, call.source == "timetable")
                    if status is None or call.eta is None:
                        continue
                    etas.append((call.eta, call.bracket, status))
                if not etas:
                    continue
                soon, bracket, status = min(etas, key=lambda item: item[0])
                if not _cluster_is_shown(status, cluster.id in focus):
                    continue
                hits.append((cluster, soon, bracket, status))
                if best is None or soon < best[0]:
                    best = (soon, cluster, bracket)
            passed_local = False if hits else _passed_local(position, lat, lon, local_codes)
            if hits or _on_this_stretch(position, lat, lon, local_codes):
                sightings.append(_sighting(position, best[0] if best else None, passed_local))
            for cluster, soon, bracket, status in hits:
                row = {
                    "train": position.train_number,
                    "name": position.name,
                    "cluster": cluster.name,
                    "count": len(cluster.crossings),
                    "eta": round(soon, 1),
                    "status": status,
                    "at_station": position.at_station,
                    "current": position.current_name,
                    "from_you_km": round(km((lat, lon), (cluster.lat, cluster.lon)), 2),
                    "lat": cluster.lat,
                    "lon": cluster.lon,
                    "ids": [c.id for c in cluster.crossings],
                }
                if bracket:
                    row["bracket"] = bracket
                row.update(_run_fields(position))
                approaching.append(row)
        approaching.sort(key=lambda row: row["eta"])
        return {
            "crossings": len(crossings),
            "stations": [s.code for s in stations[:4]],
            "checked": len(numbers),
            "running": len(positions),
            "approaching": approaching,
            "sightings": sightings,
        }

    async def _discover_trains(self, stations: list[Station]) -> list[str]:
        return await _discover_trains_from(self.session, stations)

    def _cached_routes(self, numbers: list[str]) -> dict[str, SavedRoute]:
        found: dict[str, SavedRoute] = {}
        for number in numbers:
            if not number or number in found:
                continue
            cached = self.store.load_route(number, _ROUTE_HOURS)
            if cached is not None:
                found[number] = SavedRoute(self._pin_route(cached.stops), cached.run_days)
        return found

    async def _routes(self, numbers: list[str]) -> dict[str, SavedRoute]:
        found = self._cached_routes(numbers)
        missing: list[str] = []
        seen = set(found)
        for number in numbers:
            if number and number not in seen:
                seen.add(number)
                missing.append(number)
        semaphore = asyncio.Semaphore(_ROUTE_FETCH)

        async def one(number: str) -> tuple[str, SavedRoute | None]:
            async with semaphore:
                stops, run_days = await fetch_timetable(self.session, number)
                if len(stops) < 2:
                    return number, None
                self.store.save_route(number, stops, run_days)
                return number, SavedRoute(self._pin_route(stops), run_days)

        for number, route in await asyncio.gather(*[one(number) for number in missing]):
            if route is not None:
                found[number] = route
        return found

    def _pin_route(self, stops: list) -> list:
        missing = [stop.code for stop in stops if stop.lat is None or stop.lon is None]
        return locate_stops(stops, self.store.halt_coords(missing))


def known_points(
    stations: list[Station], extra: list[tuple[Halt, float]]
) -> dict[str, tuple[float, float]]:
    points = {station.code.upper(): station.coords for station in stations if station.code}
    for halt, _distance in extra:
        points.setdefault(halt.code.upper(), halt.coords)
    return points


def _remember_osm(store: Store, stations: list[Station]) -> None:
    from smartrail.halts import clean_code

    rows: list[Halt] = []
    for station in stations:
        code = clean_code(station.code)
        if not code:
            continue
        rows.append(Halt(code, station.name, station.lat, station.lon, station.kind or "station", "", "osm"))
    if rows:
        store.upsert_halts(rows)


def _cluster_is_shown(status: str, in_focus: bool) -> bool:
    if in_focus:
        return True
    return status in {"closed", "closing_soon", "approaching"}


def _crossing_eta(position, crossing, rails, stations, settings, stops=None, chain=None) -> Call:
    local_on = False
    local_eta = None
    local_bracket = None
    if chain is None and rails:
        chain = stitch_rail(rails, crossing.coords, BEND_HALF_KM)
    if chain is not None:
        on_route, eta, bracket = route_eta(position, crossing, chain, stations, settings)
        if on_route and eta is None:
            return Call(None, None, "rail", 0)
        if on_route:
            local_on = True
            local_eta = eta
            local_bracket = bracket
    if not local_on and crossing_is_ahead(position, crossing):
        chord = eta_minutes(position, crossing, settings)
        if chord is not None:
            local_on = True
            local_eta = chord
    timed = timetable_call(stops, position, crossing, chain) if stops else None
    return merge_call(local_on, local_eta, local_bracket, timed)


def resolve_position(
    number: str, look: LiveLook, route: SavedRoute | None, now: datetime
) -> TrainPosition | None:
    if look.moving:
        return position_from_lts(number, look.moving, now)
    if look.finished or route is None:
        return None
    return waiting_position(number, route, now)


def positions_from_looks(
    numbers: list[str],
    looks: dict[str, LiveLook],
    routes: dict[str, SavedRoute],
    now: datetime,
) -> list[TrainPosition]:
    found: list[TrainPosition] = []
    for number in numbers:
        position = resolve_position(
            number,
            looks.get(number, LiveLook(None, "", False)),
            routes.get(number),
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
    pairs = _pair_plan(stations)
    for start in range(0, len(pairs), _DISCOVER_BATCH):
        chunk = pairs[start : start + _DISCOVER_BATCH]
        lists = await asyncio.gather(*[train_rows(session, origin, dest) for origin, dest in chunk])
        for rows in lists:
            found.extend(rows)
    moment = now or datetime.now()
    ordered = _order_due(found, moment.hour * 60 + moment.minute)
    return ordered[:_DISCOVER_CAP]


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
    }
    row.update(_run_fields(position))
    return row


def _run_fields(position: TrainPosition) -> dict:
    fields: dict = {"delay": position.delay_minutes}
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
