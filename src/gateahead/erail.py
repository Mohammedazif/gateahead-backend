from __future__ import annotations

import re
import time

import aiohttp

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}


def parse_train_rows(body: str) -> list[tuple[str, int | None]]:
    rows: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for chunk in body.split("^"):
        parts = chunk.split("~")
        if len(parts) < 2:
            continue
        number = parts[0].strip()
        if not re.fullmatch(r"\d{5}", number) or number in seen:
            continue
        seen.add(number)
        clock = parts[10] if len(parts) > 10 else ""
        rows.append((number, _clock_minutes(clock)))
    return rows


def parse_train_numbers(body: str) -> list[str]:
    return [number for number, _clock in parse_train_rows(body)]


def _clock_minutes(value: str) -> int | None:
    text = value.strip().replace(".", ":")
    pieces = text.split(":")
    if len(pieces) != 2:
        return None
    try:
        hour, minute = int(pieces[0]), int(pieces[1])
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


_TTL = 600.0
_cache: dict[tuple[str, str], tuple[float, list[tuple[str, int | None]]]] = {}


async def train_rows(
    session: aiohttp.ClientSession, origin: str, destination: str
) -> list[tuple[str, int | None]]:
    key = (origin.upper(), destination.upper())
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    url = "https://erail.in/rail/getTrains.aspx"
    params = {
        "Station_From": key[0],
        "Station_To": key[1],
        "DataSource": "0",
        "Language": "0",
        "Cache": "true",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with session.get(url, params=params, headers=_HEADERS, timeout=timeout) as response:
            if response.status != 200:
                return []
            rows = parse_train_rows(await response.text())
    except (aiohttp.ClientError, TimeoutError):
        return []
    _cache[key] = (now, rows)
    return rows


async def trains_between(
    session: aiohttp.ClientSession, origin: str, destination: str
) -> list[str]:
    return [number for number, _clock in await train_rows(session, origin, destination)]
