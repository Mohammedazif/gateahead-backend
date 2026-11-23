from __future__ import annotations

import asyncio
import logging
import os
import math
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel, Field

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from gateahead.cities import search_cities
from gateahead.clusters import gate_label
from gateahead.config import load_settings
from gateahead.halts import clean_code, ensure_catalog
from gateahead.place import lookup_place
from gateahead.service import GateAhead, crossing_distances
from gateahead.reports import Reports
from gateahead.store import Store



@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await startup()
        yield
    finally:
        await shutdown()


app = FastAPI(title="GateAhead", lifespan=lifespan)
_origins = [item.strip() for item in os.getenv("WEB_ORIGIN", "http://localhost:5173").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)
_HITS: dict[str, list[float]] = {}
_HITS_CAPACITY = 16_384
_HITS_PRUNED_AT = float("-inf")
_LIMIT = 20
_WINDOW = 600.0
_settings = load_settings()
_store = Store(_settings.db_path)
_reports = Reports(_settings.db_path)
_session: aiohttp.ClientSession | None = None
_service: GateAhead | None = None
_maintenance: asyncio.Task | None = None
_log = logging.getLogger(__name__)


async def _maintain_storage() -> None:
    while True:
        try:
            _store.prune_retained_data()
        except sqlite3.OperationalError:
            _log.exception("Deferred database cleanup until the next maintenance interval")
        await asyncio.sleep(60)


async def startup() -> None:
    global _session, _service, _maintenance
    _session = aiohttp.ClientSession()
    _service = GateAhead(_settings, _store, _session)
    await ensure_catalog(_session, _store)
    _maintenance = asyncio.create_task(_maintain_storage())


async def shutdown() -> None:
    global _maintenance, _service, _session
    if _maintenance:
        _maintenance.cancel()
        await asyncio.gather(_maintenance, return_exceptions=True)
    if _service:
        await _service.snapshots.shutdown()
    if _session:
        await _session.close()
    _maintenance = _service = _session = None


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "database_id": _store.meta("database_id"), "providers": _store.provider_status(),
            "worker": _store.worker_status(), "worker_required": os.getenv("GATEAHEAD_WORKER_REQUIRED") == "1"}


@app.get("/ready")
async def ready(response: Response) -> dict:
    status = _store.catalog_status(_settings.route_max_age_hours)
    checks = {"worker": status["worker"]["state"] in {"healthy", "partial"},
              "stations": status["stations"] > 0, "fresh_routes": status["fresh_routes"] > 0,
              "route_geometry": status["fresh_routes_with_geometry"] > 0}
    ok = all(checks.values())
    response.status_code = 200 if ok else 503
    return {"ok": ok, "database_id": status["database_id"], "checks": checks,
            "worker": status["worker"], "national_coverage_verified": False}


@app.get("/coverage")
async def coverage() -> dict:
    return _store.catalog_status(_settings.route_max_age_hours)


@app.get("/cities")
async def cities(q: str = Query("", max_length=100), limit: int = Query(8, ge=1, le=20)) -> dict:
    query = " ".join(q.split())
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Search needs at least 2 letters")
    found = search_cities(query, limit)
    return {
        "cities": [
            {"name": city.name, "state": city.state, "lat": city.lat, "lon": city.lon} for city in found
        ]
    }


@app.get("/place")
async def place(
    lat: float = Query(..., ge=-90, le=90, allow_inf_nan=False),
    lon: float = Query(..., ge=-180, le=180, allow_inf_nan=False),
) -> dict:
    assert _session is not None
    return {"name": await lookup_place(_session, lat, lon)}


def _halt_row(halt, distance: float | None = None) -> dict:
    row = {
        "code": halt.code,
        "name": halt.name,
        "lat": halt.lat,
        "lon": halt.lon,
        "kind": halt.kind,
        "zone": halt.zone,
    }
    if distance is not None:
        row["km"] = round(distance, 3)
    return row


def _catalog_meta() -> dict:
    counts = _store.halt_counts()
    return {
        "total": sum(counts.values()),
        "halt_count": counts.get("halt", 0),
        "station_count": counts.get("station", 0),
        "source": _store.meta("catalog_source") or "empty",
    }


@app.get("/halts")
async def halts(
    lat: float | None = Query(None, ge=-90, le=90, allow_inf_nan=False),
    lon: float | None = Query(None, ge=-180, le=180, allow_inf_nan=False),
    km: float = Query(25, allow_inf_nan=False),
    q: str = Query(""),
    kind: str = Query(""),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=2000),
    all: bool = Query(False),
) -> dict:
    assert _session is not None
    await ensure_catalog(_session, _store)
    if kind not in {"", "halt", "station"}:
        raise HTTPException(status_code=400, detail="kind must be halt or station")
    if (lat is None) != (lon is None):
        raise HTTPException(status_code=400, detail="Pass both lat and lon")
    body = _catalog_meta()
    if lat is not None and lon is not None:
        radius = min(80.0, max(1.0, km))
        found = _store.halts_near(lat, lon, radius, limit, kind)
        body["returned"] = len(found)
        body["halts"] = [_halt_row(halt, distance) for halt, distance in found]
        return body
    query = q.strip()
    if query:
        if len(query) < 2:
            raise HTTPException(status_code=400, detail="Search needs at least 2 letters")
        found = _store.search_halts(query, limit, kind)
        body["returned"] = len(found)
        body["halts"] = [_halt_row(halt) for halt in found]
        return body
    if all:
        page = _store.halts_page(limit, offset, kind)
        body["offset"] = offset
        body["returned"] = len(page)
        body["halts"] = [_halt_row(halt) for halt in page]
        return body
    body["halts"] = []
    body["returned"] = 0
    return body


@app.get("/halts/{code}")
async def halt(code: str) -> dict:
    assert _session is not None
    await ensure_catalog(_session, _store)
    cleaned = clean_code(code)
    if not cleaned:
        raise HTTPException(status_code=400, detail="Unknown station code")
    found = _store.halt(cleaned)
    if found is None:
        raise HTTPException(status_code=404, detail="Halt not in the catalog")
    return _halt_row(found)


@app.get("/gates")
async def gates(
    lat: float = Query(..., ge=-90, le=90, allow_inf_nan=False),
    lon: float = Query(..., ge=-180, le=180, allow_inf_nan=False),
) -> dict:
    assert _service is not None
    mapped = await _service.map_around(lat, lon)
    if mapped.error:
        return {"error": mapped.error, "gates": []}
    return {
        "gates": [
            {"id": c.id, "name": gate_label(c), "lat": c.lat, "lon": c.lon, "km": round(d, 3), "source": c.source,
             "road": c.road, "area": c.area, "group_id": c.group_id,
             "group_name": c.group_name, "group_size": c.group_size}
            for c, d in crossing_distances(mapped, lat, lon)
        ],
        **mapped.freshness(),
    }


def _caller(request: Request) -> str:
    # The server applies forwarding headers only for its configured proxy peers.
    if request.client:
        return request.client.host
    return "unknown"


def _limit(request: Request, bucket: str = "predictions", cap: int = _LIMIT) -> None:
    global _HITS_PRUNED_AT
    ip = f"{bucket}:{_caller(request)}"
    now = time.monotonic()
    if now - _HITS_PRUNED_AT >= 60:
        for key in [key for key, hits in _HITS.items() if not hits or now - hits[-1] >= _WINDOW]:
            del _HITS[key]
        _HITS_PRUNED_AT = now
    if ip not in _HITS and len(_HITS) >= _HITS_CAPACITY:
        raise HTTPException(status_code=429, detail="Checks are busy. Please try again shortly.",
                            headers={"Retry-After": "60"})
    recent = [stamp for stamp in _HITS.get(ip, ()) if now - stamp < _WINDOW]
    if len(recent) >= cap:
        retry = max(1, math.ceil(_WINDOW - (now - recent[0])))
        raise HTTPException(status_code=429, detail="Slow down and try again in a few minutes.", headers={"Retry-After": str(retry)})
    recent.append(now)
    _HITS[ip] = recent


@app.get("/predict")
async def predict(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, allow_inf_nan=False),
    lon: float = Query(..., ge=-180, le=180, allow_inf_nan=False),
) -> dict:
    _limit(request)
    assert _service is not None
    return await _service.predict_here(lat, lon)


@app.get("/scene")
async def scene(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, allow_inf_nan=False),
    lon: float = Query(..., ge=-180, le=180, allow_inf_nan=False),
    crossing_id: str = Query(..., min_length=1, max_length=120),
) -> dict:
    _limit(request)
    assert _service is not None
    return await _service.scene_here(lat, lon, crossing_id)


class GateReport(BaseModel):
    crossing_id: str = Field(min_length=1, max_length=120)
    installation_id: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9-]+$")
    state: Literal["open", "closed", "wrong_location"]
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


@app.get("/reports")
async def reports(crossing_id: str = Query(..., min_length=1, max_length=120)) -> dict:
    canonical_id = _store.canonical_crossing_id(crossing_id)
    if canonical_id.startswith("pin:") and not _store.is_public_pin(canonical_id):
        return _reports.empty()
    return _reports.recent(canonical_id)


@app.post("/reports")
async def report(request: Request, body: GateReport) -> dict:
    _limit(request, "reports", 10)
    assert _service is not None
    mapped = await _service.map_around(body.lat, body.lon)
    if mapped.error or getattr(mapped, "map_warning", None):
        raise HTTPException(status_code=503, detail="Crossing could not be verified. Try again later.")
    canonical_id = _store.canonical_crossing_id(body.crossing_id)
    if not any(gate.id == canonical_id for gate in mapped.crossings):
        raise HTTPException(status_code=404, detail="Crossing not found at this location.")
    return _reports.submit(canonical_id, body.installation_id, body.state)
