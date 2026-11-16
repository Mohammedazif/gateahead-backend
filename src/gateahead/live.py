from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

import aiohttp

from gateahead.geo import interpolate, km
from gateahead.clock import INDIA
from gateahead.models import Direction, TrainPosition
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
        key = f"live:v2:{train_number}:{start.isoformat()}"
        raw = cache.peek(key) if cache else None
        if raw is None and cache:
            legacy = cache.peek(f"live:{train_number}")
            if legacy and report_start(legacy.get("moving") or {}) == start.isoformat():
                raw = {**legacy, "start_date": start.isoformat()}
        return LiveLook(**{**raw, "operating_status": raw.get("operating_status", operating_status(raw.get("moving")))}) if raw else None
    if cache is not None:
        raw = cache.peek(f"live:{train_number}")
        return LiveLook(raw["moving"], raw["notice"], raw["finished"], raw.get("fetched_at"), raw.get("start_date", ""),
                        raw.get("operating_status", operating_status(raw.get("moving")))) if raw else None
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
        raw = await cache.get(f"live:v2:{train_number}:{start.isoformat()}", _LTS_TTL, load_run) if cache else await load_run()
        return LiveLook(**raw)
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
        look = LiveLook(raw["moving"], raw["notice"], raw["finished"], raw.get("fetched_at"), raw.get("start_date", ""),
                        raw.get("operating_status", operating_status(raw.get("moving"))))
    else:
        look = replace(await _fetch_live_data(session, train_number), fetched_at=time.time())
    if cache is None:
        _lts_cache[train_number] = (time.monotonic(), look)
    return look


def report_start(lts: dict) -> str:
    value = str(lts.get("train_start_date") or lts.get("start_date") or "").strip()
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
    if payload is None:
        payload = await _fetch_lts_page(session, number, start)
    # Never relabel today's response as an older journey when an upstream ignores the requested date.
    if payload is None or report_start(payload) != start.isoformat():
        return LiveLook(None, "Journey-date report unavailable", False, start_date=start.isoformat(), operating_status="unknown")
    status = operating_status(payload)
    if status != "running":
        return LiveLook(payload, str(payload.get("new_message") or payload.get("message") or status.replace("_", " ")),
                        status == "cancelled", start_date=start.isoformat(), operating_status=status)
    result = classify_live(payload, None)
    return replace(result, start_date=start.isoformat())


async def _fetch_live_data(session: aiohttp.ClientSession, train_number: str) -> LiveLook:
    today = await _fetch_lts_api(session, train_number, 0)
    yesterday = None
    if not _on_the_move(today):
        yesterday = await _fetch_lts_api(session, train_number, 1)
    look = classify_live(today, yesterday)
    if look.moving is None and today is None and yesterday is None:
        page = await _fetch_lts_page(session, train_number, None)
        if page is not None:
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
        return LiveLook(moving, "", False)
    if isinstance(today, dict) and today.get("success") and today.get("at_dstn"):
        return LiveLook(None, "", True)
    notice = _notice_text(today)
    if notice or _bare_origin(today):
        return LiveLook(None, notice or "not started", False)
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


def _keep_notice(raw: object, start: date | None = None) -> dict | None:
    if isinstance(raw, list):
        if start is not None:
            raw = [row for row in raw if isinstance(row, dict) and report_start(row) == start.isoformat()]
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
    if isinstance(raw, dict) and (raw.get("success") or _notice_text(raw) or operating_status(raw) in {"cancelled", "diverted", "partially_cancelled"}):
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
    return _keep_notice(payload, datetime.now(INDIA).date() - timedelta(days=day))


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
    lts = _keep_notice(raw, start)
    if _usable(lts) or (lts and operating_status(lts) in {"cancelled", "diverted", "partially_cancelled"}):
        return lts
    return _lts_from_html(html, train_number)


_CLOCK = re.compile(r"^\d{1,2}:\d{2}$")
_NEXT_IN = re.compile(r"(\d+)\s*min", re.I)


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


_FEEDBACK_STATION = re.compile(
    r"live-train-status-user-feedback[^\"']*current_station=([A-Z]{2,6})",
    re.I,
)
_CURRENT_STATION = re.compile(r"current_station=([A-Z]{2,6})", re.I)


def _lts_from_html(html: str, train_number: str) -> dict | None:
    match = _FEEDBACK_STATION.search(html) or _CURRENT_STATION.search(html)
    if not match:
        return None
    code = match.group(1).upper()
    return {
        "success": True,
        "train_number": str(train_number),
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


def position_from_lts(train_number: str, lts: dict, now: datetime | None = None) -> TrainPosition | None:
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
    _reported, age = _report_age(lts, now)
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
        station_etas=_station_etas(upcoming_rows, now),
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


def _report_age(lts: dict, now: datetime) -> tuple[datetime, float]:
    raw_age = lts.get("status_as_of_min")
    if raw_age is None or raw_age == "":
        return now, 0.0
    age = _float(raw_age)
    # An invalid supplied age cannot establish that the report is fresh.
    if age is None:
        age = 8.0
    age = max(0.0, age)
    try:
        return now - timedelta(minutes=age), age
    except OverflowError:
        return now - timedelta(minutes=8), 8.0


def _station_etas(rows: list, now: datetime) -> tuple[tuple[str, datetime], ...]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[tuple[str, datetime]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("station_code") or "").upper()
        if not code:
            continue
        stamp = None
        a_min = row.get("a_min")
        if a_min is not None and str(a_min).strip() != "":
            try:
                stamp = midnight + timedelta(minutes=int(a_min))
            except (TypeError, ValueError, OverflowError):
                stamp = None
        if stamp is not None:
            out.append((code, stamp))
    return tuple(out)


def _direction(lts: dict) -> Direction:
    text = str(lts.get("travelling_towards") or "").lower()
    if "south" in text or "east" in text:
        return "southbound"
    if "north" in text or "west" in text:
        return "northbound"
    return "unknown"
