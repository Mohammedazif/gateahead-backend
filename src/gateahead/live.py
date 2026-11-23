from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from html import unescape
from urllib.parse import parse_qs, urlsplit

import aiohttp

from gateahead.geo import interpolate, km
from gateahead.clock import INDIA
from gateahead.models import Direction, TrainPosition
from gateahead.predict import REPORT_FRESH_MIN
from gateahead.snapshots import SnapshotCache

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


_LTS_TTL = 90.0
_lts_cache: dict[str, tuple[float, "LiveLook"]] = {}


_ETA_URL = "https://livestatus.railyatri.in/api/v3/train_eta_data/{number}/{day}.json"


@dataclass(frozen=True)
class LiveLook:
    moving: dict | None
    notice: str
    finished: bool
    fetched_at: float | None = None
    start_date: str = ""
    operating_status: str = "running"


async def fetch_lts(session: aiohttp.ClientSession, train_number: str) -> dict | None:
    return (await fetch_live(session, train_number)).moving


def cached_live(train_number: str, cache: SnapshotCache | None = None, start: date | None = None) -> LiveLook | None:
    if start is not None:
        key = f"live:v3:{train_number}:{start.isoformat()}"
        raw = cache.peek(key) if cache else None
        if raw is None and cache:
            legacy = cache.peek(f"live:{train_number}")
            if legacy and _report_matches(legacy.get("moving"), train_number, start):
                raw = {**legacy, "start_date": start.isoformat()}
        return _cached_look(raw, train_number, start)
    if cache is not None:
        return _cached_look(cache.peek(f"live:{train_number}"), train_number)
    hit = _lts_cache.get(train_number)
    return hit[1] if hit and time.monotonic() - hit[0] < _LTS_TTL else None


async def fetch_live(
    session: aiohttp.ClientSession, train_number: str, cache: SnapshotCache | None = None, start: date | None = None
) -> LiveLook:
    if start is not None:
        existing = cached_live(train_number, cache, start)
        if existing is not None:
            return existing
        async def load_run() -> dict:
            look = await fetch_run(session, train_number, start)
            return {"moving": look.moving, "notice": look.notice, "finished": look.finished,
                    "fetched_at": time.time(), "start_date": start.isoformat(), "operating_status": look.operating_status}
        raw = await cache.get(f"live:v3:{train_number}:{start.isoformat()}", _LTS_TTL, load_run) if cache else await load_run()
        return _cached_look(raw, train_number, start) or _unavailable_run(start)
    now = time.monotonic()
    hit = _lts_cache.get(train_number)
    if cache is None and hit and now - hit[0] < _LTS_TTL:
        return hit[1]
    if cache is not None:
        async def load() -> dict:
            look = await _fetch_live_data(session, train_number)
            return {"moving": look.moving, "notice": look.notice, "finished": look.finished, "fetched_at": time.time(),
                    "start_date": look.start_date, "operating_status": look.operating_status}

        raw = await cache.get(f"live:{train_number}", _LTS_TTL, load)
        look = _cached_look(raw, train_number) or LiveLook(None, "", False, operating_status="unknown")
    else:
        look = replace(await _fetch_live_data(session, train_number), fetched_at=time.time())
    if cache is None:
        _lts_cache[train_number] = (time.monotonic(), look)
    return look


def _cached_look(raw: dict | None, number: str, start: date | None = None) -> LiveLook | None:
    if not raw:
        return None
    moving = raw.get("moving")
    if moving is not None and not _report_matches(moving, number, start):
        return None
    return LiveLook(moving, raw["notice"], raw["finished"], raw.get("fetched_at"), raw.get("start_date", ""),
                    raw.get("operating_status", operating_status(moving)))


def _report_matches(report: object, number: str | None, start: date | None = None) -> bool:
    if not isinstance(report, dict):
        return False
    dates = {_parse_start(report[key]) for key in ("train_start_date", "start_date") if report.get(key)}
    if len(dates) > 1 or (start is not None and dates != {start.isoformat()}):
        return False
    if number is None:
        return True
    for key in ("train_number", "train_no", "trainNumber"):
        supplied = report.get(key)
        if supplied is None or supplied == "":
            continue
        identifier = str(supplied).strip()
        if not identifier.isascii() or not identifier.isdecimal() or identifier.zfill(5) != str(number).zfill(5):
            return False
    return True


def report_start(lts: dict) -> str:
    return _parse_start(lts.get("train_start_date") or lts.get("start_date") or "")


def _parse_start(raw: object) -> str:
    value = str(raw).strip()
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value[:10] if pattern != "%d %b %Y" else value, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def operating_status(lts: dict | None) -> str:
    if not lts:
        return "unknown"
    enabled = lambda value: value is True or str(value).lower() in {"1", "true", "yes"}
    text = " ".join(str(lts.get(k) or "") for k in ("train_status", "running_status", "message", "new_message")).lower()
    text = re.sub(r"\b(?:not|never)\s+(?:(?:been|be|is|yet|currently)\s+)*(?:cancelled|canceled|diverted)\b", "", text)
    if any(enabled(lts.get(k)) for k in ("is_partially_cancelled", "is_partially_canceled")) or re.search(r"\bpartially cancell?ed\b", text) or "partial cancellation" in text:
        return "partially_cancelled"
    if any(enabled(lts.get(k)) for k in ("is_cancelled", "cancelled", "is_canceled", "canceled")) or re.search(r"\bcancell?ed\b", text):
        return "cancelled"
    if enabled(lts.get("is_diverted")) or enabled(lts.get("diverted")) or re.search(r"(?<!not )\bdiverted\b", text):
        return "diverted"
    return "running"


async def fetch_run(session: aiohttp.ClientSession, number: str, start: date) -> LiveLook:
    offset = (datetime.now(INDIA).date() - start).days
    if offset < 0:
        return LiveLook(None, "Not started", False, start_date=start.isoformat())
    payload = await _fetch_lts_api(session, number, offset)
    if not _report_matches(payload, number, start):
        payload = await _fetch_lts_page(session, number, start)
    if not _report_matches(payload, number, start):
        return _unavailable_run(start)
    status = operating_status(payload)
    if status != "running":
        return LiveLook(payload, str(payload.get("new_message") or payload.get("message") or status.replace("_", " ")),
                        status == "cancelled", start_date=start.isoformat(), operating_status=status)
    result = classify_live(payload, None)
    return replace(result, start_date=start.isoformat())


def _unavailable_run(start: date) -> LiveLook:
    return LiveLook(None, "Journey report unavailable", False, start_date=start.isoformat(), operating_status="unknown")


async def _fetch_live_data(session: aiohttp.ClientSession, train_number: str) -> LiveLook:
    today = await _fetch_lts_api(session, train_number, 0)
    if not _report_matches(today, train_number):
        today = None
    yesterday = None
    if not _on_the_move(today):
        yesterday = await _fetch_lts_api(session, train_number, 1)
        if not _report_matches(yesterday, train_number):
            yesterday = None
    look = classify_live(today, yesterday)
    if look.moving is None and today is None and yesterday is None:
        page = await _fetch_lts_page(session, train_number, None)
        if _report_matches(page, train_number):
            look = classify_live(page, None)
    return look


def _on_the_move(lts: dict | None) -> bool:
    if not isinstance(lts, dict) or not lts.get("success"):
        return False
    if lts.get("at_dstn"):
        return False
    return bool(lts.get("current_station_code") or lts.get("current_station_name"))


def _choose_lts(today: dict | None, yesterday: dict | None) -> dict | None:
    if _on_the_move(today):
        return today
    if _on_the_move(yesterday):
        return yesterday
    return None


def classify_live(today: dict | None, yesterday: dict | None) -> LiveLook:
    if today and operating_status(today) in {"cancelled", "diverted", "partially_cancelled"}:
        state = operating_status(today)
        return LiveLook(today, str(today.get("new_message") or today.get("message") or state), state == "cancelled",
                        start_date=report_start(today), operating_status=state)
    moving = _choose_lts(today, yesterday)
    if moving is not None:
        return LiveLook(moving, "", False, start_date=report_start(moving))
    if isinstance(today, dict) and today.get("success") and today.get("at_dstn"):
        return LiveLook(None, "", True, start_date=report_start(today))
    notice = _notice_text(today)
    if notice or _bare_origin(today):
        return LiveLook(None, notice or "not started", False, start_date=report_start(today or {}))
    return LiveLook(None, "", False)


def _notice_text(lts: dict | None) -> str:
    if not isinstance(lts, dict) or _on_the_move(lts):
        return ""
    text = str(lts.get("new_message") or lts.get("message") or "").strip()
    folded = text.lower().replace("’", "'")
    if "already expired" in folded or "not started" in folded or "hasn't started" in folded:
        return text
    return ""


def _bare_origin(lts: dict | None) -> bool:
    if not isinstance(lts, dict) or not lts.get("success") or not lts.get("at_src"):
        return False
    return not (lts.get("current_station_code") or lts.get("current_station_name"))


def _keep_notice(raw: object, start: date | None = None, number: str | None = None) -> dict | None:
    if isinstance(raw, list):
        raw = [row for row in raw if _report_matches(row, number, start)]
        for row in raw:
            if isinstance(row, dict) and operating_status(row) in {"cancelled", "diverted", "partially_cancelled"}:
                return row
        kept = _normalize_lts(raw)
        if kept is not None:
            return kept
        for row in raw:
            if isinstance(row, dict) and _notice_text(row):
                return row
        return None
    if _report_matches(raw, number) and (raw.get("success") or _notice_text(raw) or operating_status(raw) in {"cancelled", "diverted", "partially_cancelled"}):
        return raw
    return None


async def _fetch_lts_api(session: aiohttp.ClientSession, train_number: str, day: int) -> dict | None:
    url = _ETA_URL.format(number=train_number, day=day)
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with session.get(
            url,
            params={
                "start_day": str(day),
                "user_id": "",
                "device_type_id": "4",
                "change_name": "seo_train_name",
                "claim_on_train": "false",
            },
            headers={**_HEADERS, "Accept": "application/json"},
            timeout=timeout,
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        payload = payload["data"]
    return _keep_notice(payload, datetime.now(INDIA).date() - timedelta(days=day), train_number)


def _usable(lts: dict | None) -> bool:
    if not lts:
        return False
    if lts.get("current_station_code") or lts.get("current_station_name"):
        return True
    if lts.get("at_src"):
        return False
    return bool(lts.get("success"))


async def _fetch_lts_page(
    session: aiohttp.ClientSession, train_number: str, start: date | None
) -> dict | None:
    url = f"https://www.railyatri.in/live-train-status/{train_number}"
    if start is not None:
        url += f"?startDate={start.isoformat()}"
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with session.get(url, headers=_HEADERS, timeout=timeout) as response:
            if response.status != 200:
                return None
            html = await response.text()
    except (aiohttp.ClientError, TimeoutError):
        return None
    match = _NEXT_DATA.search(html)
    raw = None
    if match:
        try:
            payload = json.loads(match.group(1))
            for key in ("props", "pageProps", "ltsData"):
                payload = payload.get(key) if isinstance(payload, dict) else None
            raw = payload
        except json.JSONDecodeError:
            raw = None
    lts = _keep_notice(raw, start, train_number)
    if _report_matches(lts, train_number, start) and (
        _usable(lts) or operating_status(lts) in {"cancelled", "diverted", "partially_cancelled"}
    ):
        return lts
    return _lts_from_html(html, train_number, start)


_CLOCK = re.compile(r"^\d{1,2}:\d{2}$")
_NEXT_IN = re.compile(r"(\d+)\s*min", re.I)
_AGE_TEXT = re.compile(r"(?:as of\s+)?(\d+(?:\.\d+)?)\s*(?:min|mins|minute|minutes)\s+ago", re.I)


def clean_place(value: object) -> str:
    text = str(value or "").replace("~", " ")
    parts: list[str] = []
    for word in text.split():
        key = word.upper().strip(".")
        if key == "JN":
            parts.append("Jn")
        elif key == "CNTL":
            parts.append("Central")
        elif word.isupper():
            parts.append(word.title())
        else:
            parts.append(word)
    return " ".join(parts)


def _clock(value: object) -> str:
    text = str(value or "").strip()
    return text if _CLOCK.match(text) else ""


def _next_in(info: object) -> str:
    if not isinstance(info, dict):
        return ""
    match = _NEXT_IN.search(str(info.get("next_stoppage_time_diff") or ""))
    if not match:
        return ""
    return f"in {int(match.group(1))} min"


def _covered_km(lts: dict) -> float | None:
    covered = _float(lts.get("distance_from_source"))
    if covered is None or covered < 0 or covered > 5000:
        return None
    return covered


def _left_km(lts: dict) -> int | None:
    total = lts.get("total_distance")
    covered = lts.get("distance_from_source")
    try:
        left = int(total) - int(covered)
    except (TypeError, ValueError, OverflowError):
        return None
    if left <= 0 or left > 5000:
        return None
    return left


_FEEDBACK_LINK = re.compile(
    r"live-train-status-user-feedback\?[^\"'<>\r\n]+",
    re.I,
)
_CURRENT_STATION = re.compile(r"current_station=([A-Z]{2,6})", re.I)


def _lts_from_html(html: str, train_number: str, start: date | None = None) -> dict | None:
    feedback_links = list(_FEEDBACK_LINK.finditer(html))
    for feedback in feedback_links:
        query = parse_qs(urlsplit(unescape(feedback.group())).query)
        code = (query.get("current_station") or [""])[0].upper()
        identity = {key: query[key][0] for key in ("train_no", "train_number", "start_date") if key in query}
        if re.fullmatch(r"[A-Z0-9]{1,6}", code) and _report_matches(identity, train_number, start):
            break
    else:
        if feedback_links or start is not None:
            return None
        match = _CURRENT_STATION.search(html)
        if not match:
            return None
        code = match.group(1).upper()
        identity = {}
    return {
        "success": True,
        **identity,
        "current_station_code": code,
        "current_station_name": code,
        "at_src": False,
        "status": "A",
        "delay": 0,
    }


def _normalize_lts(raw: object) -> dict | None:
    if isinstance(raw, dict) and raw.get("success"):
        return raw
    if isinstance(raw, list):
        dicts = [row for row in raw if isinstance(row, dict) and row.get("success")]
        live = [
            row
            for row in dicts
            if not row.get("at_src") and (row.get("current_station_code") or row.get("current_station_name"))
        ]
        if live:
            return live[0]
        return dicts[0] if dicts else None
    return None


def position_from_lts(
    train_number: str, lts: dict, now: datetime | None = None, *, fetched_at: float | None = None,
) -> TrainPosition | None:
    if not _report_matches(lts, train_number):
        return None
    now = now or datetime.now(INDIA)
    current_code = str(lts.get("current_station_code") or "").upper() or None
    if not current_code and lts.get("at_src"):
        current_code = str(lts.get("source") or "").upper() or None
    coords = _coords(lts)
    if coords is None:
        if not current_code:
            return None
        coords = (0.0, 0.0)
    upcoming_rows = lts.get("upcoming_stations")
    if not isinstance(upcoming_rows, (list, tuple)):
        upcoming_rows = []
    upcoming = tuple(
        str(row.get("station_code") or "").upper()
        for row in upcoming_rows
        if isinstance(row, dict) and row.get("station_code")
    )
    next_code = upcoming[0] if upcoming else str(lts.get("next_station_code") or "").upper() or None
    status = str(lts.get("status") or "").upper()
    at_station = bool(lts.get("at_src")) or status == "A"
    if status in {"T", "D"}:
        at_station = False
        to_point = _pair(lts.get("travelling_to_lat_lng"))
        if to_point:
            # ahead_distance is how far the train has run past the station it last reported ("8 kms ahead of X").
            ahead = _float(lts.get("ahead_distance")) or 0.0
            span = _track_span(lts, upcoming_rows, ahead) or km(coords, to_point)
            if span > 0.1 and ahead > 0:
                coords = interpolate(coords, to_point, min(0.95, ahead / span))
    delay = lts.get("delay")
    try:
        delay_minutes = int(delay) if delay is not None and str(delay).strip() != "" else 0
    except (TypeError, ValueError, OverflowError):
        delay_minutes = 0
    fetched = min(datetime.fromtimestamp(fetched_at, now.tzinfo), now) if fetched_at is not None else now
    report_at = _report_time(lts, fetched)
    age = (now - report_at).total_seconds() / 60 if report_at is not None else REPORT_FRESH_MIN
    info = lts.get("next_stoppage_info") if isinstance(lts.get("next_stoppage_info"), dict) else {}
    platform_no = lts.get("platform_number")
    try:
        platform = str(int(platform_no)) if int(platform_no) > 0 else ""
    except (TypeError, ValueError, OverflowError):
        platform = ""
    if status == "D":
        run = "departed"
    elif at_station:
        run = "at"
    else:
        run = "moving"
    scheduled = _clock(lts.get("cur_stn_std") if run == "departed" else lts.get("cur_stn_sta"))
    if not scheduled:
        scheduled = _clock(lts.get("cur_stn_std") or lts.get("cur_stn_sta"))
    return TrainPosition(
        train_number=str(train_number),
        lat=coords[0],
        lon=coords[1],
        last_station=current_code,
        next_station=next_code,
        at_station=at_station,
        delay_minutes=delay_minutes,
        direction=_direction(lts),
        as_of=now,
        upcoming_codes=upcoming,
        current_name=clean_place(lts.get("current_station_name") or current_code or ""),
        age_minutes=age,
        station_etas=_station_etas(upcoming_rows, report_at or fetched),
        name=str(lts.get("train_name") or ""),
        origin_name=clean_place(lts.get("source_stn_name")),
        dest_name=clean_place(lts.get("dest_stn_name")),
        next_name=clean_place(info.get("next_stoppage")),
        next_in=_next_in(info),
        departed_at=_clock(lts.get("etd") if run == "departed" else ""),
        scheduled_at=scheduled,
        platform=platform,
        left_km=_left_km(lts),
        run=run,
        covered_km=_covered_km(lts),
        start_date=report_start(lts),
        report_age_known=report_at is not None,
    )


def _track_span(lts: dict, upcoming_rows: list, ahead: float) -> float | None:
    """Track km from the last reported station to the next stop, when the feed gives both distances."""
    covered = _covered_km(lts)
    if covered is None:
        return None
    for row in upcoming_rows:
        if not isinstance(row, dict) or not row.get("station_code"):
            continue
        dest = _float(row.get("distance_from_source"))
        if dest is None:
            return None
        span = dest - (covered - ahead)
        return span if span > 0.1 else None
    return None


def _coords(lts: dict) -> tuple[float, float] | None:
    lat = _float(lts.get("cur_stn_lat"))
    lon = _float(lts.get("cur_stn_lng"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return _pair(lts.get("travelling_from_lat_lng"))


def _pair(value: object) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lat, lon = _float(value[0]), _float(value[1])
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _report_time(lts: dict, fetched: datetime) -> datetime | None:
    raw_age = lts.get("status_as_of_min")
    shown = str(lts.get("status_as_of") or "").strip()
    relative = _AGE_TEXT.fullmatch(shown) if raw_age is None or raw_age == "" else None
    age = _float(relative.group(1) if relative else raw_age) if not isinstance(raw_age, bool) else None
    if age is not None and age >= 0:
        try:
            return fetched - timedelta(minutes=age)
        except OverflowError:
            return None
    try:
        stamp = datetime.fromisoformat(shown)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    stamp = stamp.astimezone(fetched.tzinfo or INDIA)
    if fetched.tzinfo is None:
        stamp = stamp.replace(tzinfo=None)
    return stamp if stamp <= fetched else None


def _station_etas(rows: list, now: datetime) -> tuple[tuple[str, datetime], ...]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[tuple[str, datetime]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("station_code") or "").upper()
        if not code:
            continue
        raw_minute = row.get("a_min")
        raw_day = row.get("a_day")
        if isinstance(raw_minute, bool) or isinstance(raw_day, bool):
            continue
        minute = _float(raw_minute)
        day = _float(raw_day)
        if minute is None or minute < 0 or (raw_day not in (None, "") and (day is None or not day.is_integer())):
            continue
        try:
            stamp = midnight + timedelta(minutes=minute, days=day if day is not None and minute < 1440 else 0)
            if day is None and minute < 1440 and now - stamp > timedelta(hours=12):
                stamp += timedelta(days=1)
        except OverflowError:
            continue
        out.append((code, stamp))
    return tuple(out)


def _direction(lts: dict) -> Direction:
    text = str(lts.get("travelling_towards") or "").lower()
    if "south" in text or "east" in text:
        return "southbound"
    if "north" in text or "west" in text:
        return "northbound"
    return "unknown"
