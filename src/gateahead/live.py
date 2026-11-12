from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import aiohttp

from gateahead.geo import interpolate, km
from gateahead.models import Direction, TrainPosition

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


async def fetch_lts(session: aiohttp.ClientSession, train_number: str) -> dict | None:
    return (await fetch_live(session, train_number)).moving


async def fetch_live(session: aiohttp.ClientSession, train_number: str) -> LiveLook:
    now = time.monotonic()
    hit = _lts_cache.get(train_number)
    if hit and now - hit[0] < _LTS_TTL:
        return hit[1]
    today = await _fetch_lts_api(session, train_number, 0)
    yesterday = None
    if not _on_the_move(today):
        yesterday = await _fetch_lts_api(session, train_number, 1)
    look = classify_live(today, yesterday)
    if look.moving is None and today is None and yesterday is None:
        page = await _fetch_lts_page(session, train_number, None)
        if page is not None:
            look = classify_live(page, None)
    _lts_cache[train_number] = (now, look)
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


def _keep_notice(raw: object) -> dict | None:
    if isinstance(raw, list):
        kept = _normalize_lts(raw)
        if kept is not None:
            return kept
        for row in raw:
            if isinstance(row, dict) and _notice_text(row):
                return row
        return None
    if isinstance(raw, dict) and (raw.get("success") or _notice_text(raw)):
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
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return _keep_notice(payload)


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
            raw = payload.get("props", {}).get("pageProps", {}).get("ltsData")
        except json.JSONDecodeError:
            raw = None
    lts = _normalize_lts(raw)
    if _usable(lts):
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
    try:
        covered = float(lts.get("distance_from_source"))
    except (TypeError, ValueError):
        return None
    if covered < 0 or covered > 5000:
        return None
    return covered


def _left_km(lts: dict) -> int | None:
    total = lts.get("total_distance")
    covered = lts.get("distance_from_source")
    try:
        left = int(total) - int(covered)
    except (TypeError, ValueError):
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
    now = now or datetime.now()
    current_code = str(lts.get("current_station_code") or "").upper() or None
    if not current_code and lts.get("at_src"):
        current_code = str(lts.get("source") or "").upper() or None
    coords = _coords(lts)
    if coords is None:
        if not current_code:
            return None
        coords = (0.0, 0.0)
    upcoming_rows = lts.get("upcoming_stations") or []
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
            ahead = _float(lts.get("ahead_distance")) or 0.0
            seg = km(coords, to_point)
            if seg > 0.1 and ahead >= 0:
                t = min(0.9, max(0.0, 1.0 - (ahead / seg)))
                coords = interpolate(coords, to_point, t)
    delay = lts.get("delay")
    try:
        delay_minutes = int(delay) if delay is not None and str(delay).strip() != "" else 0
    except (TypeError, ValueError):
        delay_minutes = 0
    _reported, age = _report_age(lts, now)
    info = lts.get("next_stoppage_info") if isinstance(lts.get("next_stoppage_info"), dict) else {}
    platform_no = lts.get("platform_number")
    try:
        platform = str(int(platform_no)) if int(platform_no) > 0 else ""
    except (TypeError, ValueError):
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
    )


def _coords(lts: dict) -> tuple[float, float] | None:
    lat = _float(lts.get("cur_stn_lat"))
    lon = _float(lts.get("cur_stn_lng"))
    if lat is not None and lon is not None:
        return lat, lon
    return _pair(lts.get("travelling_from_lat_lng"))


def _pair(value: object) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lat, lon = _float(value[0]), _float(value[1])
    if lat is None or lon is None:
        return None
    return lat, lon


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _report_age(lts: dict, now: datetime) -> tuple[datetime, float]:
    raw_age = lts.get("status_as_of_min")
    if isinstance(raw_age, (int, float)):
        age = max(0.0, float(raw_age))
        return now - timedelta(minutes=age), age
    return now, 0.0


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
            except (TypeError, ValueError):
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
