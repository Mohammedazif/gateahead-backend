from __future__ import annotations

import asyncio
import json
import re
import time

import aiohttp

from gateahead.models import Halt
from gateahead.store import Store

CATALOG_URL = "https://raw.githubusercontent.com/datameet/railways/master/stations.json"
_CODE = re.compile(r"^[A-Z]{1,5}$")
_HALT = re.compile(r"\bhalt\b", re.I)
_HEADERS = {
    "User-Agent": "GateAhead/0.3 (https://github.com/Mohammedazif/gateahead-tele)",
    "Accept": "application/json",
}
_failed_at = 0.0


def clean_code(value: object) -> str:
    code = str(value or "").upper().split(";")[0].strip().replace(" ", "")
    if code.startswith("IR:"):
        code = code[3:]
    return code if _CODE.fullmatch(code) else ""


def halt_kind(name: str) -> str:
    return "halt" if _HALT.search(name or "") else "station"


def parse_station_catalog(payload: object) -> list[Halt]:
    if isinstance(payload, dict):
        features = payload.get("features") or []
    elif isinstance(payload, list):
        features = payload
    else:
        return []
    found: dict[str, Halt] = {}
    for feature in features:
        halt = _feature(feature)
        if halt is not None:
            found[halt.code] = halt
    return list(found.values())


def _feature(feature: object) -> Halt | None:
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") if isinstance(feature.get("properties"), dict) else feature
    if not isinstance(props, dict):
        return None
    code = clean_code(props.get("code") or props.get("station_code"))
    if not code:
        return None
    lon, lat = _point(feature.get("geometry"))
    if lat is None or lon is None:
        lon, lat = _pair(props.get("lon") or props.get("lng"), props.get("lat"))
    if lat is None or lon is None:
        return None
    if not (6.0 <= lat <= 37.5 and 68.0 <= lon <= 98.0):
        return None
    name = str(props.get("name") or props.get("station_name") or code).strip() or code
    zone = str(props.get("zone") or props.get("region_code") or "").strip()
    if zone == "?":
        zone = ""
    kind = "halt" if str(props.get("railway") or "") == "halt" else halt_kind(name)
    return Halt(code, name, lat, lon, kind, zone, "datameet")


def _point(geometry: object) -> tuple[float | None, float | None]:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None, None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return None, None
    return _pair(coords[0], coords[1])


def _pair(lon: object, lat: object) -> tuple[float | None, float | None]:
    try:
        lon_f, lat_f = float(lon), float(lat)
    except (TypeError, ValueError):
        return None, None
    return lon_f, lat_f


async def fetch_catalog(session: aiohttp.ClientSession, url: str = CATALOG_URL) -> list[Halt]:
    timeout = aiohttp.ClientTimeout(total=60)
    async with session.get(url, headers=_HEADERS, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"catalog_http_{response.status}")
        payload = json.loads(await response.text())
    halts = parse_station_catalog(payload)
    if len(halts) < 1000:
        raise RuntimeError("catalog_short")
    return halts


async def ensure_catalog(session: aiohttp.ClientSession, store: Store) -> int:
    global _failed_at
    if store.meta("catalog_source") == "datameet":
        return store.halt_count()
    now = time.monotonic()
    if _failed_at and now - _failed_at < 300:
        return store.halt_count()
    try:
        halts = await fetch_catalog(session)
    except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError):
        _failed_at = now
        return store.halt_count()
    store.upsert_halts(halts)
    store.meta_set("catalog_source", "datameet")
    _failed_at = 0.0
    return store.halt_count()


def main() -> None:
    import argparse

    from gateahead.config import load_settings

    parser = argparse.ArgumentParser(description="Load the station and halt catalog")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    store = Store(settings.db_path)
    if args.refresh:
        store.meta_set("catalog_source", "")

    async def run() -> int:
        async with aiohttp.ClientSession() as session:
            return await ensure_catalog(session, store)

    count = asyncio.run(run())
    print(json.dumps({"halts": count, "source": store.meta("catalog_source")}))


if __name__ == "__main__":
    main()
