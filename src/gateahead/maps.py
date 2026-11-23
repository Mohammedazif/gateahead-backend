from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import unquote

import aiohttp
from aiohttp.resolver import ThreadedResolver
from openlocationcode import openlocationcode as olc
from yarl import URL

_PATTERNS = [
    r"@(-?\d+\.\d+),(-?\d+\.\d+)",
    r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
    r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)",
    r"[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)",
    r"[?&]center=(-?\d+\.\d+),(-?\d+\.\d+)",
    r"/search/(-?\d+\.\d+),?\+?(-?\d+\.\d+)",
    r"/place/[^/]+/(-?\d+\.\d+),(-?\d+\.\d+)",
    r"\[null,null,(-?\d+\.\d+),(-?\d+\.\d+)\]",
    r"#map=\d+\/(-?\d+\.\d+)\/(-?\d+\.\d+)",
]
_PLUS = re.compile(
    r"\b([23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3})\b",
    re.I,
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}
_FULL_HOSTS = {"google.com", "www.google.com", "google.co.in", "www.google.co.in"}
_MAP_HOSTS = {"maps.google.com", "maps.google.co.in", "maps.app.goo.gl", "openstreetmap.org", "www.openstreetmap.org"}
_MAP_BODY_LIMIT = 250_000


class _PublicMapResolver(ThreadedResolver):
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        addresses = await super().resolve(host, port, family)
        for entry in addresses:
            address = ipaddress.ip_address(entry["host"].split("%", 1)[0])
            address = getattr(address, "ipv4_mapped", None) or address
            if not address.is_global or address.is_multicast:
                raise OSError("Non-public map provider address")
        return addresses


def _map_url(text: str) -> URL | None:
    text = text.strip()
    if len(text) > 8192 or any(character.isspace() or character == "\\" for character in text):
        return None
    try:
        url = URL(text)
        host = url.raw_host
        if url.scheme != "https" or url.user is not None or url.password is not None or url.port != 443:
            return None
    except (ValueError, UnicodeError):
        return None
    maps_path = url.path == "/maps" or url.path.startswith("/maps/")
    if host in _MAP_HOSTS or ((host in _FULL_HOSTS or host == "goo.gl") and maps_path):
        return url
    return None


def coords_from_url(url: str) -> tuple[float, float] | None:
    for pattern in _PATTERNS:
        match = re.search(pattern, url)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None
    code = _plus_code(url)
    if code and not olc.isShort(code):
        return _decode_plus(code)
    return None


def looks_like_maps(text: str) -> bool:
    return _map_url(text) is not None


async def resolve_maps_url(text: str) -> tuple[float, float] | None:
    url = _map_url(text)
    if url is None:
        return None
    coords = coords_from_url(str(url))
    if coords is not None:
        return coords
    try:
        async with asyncio.timeout(15):
            resolver = _PublicMapResolver()
            try:
                connector = aiohttp.TCPConnector(resolver=resolver)
                async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
                    return await _resolve_plus(session, url) or await _follow_map_link(session, url)
            finally:
                await resolver.close()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
        return None


async def _follow_map_link(session: aiohttp.ClientSession, url: URL) -> tuple[float, float] | None:
    for _ in range(4):
        async with session.get(url, allow_redirects=False, headers=_HEADERS) as response:
            if response.status in {301, 302, 303, 307, 308}:
                target = response.headers.get("Location")
                next_url = _map_url(str(url.join(URL(target)))) if target else None
                if next_url is None:
                    return None
                url = next_url
                coords = coords_from_url(str(url)) or await _resolve_plus(session, url)
                if coords is not None:
                    return coords
                continue
            if response.status != 200:
                return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(16_384):
                body.extend(chunk[:_MAP_BODY_LIMIT - len(body)])
                if len(body) >= _MAP_BODY_LIMIT:
                    break
            return coords_from_url(body.decode("utf-8", errors="replace"))
    return None


def _plus_code(text: str) -> str | None:
    match = _PLUS.search(text) or _PLUS.search(unquote(text))
    if not match:
        return None
    code = match.group(1).upper()
    return code if olc.isValid(code) else None


def _decode_plus(code: str) -> tuple[float, float]:
    decoded = olc.decode(code)
    return decoded.latitudeCenter, decoded.longitudeCenter


async def _resolve_plus(session: aiohttp.ClientSession, url: URL) -> tuple[float, float] | None:
    text = unquote(str(url))
    code = _plus_code(text)
    if not code or not olc.isShort(code):
        return None
    place = text.upper().find(code) + len(code)
    hint = await _place_hint(session, text[place:].split("&", 1)[0].replace("+", " ").strip(" ,&="))
    return _decode_plus(olc.recoverNearest(code, *hint)) if hint else None


async def _place_hint(session: aiohttp.ClientSession, place: str) -> tuple[float, float] | None:
    if not place:
        return None
    cleaned = place.replace("Keralam", "Kerala")
    cleaned = re.sub(r"\b\d{6}\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    queries = [cleaned, re.sub(r"Railway Gate", "", cleaned, flags=re.I).strip(" ,")]
    for query in dict.fromkeys(queries):
        if not query:
            continue
        try:
            async with session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query[:200], "format": "jsonv2", "limit": 1},
                headers={"User-Agent": "GateAhead/0.3 (https://github.com/Mohammedazif/gateahead-tele)"},
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=4),
            ) as response:
                if response.status != 200:
                    continue
                found = await response.json(content_type=None)
                if isinstance(found, list) and found and isinstance(found[0], dict):
                    lat, lon = float(found[0]["lat"]), float(found[0]["lon"])
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        return lat, lon
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError):
            continue
    return None
