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
from gateahead.crossing_details import record_crossing_flag
from gateahead.geo import km
from gateahead.halts import clean_code, ensure_catalog
from gateahead.models import Crossing
from gateahead.osm import OsmAround
from gateahead.phone_alerts import (
    PhoneAlertCapacity,
    PhoneAlertChallenge,
    PhoneAlertConfig,
    PhoneAlertConflict,
    PhoneAlertDevice,
    PhoneAlertPoller,
    PhoneAlertProofRequired,
    PhoneAlertRegistration,
    PhoneAlertStore,
    SavedGateAlert,
)
from gateahead.place import lookup_place
from gateahead.push_delivery import FCMPushDelivery
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
_phone_alerts = PhoneAlertStore(_settings.db_path)
_session: aiohttp.ClientSession | None = None
_service: GateAhead | None = None
_maintenance: asyncio.Task | None = None
_phone_poll: asyncio.Task | None = None
_phone_config: PhoneAlertConfig | None = None
_phone_delivery: FCMPushDelivery | None = None
_phone_map_slots = asyncio.Semaphore(6)
_log = logging.getLogger(__name__)


async def _maintain_storage() -> None:
    while True:
        try:
            _store.prune_retained_data()
            await asyncio.to_thread(_phone_alerts.prune)
        except sqlite3.OperationalError:
            _log.exception("Deferred database cleanup until the next maintenance interval")
        await asyncio.sleep(60)


async def startup() -> None:
    global _session, _service, _maintenance, _phone_poll, _phone_config, _phone_delivery
    _session = aiohttp.ClientSession()
    _service = GateAhead(_settings, _store, _session)
    await ensure_catalog(_session, _store)
    _maintenance = asyncio.create_task(_maintain_storage())
    if os.getenv("GATEAHEAD_FCM_PROJECT_ID", "").strip():
        try:
            _phone_config = PhoneAlertConfig.from_environment()
        except ValueError as exc:
            _log.error("Saved crossing alerts disabled. %s", exc)
            return
        _phone_delivery = await FCMPushDelivery.from_environment(_session, _phone_alerts)
        if _phone_delivery is not None:
            _phone_poll = asyncio.create_task(
                PhoneAlertPoller(_service, _phone_alerts, _phone_delivery, _phone_config).run()
            )


async def shutdown() -> None:
    global _maintenance, _service, _session, _phone_poll, _phone_config, _phone_delivery
    if _phone_poll:
        _phone_poll.cancel()
        await asyncio.gather(_phone_poll, return_exceptions=True)
        _phone_poll = None
    if _maintenance:
        _maintenance.cancel()
        await asyncio.gather(_maintenance, return_exceptions=True)
    if _service:
        await _service.snapshots.shutdown()
    if _session:
        await _session.close()
    _maintenance = _service = _session = None
    _phone_config = _phone_delivery = None


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "database_id": _store.meta("database_id"), "providers": _store.provider_status(),
            "worker": _store.worker_status(), "worker_required": os.getenv("GATEAHEAD_WORKER_REQUIRED") == "1",
            "phone_alerts": await phone_alerts_available()}


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


@app.get("/phone-alerts")
async def phone_alerts_available() -> dict:
    if not _phone_config or not _phone_delivery or not _phone_poll or _phone_poll.done():
        return {"enabled": False, "status": "disabled"}
    worker, provider = await asyncio.gather(
        asyncio.to_thread(_phone_alerts.worker_health), asyncio.to_thread(_phone_alerts.provider_health)
    )
    now = time.time()
    last_poll = worker["last_success"]
    lag = max(0, round(now - last_poll)) if last_poll else None
    overdue = (lag > 2 * _phone_config.interval if lag is not None
               else worker["next_poll"] and now - worker["next_poll"] > _phone_config.interval)
    if worker["failed_areas"] or provider["failures"] or overdue:
        status = "degraded"
    else:
        status = "ready" if last_poll and provider["last_success"] else "starting"
    return {
        "enabled": True, "status": status, "last_poll_age_seconds": lag,
        "failed_areas": worker["failed_areas"], "provider_failures": provider["failures"],
        "retry_after_seconds": max(0, math.ceil(provider["retry_at"] - now)),
    }


@app.post("/phone-alerts/status")
async def phone_alert_status(request: Request, body: PhoneAlertDevice) -> dict:
    _limit(request, "phone-alert-status", 60)
    requested, availability = await asyncio.gather(
        asyncio.to_thread(_phone_alerts.status, body), phone_alerts_available()
    )
    return {**availability, **requested}


def _phone_conflict(exc: PhoneAlertConflict) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "message": "Phone alert settings changed. Refresh before saving.", "revision": exc.revision,
    })


@app.post("/phone-alerts/challenge")
async def phone_alert_challenge(request: Request, body: PhoneAlertChallenge) -> dict:
    _limit(request, "phone-alert-challenge", 8)
    availability = await phone_alerts_available()
    if not availability["enabled"] or _phone_delivery is None:
        raise HTTPException(status_code=503, detail="Phone alerts are unavailable on this server.")
    if availability["retry_after_seconds"]:
        raise HTTPException(status_code=503, detail="Firebase delivery is temporarily unavailable.",
                            headers={"Retry-After": str(availability["retry_after_seconds"])})
    try:
        challenge_id, proof, deadline = await asyncio.to_thread(_phone_alerts.issue_challenge, body)
    except PhoneAlertConflict as exc:
        raise _phone_conflict(exc) from exc
    except PhoneAlertCapacity as exc:
        raise HTTPException(status_code=429, detail="Wait before requesting another registration check.",
                            headers={"Retry-After": "120"}) from exc
    outcome = await _phone_delivery.challenge(body.fid, challenge_id, proof, deadline)
    if outcome != "sent":
        await asyncio.to_thread(_phone_alerts.close_challenge, challenge_id)
        if outcome in ("unregistered", "rejected"):
            raise HTTPException(status_code=422, detail="Firebase could not reach this installation.")
        raise HTTPException(status_code=503, detail="Firebase delivery is temporarily unavailable.")
    return {"challenge_id": challenge_id, "expires_in": max(1, int(deadline - time.time()))}


@app.post("/phone-alerts")
async def update_phone_alerts(request: Request, body: PhoneAlertRegistration) -> dict:
    _limit(request, "phone-alerts" if body.gates else "phone-alert-revoke", 30)
    config = _phone_config
    if body.gates and (not config or not (await phone_alerts_available())["enabled"]):
        raise HTTPException(status_code=503, detail="Phone alerts are unavailable on this server.")
    try:
        accepted, previous = await asyncio.to_thread(_phone_alerts.preflight, body)
        if not accepted:
            gates = await _verify_alert_crossings(body.gates, previous)
            await asyncio.to_thread(_phone_alerts.replace, body, gates, config or PhoneAlertConfig())
    except PhoneAlertConflict as exc:
        raise _phone_conflict(exc) from exc
    except PhoneAlertProofRequired as exc:
        raise HTTPException(status_code=428, detail={
            "code": "proof_required", "message": "Verify this phone before saving alerts.",
        }) from exc
    except PhoneAlertCapacity as exc:
        raise HTTPException(status_code=409, detail={
            "code": "capacity", "message": "This service has no capacity for the selected alert coverage.",
        }) from exc
    if not body.gates:
        # The revocation is durable before waiting. Any older dispatch must
        # drain before we return a confirmed turn-off, across API workers.
        try:
            async with asyncio.timeout(13):
                while await asyncio.to_thread(_phone_alerts.has_dispatch, body.device_key):
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise HTTPException(status_code=503, detail="Settings were saved. Confirm turn-off again shortly.") from None
    return {**(await phone_alerts_available()), **(await asyncio.to_thread(_phone_alerts.status, body))}


async def _verify_alert_crossings(
    gates: list[SavedGateAlert], previous: list[SavedGateAlert],
) -> list[SavedGateAlert]:
    known = {gate.crossing_id: gate for gate in previous}
    ids = set()
    resolved = []
    for gate in gates:
        crossing_id = _store.canonical_crossing_id(gate.crossing_id)
        if crossing_id.startswith("user:") or (
                crossing_id.startswith("pin:") and not _store.is_public_pin(crossing_id)):
            raise HTTPException(status_code=404, detail="A saved crossing could not be verified.")
        if crossing_id in ids:
            raise HTTPException(status_code=422, detail="Choose each crossing once.")
        ids.add(crossing_id)
        resolved.append(gate.model_copy(update={"crossing_id": crossing_id}))

    async def verify(gate):
        old = known.get(gate.crossing_id)
        point = (gate.lat, gate.lon)
        if old and km((old.lat, old.lon), point) <= 0.3:
            return gate.model_copy(update={"lat": old.lat, "lon": old.lon, "name": old.name})
        assert _service is not None
        async with _phone_map_slots:
            mapped = await _service.map_around(*point)
        if mapped.error or mapped.map_warning:
            raise HTTPException(status_code=503, detail="Current crossing map data is unavailable.")
        crossing = next((crossing for crossing in mapped.crossings
                         if crossing.id == gate.crossing_id and crossing.status == "public"), None)
        if crossing is None or km(crossing.coords, point) > 0.3:
            raise HTTPException(status_code=404, detail="A saved crossing could not be verified.")
        return gate.model_copy(update={"lat": crossing.lat, "lon": crossing.lon, "name": gate_label(crossing)[:640]})

    if not resolved:
        return []
    tasks = [asyncio.create_task(verify(gate)) for gate in resolved]
    try:
        async with asyncio.timeout(76):
            return list(await asyncio.gather(*tasks))
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Current crossing map data is unavailable.") from None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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


class CrossingSubmission(BaseModel):
    crossing_id: str = Field(min_length=1, max_length=120)
    installation_id: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9-]+$")
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


class GateReport(CrossingSubmission):
    state: Literal["open", "closed", "wrong_location"]


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


async def _details_crossing(
    lat: float, lon: float, crossing_id: str, *, require_fresh: bool,
) -> tuple[Crossing, OsmAround]:
    assert _service is not None
    try:
        async with asyncio.timeout(28):
            mapped = await _service.map_around(lat, lon)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Crossing map is busy. Try again shortly.") from exc
    if mapped.error or (require_fresh and mapped.map_warning):
        raise HTTPException(status_code=503, detail="Crossing could not be verified. Try again later.")
    canonical_id = _store.canonical_crossing_id(crossing_id)
    gate = next((gate for gate in mapped.crossings if gate.id == canonical_id), None)
    if gate is None or km(gate.coords, (lat, lon)) > 0.3:
        if mapped.map_warning:
            raise HTTPException(status_code=503, detail="Crossing could not be verified. Try again later.")
        raise HTTPException(status_code=404, detail="Crossing not found at this location.")
    return gate, mapped


@app.get("/crossing-details")
async def crossing_details(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, allow_inf_nan=False),
    lon: float = Query(..., ge=-180, le=180, allow_inf_nan=False),
    crossing_id: str = Query(..., min_length=1, max_length=120),
) -> dict:
    _limit(request, "crossing-details", 60)
    try:
        gate, mapped = await _details_crossing(lat, lon, crossing_id, require_fresh=False)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        raise HTTPException(
            status_code=404,
            detail={"code": "crossing_not_found", "message": exc.detail},
        ) from exc
    assert _service is not None
    return {
        "crossing": {"id": gate.id, "name": gate_label(gate), "lat": gate.lat, "lon": gate.lon},
        "crossing_access": _service.crossing_details.for_crossing(gate),
        **({"legacy_alias_resolved": True} if gate.id != crossing_id else {}),
        **mapped.freshness(),
    }


@app.post("/crossing-details/flags", status_code=202)
async def flag_crossing_details(request: Request, body: CrossingSubmission) -> dict:
    _limit(request, "crossing-detail-flags", 10)
    gate, _ = await _details_crossing(body.lat, body.lon, body.crossing_id, require_fresh=True)
    try:
        await asyncio.to_thread(record_crossing_flag, _store.path, gate.id, body.installation_id)
    except sqlite3.OperationalError as exc:
        error_code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if error_code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise
        raise HTTPException(status_code=503, detail="Reports are busy. Try again shortly.") from exc
    return {"accepted": True}
