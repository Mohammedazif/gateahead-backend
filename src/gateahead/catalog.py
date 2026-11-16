"""Resumable station-board discovery and timetable refresh, outside prediction requests."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import time
import uuid
from pathlib import Path

import aiohttp

from gateahead.config import load_settings
from gateahead.erail import known_train_names, station_rows
from gateahead.halts import ensure_catalog, parse_station_catalog
from gateahead.store import Store
from gateahead.timetable import fetch_timetable, locate_stops


async def sync_catalog(
    session: aiohttp.ClientSession,
    store: Store,
    *,
    station_limit: int = 25,
    route_limit: int = 100,
    station_refresh_hours: float = 168,
    route_refresh_hours: float = 24,
    request_interval: float = 2,
) -> dict:
    """Keep progress in SQLite; a failed fetch never replaces a saved route."""
    owner = uuid.uuid4().hex
    if not store.claim_catalog(owner):
        return {"status": "busy"}
    result = {"status": "ok", "stations_checked": 0, "station_errors": 0,
              "routes_refreshed": 0, "route_errors": 0}
    last_request = 0.0

    async def pace() -> None:
        nonlocal last_request
        pause = request_interval - (time.monotonic() - last_request)
        if pause > 0:
            await asyncio.sleep(pause)
        if not store.claim_catalog(owner):
            raise RuntimeError("catalog_lease_lost")
        last_request = time.monotonic()

    try:
        failures = 0
        for station in store.due_stations(station_limit, station_refresh_hours):
            await pace()
            rows = await station_rows(session, station.code, station.name, refresh=True)
            # Empty HTML is indistinguishable from a changed provider layout.
            # Keep this station due, with backoff, rather than calling it covered.
            store.record_station_scan(station.code, bool(rows))
            if rows:
                numbers = [number for number, _clock in rows]
                store.remember_train_candidates(numbers, "station-board")
                store.remember_train_names(known_train_names(numbers))
                result["stations_checked"] += 1
                failures = 0
            else:
                result["station_errors"] += 1
                failures += 1
            if failures >= 3:
                break
        failures = 0
        for number in store.due_routes(route_limit, route_refresh_hours):
            await pace()
            stops, run_days = await fetch_timetable(session, number)
            if len(stops) >= 2:
                stops = locate_stops(stops, store.halt_coords([s.code for s in stops if s.lat is None or s.lon is None]))
                store.save_route(number, stops, run_days)
                result["routes_refreshed"] += 1
                failures = 0
            else:
                store.record_route_failure(number)
                result["route_errors"] += 1
                failures += 1
            if failures >= 3:
                break
        if result["station_errors"] or result["route_errors"]:
            result["status"] = "partial"
        store.meta_set("train_catalog_last_sync", json.dumps({**result, "at": time.time()}))
        return result
    finally:
        store.release_catalog(owner)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "sync", "audit"))
    parser.add_argument("--output", type=Path, help="CSV destination for a station coverage audit")
    parser.add_argument("--station-limit", type=int, default=25)
    parser.add_argument("--route-limit", type=int, default=100)
    parser.add_argument("--route-refresh-hours", type=float, default=24)
    parser.add_argument("--station-refresh-hours", type=float, default=168)
    parser.add_argument("--request-interval", type=float, default=2)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--trains", type=Path, help="Seed train numbers from a UTF-8 file, one five-digit number per line")
    parser.add_argument("--stations", type=Path, help="Import station GeoJSON or a station JSON array before discovery")
    args = parser.parse_args()
    if min(args.station_limit, args.route_limit, args.route_refresh_hours, args.station_refresh_hours) < 0:
        parser.error("limits and refresh hours must be nonnegative")
    if args.request_interval < 0.5 or args.interval_seconds < 1:
        parser.error("request interval must be at least 0.5s and watch interval at least 1s")
    settings = load_settings()
    store = Store(settings.db_path)
    if args.action == "audit":
        if not args.output:
            parser.error("audit requires --output")
        with args.output.open("w", encoding="utf-8", newline="") as output:
            fields = ["code", "name", "lat", "lon", "board_checked_at", "known_stopping_or_passing_trains", "fresh_routes"]
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(store.station_coverage(settings.route_max_age_hours))
        store._conn.close()
        return
    if args.trains:
        numbers = args.trains.read_text(encoding="utf-8").splitlines()
        numbers = [n.strip() for n in numbers if n.strip()]
        if any(not re.fullmatch(r"\d{5}", n) for n in numbers):
            parser.error("train file must contain one five-digit number per nonempty line")
        store.remember_train_candidates(numbers, "import")
    if args.stations:
        halts = parse_station_catalog(json.loads(args.stations.read_text(encoding="utf-8")))
        if not halts:
            parser.error("station file contains no valid stations")
        store.upsert_halts(halts)
    if args.action == "status":
        print(json.dumps(store.catalog_status(settings.route_max_age_hours)), flush=True)
        store._conn.close()
        return

    async def run() -> None:
        async with aiohttp.ClientSession() as session:
            if not store.halt_count():
                await ensure_catalog(session, store)
            while True:
                result = await sync_catalog(
                    session, store, station_limit=args.station_limit, route_limit=args.route_limit,
                    route_refresh_hours=args.route_refresh_hours, station_refresh_hours=args.station_refresh_hours,
                    request_interval=args.request_interval,
                )
                print(json.dumps({"sync": result, "catalog": store.catalog_status(settings.route_max_age_hours)}), flush=True)
                if not args.watch:
                    break
                await asyncio.sleep(args.interval_seconds)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        store._conn.close()


if __name__ == "__main__":
    main()
