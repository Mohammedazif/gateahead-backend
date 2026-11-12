from __future__ import annotations

import os
import time
from collections import defaultdict

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from gateahead.clusters import gate_label
from gateahead.config import load_settings
from gateahead.halts import clean_code, ensure_catalog
from gateahead.place import lookup_place
from gateahead.service import GateAhead
from gateahead.store import Store

app = FastAPI(title="GateAhead")
_origins = [item.strip() for item in os.getenv("WEB_ORIGIN", "http://localhost:5173").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)
_HITS: dict[str, list[float]] = defaultdict(list)
_LIMIT = 20
_WINDOW = 600.0
_settings = load_settings()
_store = Store(_settings.db_path)
_session: aiohttp.ClientSession | None = None
_service: GateAhead | None = None


@app.on_event("startup")
async def startup() -> None:
    global _session, _service
    _session = aiohttp.ClientSession()
    _service = GateAhead(_settings, _store, _session)
    await ensure_catalog(_session, _store)


@app.on_event("shutdown")
async def shutdown() -> None:
    if _session:
        await _session.close()


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/place")
async def place(lat: float = Query(...), lon: float = Query(...)) -> dict:
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
    lat: float | None = Query(None),
    lon: float | None = Query(None),
    km: float = Query(25),
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
async def gates(lat: float = Query(...), lon: float = Query(...)) -> dict:
    assert _service is not None
    items, error = await _service.nearby(lat, lon)
    if error:
        return {"error": error, "gates": []}
    return {
        "gates": [
            {"id": c.id, "name": gate_label(c), "lat": c.lat, "lon": c.lon, "km": round(d, 3), "source": c.source}
            for c, d in items
        ]
    }


def _caller(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client:
        return request.client.host
    return "unknown"


def _limit(request: Request) -> None:
    ip = _caller(request)
    now = time.monotonic()
    recent = [stamp for stamp in _HITS[ip] if now - stamp < _WINDOW]
    if len(recent) >= _LIMIT:
        raise HTTPException(status_code=429, detail="Slow down and try again in a few minutes.")
    recent.append(now)
    _HITS[ip] = recent


@app.get("/predict")
async def predict(request: Request, lat: float = Query(...), lon: float = Query(...)) -> dict:
    _limit(request)
    assert _service is not None
    return await _service.predict_here(lat, lon)


@app.get("/scene")
async def scene(
    request: Request,
    lat: float = Query(...),
    lon: float = Query(...),
    crossing_id: str = Query(...),
) -> dict:
    _limit(request)
    assert _service is not None
    return await _service.scene_here(lat, lon, crossing_id)
