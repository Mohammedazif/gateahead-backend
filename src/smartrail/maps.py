from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp

_PATTERNS = [
    r"@(-?\d+\.\d+),(-?\d+\.\d+)",
    r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
    r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)",
    r"[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)",
    r"[?&]center=(-?\d+\.\d+),(-?\d+\.\d+)",
    r"/search/(-?\d+\.\d+),?\+?(-?\d+\.\d+)",
    r"/place/[^/]+/(-?\d+\.\d+),(-?\d+\.\d+)",
    r"\[null,null,(-?\d+\.\d+),(-?\d+\.\d+)\]",
]
_PLUS = re.compile(
    r"\b([23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3})\b",
    re.I,
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


def coords_from_url(url: str) -> tuple[float, float] | None:
    for pattern in _PATTERNS:
        match = re.search(pattern, url)
        if match:
            return float(match.group(1)), float(match.group(2))
    plus = _plus_code_coords(url)
    if plus:
        return plus
    return None


def looks_like_maps(text: str) -> bool:
    lower = text.lower()
    return any(
        token in lower
        for token in ("google.com/maps", "maps.google", "goo.gl/maps", "maps.app.goo.gl", "openstreetmap.org")
    )


async def resolve_maps_url(session: aiohttp.ClientSession, text: str) -> tuple[float, float] | None:
    url = text.strip()
    coords = coords_from_url(url)
    if coords:
        return coords
    if not looks_like_maps(url) and "goo.gl" not in url.lower():
        return None
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with session.get(
            url, allow_redirects=True, timeout=timeout, headers=_HEADERS
        ) as response:
            final = str(response.url)
            body = await response.text()
    except aiohttp.ClientError:
        return None
    for blob in (final, unquote(final), body[:250000]):
        coords = coords_from_url(blob)
        if coords:
            return coords
    return _plus_code_coords(unquote(final)) or _plus_code_coords(url)


def _plus_code_coords(text: str) -> tuple[float, float] | None:
    raw = text
    parsed = urlparse(text)
    q = parse_qs(parsed.query).get("q", [""])[0]
    match = _PLUS.search(parsed.query) or _PLUS.search(raw) or _PLUS.search(q)
    if not match:
        spaced = unquote(q.replace("+", " "))
        match = _PLUS.search(spaced.replace(" ", "+", 1))
    if not match:
        return None
    code = match.group(1).upper()
    try:
        from openlocationcode import openlocationcode as olc
    except ImportError:
        return None
    if not olc.isValid(code):
        return None
    if olc.isShort(code):
        rest = raw.split(match.group(1), 1)[-1]
        place = unquote(rest.replace("+", " ")).split("&")[0]
        place = re.sub(r"\s+", " ", place).strip(" ,&=")
        hint = _place_hint(place)
        if hint is None:
            return None
        code = olc.recoverNearest(code, hint[0], hint[1])
    decoded = olc.decode(code)
    return decoded.latitudeCenter, decoded.longitudeCenter


def _place_hint(place: str) -> tuple[float, float] | None:
    if not place:
        return None
    cleaned = place.replace("Keralam", "Kerala")
    cleaned = re.sub(r"\b\d{6}\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    queries = [cleaned, re.sub(r"Railway Gate", "", cleaned, flags=re.I).strip(" ,")]
    try:
        from geopy.geocoders import Nominatim

        geo = Nominatim(user_agent="smartrail-gate-alert", timeout=10)
        for query in queries:
            if not query:
                continue
            found = geo.geocode(query)
            if found:
                return found.latitude, found.longitude
    except Exception:
        return None
    return None
