from __future__ import annotations

import re
import time
from html.parser import HTMLParser

import aiohttp

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}


class TrainRows(list[tuple[str, int | None]]):
    """Preserve list compatibility while distinguishing an unavailable lookup."""

    def __init__(self, rows=(), *, unavailable: bool = False, source: str = "erail", error: str = "", attempts=()):
        super().__init__(rows)
        self.unavailable = unavailable
        self.source = source
        self.error = error
        self.attempts = list(attempts)


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


def parse_train_names(body: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for chunk in body.split("^"):
        parts = chunk.split("~")
        if len(parts) < 2:
            continue
        number, name = parts[0].strip(), " ".join(parts[1].split())
        if re.fullmatch(r"\d{5}", number) and name and name != number:
            names.setdefault(number, name)
    return names


def known_train_names(numbers: list[str]) -> dict[str, str]:
    return {number: _names[number] for number in numbers if number in _names}


class _BoardText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(" ".join(data.split()))


_BOARD_TRAIN = re.compile(r"(?P<name>[A-Z][A-Z0-9 &()./'–-]{1,120}?)\s*(?P<number>\d{5})\s*,\s*\d{1,3}[.:]\d{2}\s*hr\b", re.I)
_BOARD_CLOCK = re.compile(r"\b(?:[01]?\d|2[0-3])[.:][0-5]\d\b")


def parse_station_board(html: str) -> tuple[list[tuple[str, int | None]], dict[str, str]]:
    """Read the station's server-rendered departures, including single-stop expresses."""
    parser = _BoardText()
    parser.feed(html)
    plain = "\n".join(parser.parts)
    headers = list(_BOARD_TRAIN.finditer(plain))
    rows: list[tuple[str, int | None]] = []
    names: dict[str, str] = {}
    for index, header in enumerate(headers):
        number = header["number"]
        if number in names:
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(plain)
        clock = _BOARD_CLOCK.search(plain, header.end(), end)
        rows.append((number, _clock_minutes(clock[0]) if clock else None))
        names[number] = header["name"].strip()
    return rows, names


class _StationTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.title, self.in_title = "", False

    def handle_starttag(self, tag, attrs):
        if tag == "h1": self.in_title = True
        if tag == "table": self.table = []
        if tag == "tr" and self.table is not None: self.row = []
        if tag in {"td", "th"} and self.row is not None: self.cell = []

    def handle_data(self, data):
        if self.in_title: self.title += data
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "h1": self.in_title = False
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        if tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_railyatri_board(html: str, code: str) -> tuple[list[tuple[str, int | None]], dict[str, str]]:
    parser = _StationTable()
    parser.feed(html)
    if not re.search(r"\(" + re.escape(code) + r"\)", parser.title, re.I):
        return [], {}
    rows, names = [], {}
    for table in parser.tables:
        if not table or not any("depart" in cell.lower() for cell in table[0]):
            continue
        headers = [cell.lower() for cell in table[0]]
        departure = next(i for i, cell in enumerate(headers) if "depart" in cell)
        for cells in table[1:]:
            number = re.search(r"\b(\d{5})\b", cells[0]) if cells else None
            if not number or number[1] in names or departure >= len(cells):
                continue
            names[number[1]] = re.sub(r"\s*\(?" + number[1] + r"\)?\s*", " ", cells[0]).strip()
            rows.append((number[1], _clock_minutes(cells[departure])))
    return rows, names


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
_names: dict[str, str] = {}


async def station_rows(session: aiohttp.ClientSession, code: str, name: str, *, refresh: bool = False) -> list[tuple[str, int | None]]:
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,6}", code):
        return []
    key = (code, code)
    hit = _cache.get(key)
    if not refresh and hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or code.lower()
    station = f"{slug}-{code}"
    attempts = []
    providers = [("erail", f"https://erail.in/trains-between-stations/{station}/{station}"),
                 ("railyatri", f"https://www.railyatri.in/stations/{slug}-{code.lower()}")]
    for provider, url in providers:
        error = ""
        try:
            async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    error = f"http_{response.status}"
                else:
                    html = await response.text()
                    rows, names = parse_station_board(html) if provider == "erail" else parse_railyatri_board(html, code)
                    if rows:
                        attempts.append((provider, ""))
                        found = TrainRows(rows, source=provider, attempts=attempts)
                        _cache[key] = (time.monotonic(), found)
                        _names.update(names)
                        return found
                    error = "unparseable_board"
        except TimeoutError:
            error = "timeout"
        except (aiohttp.ClientError, ValueError):
            error = "network_or_invalid_response"
        attempts.append((provider, error))
    return TrainRows(unavailable=True, error="; ".join(f"{p}:{e}" for p, e in attempts), attempts=attempts)


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
                return TrainRows(unavailable=True)
            body = await response.text()
            rows = parse_train_rows(body)
            # The provider's delimiter-only body is an identifiable empty
            # response. Other unparseable successful bodies remain unknown.
            if not rows and not ("~" in body and not body.strip("~^ \t\r\n")):
                return TrainRows(unavailable=True)
            _names.update(parse_train_names(body))
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return TrainRows(unavailable=True)
    _cache[key] = (now, rows)
    return rows


async def trains_between(
    session: aiohttp.ClientSession, origin: str, destination: str
) -> list[str]:
    return [number for number, _clock in await train_rows(session, origin, destination)]
