import asyncio
import sqlite3
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from gateahead.clock import INDIA
from gateahead.models import Crossing
from gateahead.phone_alerts import (
    AlertCandidate,
    PhoneAlertCapacity,
    PhoneAlertChallenge,
    PhoneAlertConfig,
    PhoneAlertConflict,
    PhoneAlertPoller,
    PhoneAlertProofRequired,
    PhoneAlertRegistration,
    PhoneAlertStore,
    SavedGateAlert,
    eligible_predictions,
)
from gateahead.push_delivery import FCMPushDelivery

DEVICE = "a1" * 32
FID = "fcm_registration_id001"
GATE = SavedGateAlert(
    crossing_id="osm:node:1234", name="Office crossing", lat=12.345, lon=75.234,
    lead_minutes=5, start_minute=0, end_minute=0, weekdays_only=False,
)
CONFIG = PhoneAlertConfig()


def enrollment(store, revision=1, *, device=DEVICE, fid=FID, gates=None):
    proof_request = PhoneAlertChallenge(device_key=device, fid=fid, revision=revision)
    challenge_id, proof, _ = store.issue_challenge(proof_request)
    return PhoneAlertRegistration(device_key=device, fid=fid, gates=gates if gates is not None else [GATE],
                                  revision=revision, challenge_id=challenge_id, challenge_response=proof)


def enroll(store, **kwargs):
    registration = enrollment(store, **kwargs)
    store.replace(registration, registration.gates, CONFIG)
    return registration


def test_overnight_alert_uses_starting_weekday_in_india():
    gate = GATE.model_copy(update={"start_minute": 22 * 60, "end_minute": 5 * 60, "weekdays_only": True})
    assert gate.allows(datetime(2026, 11, 20, 22, 0, tzinfo=INDIA))
    friday = datetime(2026, 11, 21, 4, 59, tzinfo=INDIA)
    assert gate.active_until(friday) == datetime(2026, 11, 21, 5, 0, tzinfo=INDIA)
    assert not gate.allows(datetime(2026, 11, 21, 5, 0, tzinfo=INDIA))
    assert not gate.allows(datetime(2026, 11, 21, 22, 0, tzinfo=INDIA))
    assert not gate.allows(datetime(2026, 11, 23, 4, 59, tzinfo=INDIA))
    assert GATE.active_until(datetime(2026, 11, 21, 23, 59, tzinfo=INDIA)).day == 22


def test_registration_requires_proof_and_canonical_crossing_and_revokes_while_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path))
    import gateahead.api.app as api

    store = PhoneAlertStore(tmp_path / "phone.db")
    monkeypatch.setattr(api, "_phone_alerts", store)
    monkeypatch.setattr(api, "_HITS", {})
    monkeypatch.setattr(api, "_phone_poll", None)
    monkeypatch.setattr(api, "_phone_config", None)
    monkeypatch.setattr(api, "_phone_delivery", None)
    body = {"device_key": DEVICE, "fid": FID, "gates": [GATE.model_dump()], "revision": 1}
    crossing = Crossing(GATE.crossing_id, "Public Road", 12.3451, 75.234, source="osm")
    proofs = {}

    async def challenge(fid, challenge_id, response, deadline):
        proofs[challenge_id] = response
        return "sent"

    async def map_around(lat, lon):
        return SimpleNamespace(error=None, map_warning=None, crossings=[crossing])

    monkeypatch.setattr(api, "_service", SimpleNamespace(map_around=map_around))

    async def run():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/phone-alerts")).json() == {"enabled": False, "status": "disabled"}
            assert (await client.post("/phone-alerts", json=body)).status_code == 503
            monkeypatch.setattr(api, "_phone_poll", SimpleNamespace(done=lambda: False))
            monkeypatch.setattr(api, "_phone_config", CONFIG)
            monkeypatch.setattr(api, "_phone_delivery", SimpleNamespace(challenge=challenge))
            assert (await client.post("/phone-alerts", json=body)).status_code == 428
            challenge_request = {key: body[key] for key in ("device_key", "fid", "revision")}
            response = await client.post("/phone-alerts/challenge", json=challenge_request)
            challenge_id = response.json()["challenge_id"]
            assert response.status_code == 200
            assert proofs[challenge_id] not in response.text

            proven = {**body, "challenge_id": challenge_id, "challenge_response": proofs[challenge_id]}
            result = (await client.post("/phone-alerts", json=proven)).json()
            assert result["registered"] == result["revision"] == 1
            assert result["fid_verified"] is True
            assert store.active()[0].gates[0].lat == crossing.lat
            assert store.active()[0].gates[0].name == crossing.name
            with sqlite3.connect(store.path) as db:
                assert db.execute("SELECT hex(device) FROM phone_registrations").fetchone()[0].lower() != DEVICE
            retry = await client.post("/phone-alerts", json=proven)
            assert retry.status_code == 200
            assert retry.json()["revision"] == 1

            unknown = {**GATE.model_dump(), "crossing_id": "osm:node:9999"}
            assert (await client.post("/phone-alerts", json={**body, "revision": 2, "gates": [unknown]})).status_code == 404
            changed = GATE.model_copy(update={"lead_minutes": 15})
            saved = await client.post("/phone-alerts", json={**body, "revision": 2, "gates": [changed.model_dump()]})
            assert saved.json()["revision"] == 2
            assert store.active()[0].gates[0].lead_minutes == 15

            token = store.start_poll(CONFIG)
            store.finish_poll(token, 0)
            store.provider_success(time.time())
            status = await client.post("/phone-alerts/status", json={"device_key": DEVICE, "fid": FID})
            assert status.json()["status"] == "ready" and status.json()["fid_verified"]
            store.provider_failure("oauth", 60)
            availability = (await client.get("/phone-alerts")).json()
            assert availability["status"] == "degraded" and availability["provider_failures"] == 1
            assert availability["retry_after_seconds"] > 0 and "oauth" not in availability.values()

            monkeypatch.setattr(api, "_phone_poll", None)
            revoke = {"device_key": DEVICE, "fid": None, "gates": [], "revision": 3}
            ack = (await client.post("/phone-alerts", json=revoke)).json()
            assert ack == {"enabled": False, "status": "disabled", "registered": 0,
                           "fid_verified": False, "revision": 3}
            assert store.active() == []

    asyncio.run(run())


def test_first_revocation_blocks_delayed_challenge_and_settings(tmp_path):
    store = PhoneAlertStore(tmp_path / "phone.db")
    empty = PhoneAlertRegistration(device_key=DEVICE, revision=2, gates=[])
    store.replace(empty, [], CONFIG)
    with pytest.raises(PhoneAlertConflict) as error:
        enrollment(store, revision=1)
    assert error.value.revision == 2

    pending = enrollment(store, revision=3)
    newer = PhoneAlertRegistration(device_key=DEVICE, revision=4, gates=[])
    store.replace(newer, [], CONFIG)
    with pytest.raises(PhoneAlertConflict) as error:
        store.replace(pending, [GATE], CONFIG)
    assert error.value.revision == 4
    assert not store.active()


def test_capacity_counts_distinct_areas_and_reuses_shared_area(tmp_path):
    store = PhoneAlertStore(tmp_path / "phone.db")
    config = PhoneAlertConfig(max_areas=1, max_subscriptions=2)
    first = enrollment(store)
    store.replace(first, [GATE], config)
    distant = GATE.model_copy(update={"crossing_id": "osm:node:5678", "lat": 13.5})
    rejected = enrollment(store, device="b2" * 32, fid=FID + "b", gates=[distant])
    with pytest.raises(PhoneAlertCapacity):
        store.replace(rejected, [distant], config)
    nearby = distant.model_copy(update={"lat": GATE.lat})
    accepted = rejected.model_copy(update={"gates": [nearby]})
    store.replace(accepted, [nearby], config)
    assert len(store.active()) == 2
    excess = enrollment(store, device="c3" * 32, fid=FID + "c")
    with pytest.raises(PhoneAlertCapacity):
        store.replace(excess, [GATE], config)


def test_poll_starts_every_admitted_area_before_waiting_for_results(tmp_path):
    store = PhoneAlertStore(tmp_path / "phone.db")
    for index in range(CONFIG.max_areas):
        gate = GATE.model_copy(update={"lat": GATE.lat + index * 0.02, "crossing_id": f"osm:node:{index + 1}"})
        enroll(store, device=f"{index + 1:064x}", fid=f"{FID}{index}", gates=[gate])

    async def run():
        scanned = set()
        all_started = asyncio.Event()

        class Service:
            async def predict_here(self, lat, lon, *, budget_seconds, alert_gate_ids):
                assert alert_gate_ids and budget_seconds < CONFIG.scan_seconds
                scanned.add((lat, lon))
                if len(scanned) == CONFIG.max_areas:
                    all_started.set()
                await all_started.wait()
                return {"generated_at": datetime.now(INDIA).isoformat()}

        poller = PhoneAlertPoller(Service(), store, None, CONFIG)
        assert await asyncio.wait_for(poller.poll(), 3) == 0
        assert len(scanned) == CONFIG.max_areas

    asyncio.run(run())


def test_old_claim_expires_and_undated_arrivals_can_notify_on_later_day(tmp_path, monkeypatch):
    store = PhoneAlertStore(tmp_path / "phone.db")
    instant = [time.time()]
    monkeypatch.setattr("gateahead.phone_alerts.time.time", lambda: instant[0])
    registration = enroll(store)
    phone, = store.active()
    run = AlertCandidate(GATE.crossing_id, "16512", "undated:16512", instant[0] + 120, instant[0] + 240)
    first = store.claim(phone, run, run.closes_at)
    assert first and store.claim(phone, run, run.closes_at) is None
    assert store.begin_dispatch(phone, first, run.closes_at)
    store.finish(phone, run, first, "uncertain")
    assert not store.has_dispatch(DEVICE)
    store = PhoneAlertStore(store.path)
    assert store.claim(phone, run, run.closes_at) is None
    instant[0] += 36
    recovered = store.claim(phone, run, run.closes_at)
    assert recovered and recovered != first
    store.finish(phone, run, first, "retry")
    assert store.begin_dispatch(phone, recovered, run.closes_at)
    store.finish(phone, run, recovered, "sent")
    assert store.claim(phone, run, run.closes_at) is None
    instant[0] += 86_400
    assert store.claim(phone, run, instant[0] + 100)
    unregistered = registration.model_copy(update={"gates": [], "revision": 2})
    store.replace(unregistered, [], CONFIG)
    assert not store.begin_dispatch(phone, recovered, instant[0] + 100)


def test_poll_keeps_absolute_freshness_and_checks_current_subscription_after_oauth(tmp_path):
    store = PhoneAlertStore(tmp_path / "phone.db")
    registration = enroll(store)
    phone, = store.active()
    calls = []

    class Service:
        async def predict_here(self, lat, lon, *, budget_seconds, alert_gate_ids):
            assert budget_seconds < CONFIG.scan_seconds
            assert alert_gate_ids == {GATE.crossing_id}
            calls.append((lat, lon))
            return {
                "generated_at": datetime.now(INDIA).isoformat(),
                "gate_predictions": [
                    {"id": GATE.crossing_id, "train": "16512", "start_date": "2026-11-24",
                     "closes_in": 1.1, "age_min": 7, "timing_confident": True, "observed": True},
                ],
            }

    class Delivery:
        async def probe(self):
            return True

        async def send(self, fid, gate, candidate, deadline, authorize):
            assert candidate.fresh_until < candidate.closes_at
            assert deadline == candidate.fresh_until
            revoke = registration.model_copy(update={"gates": [], "revision": 2})
            await asyncio.to_thread(store.replace, revoke, [], CONFIG)
            assert not await authorize()
            return "dropped"

    async def run():
        poller = PhoneAlertPoller(Service(), store, Delivery(), CONFIG)
        assert await poller.poll() == 0
        assert not store.active()
        assert calls == [GATE.area]
        assert not store.has_dispatch(DEVICE)

    asyncio.run(run())


def test_stale_absolute_source_and_undated_identity():
    now = datetime.now(INDIA)
    checked = now.timestamp()
    close = {"id": GATE.crossing_id, "train": "16512", "closes_in": 2, "age_min": 7,
             "timing_confident": True, "observed": True, "timing_as_of": checked - 70}
    result = {"generated_at": now.isoformat(), "gate_predictions": [close]}
    assert not eligible_predictions(result, now)
    fresh = {**close, "timing_as_of": checked, "alert_fresh_until": checked + 60}
    result["gate_predictions"] = [fresh]
    candidate, = eligible_predictions(result, now)[GATE.crossing_id]
    assert candidate.run_id == "undated:16512"
    assert not eligible_predictions(result, now + timedelta(seconds=61))


def test_outgoing_payload_preserves_distinct_gate_alerts_and_uses_absolute_times():
    now = time.time()
    candidate = AlertCandidate(GATE.crossing_id, "16512", "16512:2026-11-24", now + 29, now + 22)
    message = FCMPushDelivery._message(FID, GATE, candidate, now + 22)
    assert message["fid"] == FID
    assert "notification" not in message and "collapse_key" not in message["android"]
    assert message["apns"]["payload"]["aps"]["alert"]["title"] == "GateAhead crossing alert"
    collapse_id = message["apns"]["headers"]["apns-collapse-id"]
    assert len(collapse_id) == 64
    assert FCMPushDelivery._message(FID, GATE, candidate, now + 19)["apns"]["headers"]["apns-collapse-id"] == collapse_id
    other_gate = GATE.model_copy(update={"crossing_id": "osm:node:789"})
    assert FCMPushDelivery._message(FID, other_gate, candidate, now + 22)["apns"]["headers"]["apns-collapse-id"] != collapse_id
    assert int(message["data"]["notification_id"]) > 0
    assert int(message["data"]["expires_at"]) <= now + 22
    assert "IST" in message["data"]["body"]
    assert parse_qs(urlsplit(message["data"]["crossing_link"]).query) == {
        "lat": ["12.345"], "lon": ["75.234"], "gate": ["osm:node:1234"],
    }


def test_fcm_retry_after_persists_across_restarts(tmp_path, monkeypatch):
    store = PhoneAlertStore(tmp_path / "phone.db")

    class Response:
        status = 503
        headers = {"Retry-After": "600"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self, **kwargs):
            return {}

    def post(*args, **kwargs):
        assert kwargs["allow_redirects"] is False
        return Response()

    async def token():
        return "fixture"

    async def run():
        dispatcher = FCMPushDelivery(SimpleNamespace(post=post), "test-project", None, store)
        monkeypatch.setattr(dispatcher, "_access_token", token)
        assert await dispatcher.challenge(FID, "sent-proof-id", "secret", time.time() + 20) == "retry"
        health = PhoneAlertStore(store.path).provider_health()
        assert health["failures"] == 1 and health["retry_at"] >= time.time() + 599

    asyncio.run(run())


def test_capacity_configuration_rejects_an_unbounded_poll_interval(monkeypatch):
    monkeypatch.setenv("GATEAHEAD_PHONE_ALERT_POLL_SECONDS", "500")
    with pytest.raises(ValueError, match="GATEAHEAD_PHONE_ALERT_POLL_SECONDS"):
        PhoneAlertConfig.from_environment()


def test_proof_is_single_mutation_and_retention_runs_outside_poll(tmp_path, monkeypatch):
    store = PhoneAlertStore(tmp_path / "phone.db")
    clock = [time.time()]
    monkeypatch.setattr("gateahead.phone_alerts.time.time", lambda: clock[0])
    saved = enroll(store)
    with pytest.raises(PhoneAlertProofRequired):
        store.replace(saved.model_copy(update={"fid": FID + "new", "revision": 2}), [GATE], CONFIG)
    clock[0] += 46 * 86_400
    assert store.status(saved)["registered"] == 0
    store.prune()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM phone_registrations").fetchone()[0] == 0
