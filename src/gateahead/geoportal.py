from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path

from gateahead.halts import clean_code, halt_kind
from gateahead.models import Crossing, Halt
from gateahead.store import Store

STATIONS_URL = (
    "https://github.com/ramSeraph/indian_transport/releases/download/railways/IR_Stations.geojsonl.7z"
)
POIS_URL = (
    "https://github.com/ramSeraph/indian_transport/releases/download/railways/IR_Track_POIs.geojsonl.7z"
)
_LEVEL = "Level Xing"
_ZONE = re.compile(r"^[A-Z]{2,5}$")
_DMS = re.compile(
    r"(\d+(?:\.\d+)?)\s*°\s*(\d+(?:\.\d+)?)?\s*'?\s*(\d+(?:\.\d+)?)?\s*\"?\s*([NSEW])",
    re.I,
)
_GENERIC = re.compile(r"^(level\s*crossing|level\s*xing|lc|xing)?$", re.I)


def collect_stations(lines: Iterable[str]) -> tuple[list[Halt], dict[str, str], int]:
    best: dict[str, tuple[int, int, Halt]] = {}
    zones: dict[str, str] = {}
    skipped = 0
    for feature in _features(lines):
        parsed = _station(feature)
        if parsed is None:
            skipped += 1
            continue
        halt, rank, oid, zone = parsed
        current = best.get(halt.code)
        if current is None or rank > current[0] or (rank == current[0] and oid < current[1]):
            best[halt.code] = (rank, oid, halt)
            if zone:
                zones[halt.code] = zone
        elif zone and halt.code not in zones:
            zones[halt.code] = zone
    return [item[2] for item in best.values()], zones, skipped


def collect_crossings(lines: Iterable[str]) -> tuple[list[Crossing], int]:
    found: list[Crossing] = []
    skipped = 0
    seen: set[str] = set()
    for feature in _features(lines):
        crossing = _crossing(feature)
        if crossing is None:
            if _layer(feature) == _LEVEL:
                skipped += 1
            continue
        if crossing.id in seen:
            continue
        seen.add(crossing.id)
        found.append(crossing)
    return found, skipped


def load_geoportal(store: Store, stations_path: Path, pois_path: Path) -> dict[str, int]:
    halts, zones, stations_skipped = collect_stations(_open_lines(stations_path))
    added_halts = store.insert_missing_halts(halts)
    filled = store.fill_blank_zones(zones)
    crossings, crossings_skipped = collect_crossings(_open_lines(pois_path))
    added_crossings = store.insert_crossings(crossings)
    store.meta_set("geoportal_source", "irgeoportal")
    return {
        "stations_read": len(halts),
        "stations_added": added_halts,
        "stations_skipped": stations_skipped,
        "zones_filled": filled,
        "crossings_read": len(crossings),
        "crossings_added": added_crossings,
        "crossings_skipped": crossings_skipped,
    }


def _features(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            feature = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(feature, dict):
            yield feature


def _props(feature: dict) -> dict:
    props = feature.get("properties")
    return props if isinstance(props, dict) else {}


def _layer(feature: dict) -> str:
    return str(_props(feature).get("layer") or "").strip()


def _station(feature: dict) -> tuple[Halt, int, int, str] | None:
    props = _props(feature)
    code = clean_code(props.get("sttncode"))
    point = _india_point(props)
    if not code or point is None:
        return None
    lat, lon = point
    name = str(props.get("sttnname") or code).strip() or code
    kind = "halt" if str(props.get("sttntype") or "").strip().upper() == "HALT" else halt_kind(name)
    zone = str(props.get("railway") or "").strip().upper()
    if not _ZONE.fullmatch(zone):
        zone = ""
    rank = 1 if name != code else 0
    if str(props.get("centerline_of_stn") or "").strip().upper() == "Y":
        rank += 2
    try:
        oid = int(props.get("objectid") or 0)
    except (TypeError, ValueError):
        oid = 0
    return Halt(code, name, lat, lon, kind, zone, "irgeoportal"), rank, oid, zone


def _crossing(feature: dict) -> Crossing | None:
    if _layer(feature) != _LEVEL:
        return None
    props = _props(feature)
    point = _india_point(props)
    oid = _oid(props.get("objectid"))
    if point is None or not oid:
        return None
    lat, lon = point
    nearest = clean_code(props.get("stnfrom")) or clean_code(props.get("stnto"))
    return Crossing(
        id=f"ir:{oid}",
        name=_crossing_name(props.get("fdetail"), props.get("remark")),
        lat=lat,
        lon=lon,
        source="irgeoportal",
        nearest_station=nearest,
    )


def _crossing_name(detail: object, remark: object) -> str:
    label = _label(detail)
    note = _label(remark)
    if label and not _GENERIC.fullmatch(label):
        return label[:80]
    if note and not _GENERIC.fullmatch(note):
        return note[:80]
    return "Level crossing"


def _label(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\[\s*Level Xing\s*\]", "", text, flags=re.I).strip(" ;")
    return re.sub(r"\s+", " ", text).strip()


def _oid(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return "" if not text or text.lower() == "none" else text


def _india_point(props: dict) -> tuple[float, float] | None:
    lat, lon = _pair(props.get("ycoord"), props.get("xcoord"))
    if lat is None or lon is None:
        lat, lon = _dms(props.get("latitude")), _dms(props.get("longitude"))
    if lat is None or lon is None:
        return None
    if not (6.0 <= lat <= 37.5 and 68.0 <= lon <= 98.0):
        return None
    return lat, lon


def _pair(lat: object, lon: object) -> tuple[float | None, float | None]:
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None


def _dms(value: object) -> float | None:
    match = _DMS.search(str(value or ""))
    if match is None:
        return None
    deg = float(match.group(1))
    minutes = float(match.group(2) or 0)
    seconds = float(match.group(3) or 0)
    sign = -1.0 if match.group(4).upper() in "SW" else 1.0
    return sign * (deg + minutes / 60.0 + seconds / 3600.0)


def _open_lines(path: Path) -> Iterator[str]:
    if path.suffix == ".7z":
        proc = subprocess.Popen(
            ["7z", "e", "-so", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        for raw in proc.stdout:
            yield raw.decode("utf-8", "replace")
        proc.wait()
        return
    with path.open(encoding="utf-8") as handle:
        yield from handle


def main() -> None:
    import argparse

    from gateahead.config import load_settings

    parser = argparse.ArgumentParser(description="Load IR GeoPortal stations and level crossings")
    parser.add_argument("--stations", type=Path, required=True)
    parser.add_argument("--pois", type=Path, required=True)
    args = parser.parse_args()
    store = Store(load_settings().db_path)
    print(json.dumps(load_geoportal(store, args.stations, args.pois)))


if __name__ == "__main__":
    main()
