from __future__ import annotations

import asyncio
import time

import aiohttp

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_HEADERS = {
    "User-Agent": "SmartRail/0.3 (https://github.com/Mohammedazif/smartrail-tele)",
    "Accept": "application/json",
}
_LOCAL = ("quarter", "neighbourhood", "suburb", "hamlet", "village", "town")
_CITY = ("city", "town", "municipality")
_DISTRICT = ("state_district", "county")
_SUCCESS_TTL = 6 * 3600.0
_MISS_TTL = 30.0
_cache: dict[tuple[int, int], tuple[float, str | None]] = {}
_inflight: dict[tuple[int, int], asyncio.Task[str | None]] = {}


def clear_place_cache() -> None:
    _cache.clear()
    _inflight.clear()


def label_from_address(address: dict) -> str | None:
    local = _pick(address, _LOCAL)
    parent = _parent(address, local)
    if local and parent:
        return f"{local}, {parent}"
    if local:
        return local
    city = _pick(address, _CITY)
    state = _text(address.get("state"))
    if city and state and not _same(city, state):
        return f"{city}, {state}"
    return city or _pick(address, _DISTRICT) or state


async def lookup_place(session: aiohttp.ClientSession, lat: float, lon: float) -> str | None:
    key = (round(lat * 1000), round(lon * 1000))
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    inflight = _inflight.get(key)
    if inflight:
        return await inflight
    task = asyncio.create_task(_fetch(session, lat, lon))
    _inflight[key] = task
    try:
        name = await task
    finally:
        _inflight.pop(key, None)
    _cache[key] = (time.monotonic() + (_SUCCESS_TTL if name else _MISS_TTL), name)
    if len(_cache) > 400:
        oldest = min(_cache, key=lambda item: _cache[item][0])
        _cache.pop(oldest, None)
    return name


def _parent(address: dict, local: str | None) -> str | None:
    for candidate in (_pick(address, _CITY), _pick(address, _DISTRICT), _text(address.get("state"))):
        if not candidate or _noisy(candidate):
            continue
        if local and _same(candidate, local):
            continue
        return candidate
    return None


def _pick(address: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        found = _text(address.get(key))
        if found:
            return found
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _same(left: str, right: str) -> bool:
    a = left.lower()
    b = right.lower()
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def _noisy(value: str) -> bool:
    lower = value.lower()
    return any(token in lower for token in ("taluka", "tehsil", "tahsil", "mandal", "subdivision"))


async def _fetch(session: aiohttp.ClientSession, lat: float, lon: float) -> str | None:
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with session.get(
            NOMINATIM_URL,
            params={
                "lat": f"{lat:.5f}",
                "lon": f"{lon:.5f}",
                "format": "jsonv2",
                "addressdetails": 1,
                "zoom": 14,
                "accept-language": "en",
            },
            headers=_HEADERS,
            timeout=timeout,
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    address = payload.get("address")
    if isinstance(address, dict):
        label = label_from_address(address)
        if label:
            return label
    name = _text(payload.get("name"))
    return name
