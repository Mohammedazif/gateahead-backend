from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from gateahead.clock import INDIA
from gateahead.predict import REPORT_FRESH_MIN

_log = logging.getLogger(__name__)
_DAY = 86_400
_REGISTRATION_TTL = 45 * _DAY
_DELIVERY_TTL = 7 * _DAY
# Actual departure dates are preferred. While a provider supplies only a train
# number, suppress duplicate observations for a short window, including midnight.
_UNDATED_TTL = 2 * 3600
_CHALLENGE_TTL = 120
_CLAIM_LEASE = 35
_DISPATCH_LEASE = 12
_DELIVERY_CONCURRENCY = 12
_ALLOWED_ID = r"^[A-Za-z0-9_-]{16,128}$"
Revision = Annotated[int, Field(strict=True, ge=1, le=9_007_199_254_740_991)]


class SavedGateAlert(BaseModel):
    crossing_id: str = Field(
        pattern=r"^(?:osm:(?:node:)?[0-9]+|ir:[0-9]+|pin:[0-9a-f]{32}|user:[0-9]+:[0-9]+)$",
        max_length=120,
    )
    # The stored value comes from the verified map. Retain compatibility with
    # clients that used to send their own gate label.
    name: str = Field(default="", max_length=640)
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    lead_minutes: Literal[5, 10, 15]
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(ge=0, lt=1440)
    weekdays_only: bool

    @property
    def area(self) -> tuple[float, float]:
        return round(self.lat, 2), round(self.lon, 2)

    def active_until(self, now: datetime) -> datetime | None:
        now = now.astimezone(INDIA)
        today = now.date()
        minute = now.hour * 60 + now.minute
        start, end = self.start_minute, self.end_minute
        if start < end and not start <= minute < end:
            return None
        day = today
        finish = today
        if start > end:
            if end <= minute < start:
                return None
            if minute < end:
                day -= timedelta(days=1)
            else:
                finish += timedelta(days=1)
        if self.weekdays_only and day.weekday() >= 5:
            return None
        if start == end:
            if not self.weekdays_only:
                return now + timedelta(days=1)
            # A full-day subscription continues across midnight except when
            # entering the weekend. The actual transport TTL is much shorter.
            finish += timedelta(days=5 - day.weekday())
            end = 0
        return datetime.combine(finish, wall_time(), INDIA) + timedelta(minutes=end)

    def allows(self, now: datetime) -> bool:
        return self.active_until(now) is not None


class PhoneAlertDevice(BaseModel):
    device_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    fid: str | None = Field(default=None, pattern=_ALLOWED_ID)


class PhoneAlertChallenge(PhoneAlertDevice):
    fid: str = Field(pattern=_ALLOWED_ID)
    revision: Revision


class PhoneAlertRegistration(PhoneAlertDevice):
    revision: Revision
    challenge_id: str | None = Field(default=None, pattern=_ALLOWED_ID)
    challenge_response: str | None = Field(default=None, pattern=_ALLOWED_ID)
    gates: list[SavedGateAlert] = Field(max_length=5)

    @field_validator("gates")
    @classmethod
    def unique_crossings(cls, gates: list[SavedGateAlert]) -> list[SavedGateAlert]:
        if len({gate.crossing_id for gate in gates}) != len(gates):
            raise ValueError("Choose each crossing once")
        return gates

    @model_validator(mode="after")
    def check_registration(self) -> PhoneAlertRegistration:
        if self.gates and not self.fid:
            raise ValueError("A notification registration is required")
        if bool(self.challenge_id) != bool(self.challenge_response):
            raise ValueError("The challenge ID and response are required together")
        return self

    def digest(self) -> bytes:
        gates = sorted(self.gates, key=lambda gate: gate.crossing_id)
        return _digest(json.dumps({"fid": self.fid if self.gates else None,
                                  "gates": [gate.model_dump(exclude={"name"}) for gate in gates]},
                                 sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class PhoneAlertConfig:
    interval: int = 120
    max_areas: int = 6
    max_phones: int = 60
    scan_seconds: int = 45
    max_subscriptions: int = 60

    def fits(self, phones: int, areas: int, subscriptions: int) -> bool:
        return (phones <= self.max_phones and areas <= self.max_areas
                and subscriptions <= self.max_subscriptions)

    @classmethod
    def from_environment(cls) -> PhoneAlertConfig:
        def value(name, default, minimum, maximum):
            raw = os.getenv(name, str(default))
            try:
                number = int(raw)
            except ValueError:
                raise ValueError(f"{name} must be an integer") from None
            if not minimum <= number <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
            return number

        interval = value("GATEAHEAD_PHONE_ALERT_POLL_SECONDS", 120, 60, 180)
        scan_seconds = value("GATEAHEAD_PHONE_ALERT_SCAN_SECONDS", 45, 10, min(80, interval - 15))
        # Reserve bounded delivery slots after the worst configured scan for
        # one notification per gate in a poll. Leave time for transport setup.
        capacity = min(120, max(1, (interval - scan_seconds - 5) // 10) * _DELIVERY_CONCURRENCY)
        return cls(interval, value("GATEAHEAD_PHONE_ALERT_AREAS_PER_POLL", 6, 1, 12),
                   value("GATEAHEAD_PHONE_ALERT_MAX_PHONES", 60, 1, 120), scan_seconds,
                   value("GATEAHEAD_PHONE_ALERT_MAX_SUBSCRIPTIONS", min(60, capacity), 1, capacity))


@dataclass(frozen=True)
class RegisteredPhone:
    key: bytes
    fid: str
    gates: list[SavedGateAlert]
    revision: int
    verified_at: float


@dataclass(frozen=True)
class AlertCandidate:
    crossing_id: str
    train: str
    run_id: str
    closes_at: float
    fresh_until: float

    @property
    def undated(self) -> bool:
        return self.run_id.startswith("undated:")


class PhoneAlertConflict(Exception):
    def __init__(self, revision: int):
        self.revision = revision


class PhoneAlertProofRequired(Exception):
    pass


class PhoneAlertCapacity(Exception):
    pass


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class PhoneAlertStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS phone_registrations (
                  device BLOB PRIMARY KEY,
                  fid TEXT NOT NULL,
                  gates TEXT NOT NULL,
                  updated_at REAL NOT NULL,
                  verified_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS phone_alert_revisions (
                  device BLOB PRIMARY KEY,
                  revision INTEGER NOT NULL,
                  request_digest BLOB NOT NULL,
                  updated_at REAL NOT NULL,
                  anonymous INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS phone_alert_deliveries (
                  device BLOB NOT NULL REFERENCES phone_registrations(device) ON DELETE CASCADE,
                  crossing_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  accepted_at REAL,
                  lease_until REAL NOT NULL DEFAULT 0,
                  expires_at REAL NOT NULL DEFAULT 0,
                  lease_token TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY (device, crossing_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS phone_alert_challenges (
                  id TEXT PRIMARY KEY,
                  device BLOB NOT NULL,
                  fid TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  response_digest BLOB NOT NULL,
                  issued_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS phone_alert_dispatches (
                  token TEXT PRIMARY KEY,
                  device BLOB NOT NULL,
                  expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS phone_alert_worker (
                  id INTEGER PRIMARY KEY CHECK (id = 1),
                  token TEXT NOT NULL DEFAULT '',
                  next_poll REAL NOT NULL DEFAULT 0,
                  lease_until REAL NOT NULL DEFAULT 0,
                  last_success REAL NOT NULL DEFAULT 0,
                  failed_areas INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS phone_alert_provider (
                  id INTEGER PRIMARY KEY CHECK (id = 1),
                  retry_at REAL NOT NULL DEFAULT 0,
                  last_success REAL NOT NULL DEFAULT 0,
                  failures INTEGER NOT NULL DEFAULT 0,
                  error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS phone_registrations_age ON phone_registrations(updated_at);
                CREATE INDEX IF NOT EXISTS phone_deliveries_age ON phone_alert_deliveries(created_at);
                CREATE INDEX IF NOT EXISTS phone_challenges_age ON phone_alert_challenges(issued_at);
                CREATE INDEX IF NOT EXISTS phone_challenges_device ON phone_alert_challenges(device);
                CREATE INDEX IF NOT EXISTS phone_dispatches_device ON phone_alert_dispatches(device);
                INSERT OR IGNORE INTO phone_alert_worker(id) VALUES(1);
                INSERT OR IGNORE INTO phone_alert_provider(id) VALUES(1);
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in db.execute("PRAGMA table_info(phone_registrations)")}
                if "verified_at" not in columns:
                    db.execute("ALTER TABLE phone_registrations ADD COLUMN verified_at REAL NOT NULL DEFAULT 0")
                columns = {row[1] for row in db.execute("PRAGMA table_info(phone_alert_revisions)")}
                if "anonymous" not in columns:
                    db.execute("ALTER TABLE phone_alert_revisions ADD COLUMN anonymous INTEGER NOT NULL DEFAULT 0")
                columns = {row[1] for row in db.execute("PRAGMA table_info(phone_alert_deliveries)")}
                if "accepted_at" not in columns:
                    db.execute("ALTER TABLE phone_alert_deliveries ADD COLUMN accepted_at REAL")
                    db.execute("ALTER TABLE phone_alert_deliveries ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")
                    db.execute("ALTER TABLE phone_alert_deliveries ADD COLUMN expires_at REAL NOT NULL DEFAULT 0")
                    db.execute("ALTER TABLE phone_alert_deliveries ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''")
                    # Legacy claim rows were indistinguishable from accepted
                    # messages. Keep only the already dated ones for dedup.
                    db.execute("DELETE FROM phone_alert_deliveries WHERE instr(run_id, ':') = 0")
                    db.execute("UPDATE phone_alert_deliveries SET accepted_at=created_at, expires_at=created_at + ?",
                               (_DELIVERY_TTL,))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def prune(self) -> None:
        now = time.time()
        with self._write() as db:
            db.execute("DELETE FROM phone_registrations WHERE updated_at <= ? OR (verified_at > 0 AND verified_at <= ?)",
                       (now - _REGISTRATION_TTL, now - _REGISTRATION_TTL))
            db.execute("DELETE FROM phone_alert_deliveries WHERE expires_at <= ? AND lease_until <= ?", (now, now))
            db.execute("DELETE FROM phone_alert_challenges WHERE issued_at <= ?", (now - 3600,))
            db.execute("DELETE FROM phone_alert_dispatches WHERE expires_at <= ?", (now,))
            db.execute("""
                DELETE FROM phone_alert_revisions WHERE updated_at <= ?
                AND NOT EXISTS (SELECT 1 FROM phone_registrations p WHERE p.device=phone_alert_revisions.device)
            """, (now - _REGISTRATION_TTL,))

    @staticmethod
    def _revision(db, key):
        return db.execute("SELECT * FROM phone_alert_revisions WHERE device=?", (key,)).fetchone()

    @staticmethod
    def _registration(db, key, now):
        return db.execute("""
            SELECT * FROM phone_registrations
            WHERE device=? AND verified_at>0 AND MIN(updated_at, verified_at) > ?
        """, (key, now - _REGISTRATION_TTL)).fetchone()

    def status(self, device: PhoneAlertDevice) -> dict:
        key = _digest(device.device_key)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            revision = self._revision(db, key)
            row = self._registration(db, key, time.time())
            return {"registered": len(json.loads(row["gates"])) if row else 0,
                    "revision": revision["revision"] if revision else 0,
                    "fid_verified": bool(row and device.fid and row["fid"] == device.fid)}

    def issue_challenge(self, body: PhoneAlertChallenge) -> tuple[str, str, float]:
        key = _digest(body.device_key)
        now = time.time()
        challenge_id, response = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
        with self._write() as db:
            revision = self._revision(db, key)
            if revision and body.revision <= revision["revision"]:
                raise PhoneAlertConflict(revision["revision"])
            # Bound provider workload across API workers, including failed
            # sends. A device or FID can request one fresh proof at a time.
            if db.execute("SELECT COUNT(*) FROM phone_alert_challenges WHERE issued_at>?", (now - 600,)).fetchone()[0] >= 40:
                raise PhoneAlertCapacity
            if db.execute("""
                SELECT 1 FROM phone_alert_challenges
                WHERE expires_at>? AND consumed=0 AND (device=? OR fid=?)
            """, (now, key, body.fid)).fetchone():
                raise PhoneAlertCapacity
            db.execute("INSERT INTO phone_alert_challenges VALUES(?,?,?,?,?,?,?,0)",
                       (challenge_id, key, body.fid, body.revision, _digest(response), now, now + _CHALLENGE_TTL))
        return challenge_id, response, now + _CHALLENGE_TTL

    def close_challenge(self, challenge_id: str) -> None:
        with self._write() as db:
            db.execute("UPDATE phone_alert_challenges SET consumed=1 WHERE id=?", (challenge_id,))

    @staticmethod
    def _check_mutation(db, body, now):
        key = _digest(body.device_key)
        current = PhoneAlertStore._revision(db, key)
        row = PhoneAlertStore._registration(db, key, now)
        if current and body.revision <= current["revision"]:
            same = body.revision == current["revision"] and secrets.compare_digest(body.digest(), current["request_digest"])
            if same and (not body.gates or row):
                return row, None, True
            raise PhoneAlertConflict(current["revision"])
        if not body.gates or (row and row["fid"] == body.fid):
            return row, None, False
        challenge = db.execute("""
            SELECT * FROM phone_alert_challenges
            WHERE id=? AND device=? AND fid=? AND revision=? AND expires_at>? AND consumed=0
        """, (body.challenge_id, key, body.fid, body.revision, now)).fetchone()
        if not challenge or not secrets.compare_digest(_digest(body.challenge_response or ""), challenge["response_digest"]):
            raise PhoneAlertProofRequired
        return row, challenge, False

    def preflight(self, body: PhoneAlertRegistration) -> tuple[bool, list[SavedGateAlert]]:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            row, _, already = self._check_mutation(db, body, time.time())
            return already, _gates(row) if row else []

    def replace(self, body: PhoneAlertRegistration, gates: list[SavedGateAlert], config: PhoneAlertConfig) -> None:
        key = _digest(body.device_key)
        now = time.time()
        with self._write() as db:
            existing, challenge, already = self._check_mutation(db, body, now)
            if already:
                return
            anonymous = False
            if gates:
                transfer = None
                if challenge:
                    # A device which proves receipt may recover its FID after
                    # reinstalling. An old key cannot renew a deleted binding.
                    transfer = db.execute("""
                        SELECT device FROM phone_registrations WHERE fid=? AND device<>?
                        AND verified_at>0 AND MIN(updated_at,verified_at)>?
                    """, (body.fid, key, now - _REGISTRATION_TTL)).fetchone()
                active = db.execute("""
                    SELECT device, gates FROM phone_registrations WHERE device<>?
                    AND verified_at>0 AND MIN(updated_at,verified_at)>?
                """, (key, now - _REGISTRATION_TTL)).fetchall()
                others = [row for row in active if not transfer or row["device"] != transfer["device"]]
                areas = {gate.area for gate in gates}
                subscriptions = len(gates)
                for row in others:
                    other_gates = _gates(row)
                    areas.update(gate.area for gate in other_gates)
                    subscriptions += len(other_gates)
                if not config.fits(len(others) + 1, len(areas), subscriptions):
                    raise PhoneAlertCapacity
                if transfer:
                    db.execute("DELETE FROM phone_registrations WHERE device=?", (transfer["device"],))
                db.execute("""
                    INSERT INTO phone_registrations(device,fid,gates,updated_at,verified_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(device) DO UPDATE SET fid=excluded.fid,gates=excluded.gates,
                        updated_at=excluded.updated_at,verified_at=excluded.verified_at
                """, (key, body.fid, json.dumps([gate.model_dump() for gate in gates]), now,
                      now if challenge else existing["verified_at"]))
            else:
                # A revocation may arrive ahead of the first request for
                # proof. Retain its revision too, with a bounded pool for
                # installations that have never contacted the service.
                previous = self._revision(db, key)
                known = (previous and not previous["anonymous"]) or db.execute(
                    "SELECT 1 FROM phone_registrations WHERE device=? UNION SELECT 1 FROM phone_alert_challenges WHERE device=?",
                    (key, key),
                ).fetchone()
                anonymous = not known
                if anonymous and not previous and db.execute(
                    "SELECT COUNT(*) FROM phone_alert_revisions WHERE anonymous=1"
                ).fetchone()[0] >= 1024:
                    raise PhoneAlertCapacity
                db.execute("DELETE FROM phone_registrations WHERE device=?", (key,))
            db.execute("""
                INSERT INTO phone_alert_revisions(device,revision,request_digest,updated_at,anonymous)
                VALUES(?,?,?,?,?) ON CONFLICT(device) DO UPDATE SET revision=excluded.revision,
                    request_digest=excluded.request_digest,updated_at=excluded.updated_at,
                    anonymous=excluded.anonymous
            """, (key, body.revision, body.digest(), now, anonymous))
            db.execute("UPDATE phone_alert_challenges SET consumed=1 WHERE device=? AND revision<=?",
                       (key, body.revision))

    def active(self) -> list[RegisteredPhone]:
        return self._active(time.time())

    def _active(self, now):
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT p.*, r.revision FROM phone_registrations p JOIN phone_alert_revisions r USING(device)
                WHERE p.verified_at>0 AND MIN(p.updated_at,p.verified_at)>? ORDER BY p.device
            """, (now - _REGISTRATION_TTL,)).fetchall()
        return [RegisteredPhone(row["device"], row["fid"], _gates(row), row["revision"], row["verified_at"]) for row in rows]

    @staticmethod
    def _current(db, phone: RegisteredPhone, now) -> bool:
        return bool(db.execute("""
            SELECT 1 FROM phone_registrations p JOIN phone_alert_revisions r USING(device)
            WHERE p.device=? AND p.fid=? AND r.revision=? AND p.verified_at>0 AND MIN(p.updated_at,p.verified_at)>?
        """, (phone.key, phone.fid, phone.revision, now - _REGISTRATION_TTL)).fetchone())

    def claim(self, phone: RegisteredPhone, candidate: AlertCandidate, deadline: float) -> str | None:
        token = secrets.token_urlsafe(18)
        with self._write() as db:
            now = time.time()
            if now >= deadline or not self._current(db, phone, now):
                return None
            # If a report switches between dated and undated, prevent two
            # messages for the same observation window. Independently dated
            # runs remain distinct.
            if candidate.undated:
                other = db.execute("""
                    SELECT 1 FROM phone_alert_deliveries
                    WHERE device=? AND crossing_id=? AND run_id LIKE ?
                    AND (accepted_at>? OR lease_until>?)
                """, (phone.key, candidate.crossing_id, candidate.train + ":%", now - _UNDATED_TTL, now))
            else:
                other = db.execute("""
                    SELECT 1 FROM phone_alert_deliveries
                    WHERE device=? AND crossing_id=? AND run_id=?
                    AND (accepted_at>? OR lease_until>?)
                """, (phone.key, candidate.crossing_id, f"undated:{candidate.train}", now - _UNDATED_TTL, now))
            if other.fetchone():
                return None
            result = db.execute("""
                INSERT INTO phone_alert_deliveries
                    (device,crossing_id,run_id,created_at,lease_until,expires_at,lease_token)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(device,crossing_id,run_id) DO UPDATE SET
                    created_at=excluded.created_at, accepted_at=NULL, lease_until=excluded.lease_until,
                    expires_at=excluded.expires_at, lease_token=excluded.lease_token
                WHERE phone_alert_deliveries.lease_until<=?
                    AND (phone_alert_deliveries.accepted_at IS NULL OR phone_alert_deliveries.expires_at<=?)
            """, (phone.key, candidate.crossing_id, candidate.run_id, now, now + _CLAIM_LEASE,
                  deadline, token, now, now))
            return token if result.rowcount else None

    def begin_dispatch(self, phone: RegisteredPhone, token: str, deadline: float) -> bool:
        with self._write() as db:
            now = time.time()
            if now >= deadline or not self._current(db, phone, now):
                return False
            lease = db.execute("""
                SELECT 1 FROM phone_alert_deliveries
                WHERE device=? AND lease_token=? AND lease_until>? AND accepted_at IS NULL
            """, (phone.key, token, now)).fetchone()
            if not lease:
                return False
            db.execute("INSERT INTO phone_alert_dispatches VALUES(?,?,?)",
                       (token, phone.key, now + _DISPATCH_LEASE))
            return True

    def finish(self, phone: RegisteredPhone, candidate: AlertCandidate, token: str, outcome: str) -> None:
        with self._write() as db:
            now = time.time()
            db.execute("DELETE FROM phone_alert_dispatches WHERE token=?", (token,))
            if outcome == "sent":
                ttl = _UNDATED_TTL if candidate.undated else _DELIVERY_TTL
                db.execute("""
                    UPDATE phone_alert_deliveries SET accepted_at=?, lease_until=0, expires_at=?
                    WHERE device=? AND crossing_id=? AND run_id=? AND lease_token=?
                """, (now, now + ttl, phone.key, candidate.crossing_id, candidate.run_id, token))
            elif outcome == "rejected":
                db.execute("""
                    UPDATE phone_alert_deliveries SET lease_until=expires_at
                    WHERE device=? AND crossing_id=? AND run_id=? AND lease_token=?
                """, (phone.key, candidate.crossing_id, candidate.run_id, token))
            elif outcome != "uncertain":
                db.execute("""
                    DELETE FROM phone_alert_deliveries WHERE device=? AND crossing_id=? AND run_id=? AND lease_token=?
                """, (phone.key, candidate.crossing_id, candidate.run_id, token))
            if outcome == "unregistered":
                db.execute("DELETE FROM phone_registrations WHERE device=? AND fid=? AND verified_at=?",
                           (phone.key, phone.fid, phone.verified_at))

    def has_dispatch(self, device_key: str) -> bool:
        with closing(self._connect()) as db:
            return bool(db.execute("SELECT 1 FROM phone_alert_dispatches WHERE device=? AND expires_at>?",
                                   (_digest(device_key), time.time())).fetchone())

    def start_poll(self, config: PhoneAlertConfig) -> str | None:
        token = secrets.token_urlsafe(18)
        with self._write() as db:
            now = time.time()
            result = db.execute("""
                UPDATE phone_alert_worker SET token=?, next_poll=?, lease_until=?
                WHERE id=1 AND next_poll<=? AND lease_until<=?
            """, (token, now + config.interval, now + config.interval + _DISPATCH_LEASE, now, now))
            return token if result.rowcount else None

    def finish_poll(self, token: str, failed_areas: int) -> None:
        with self._write() as db:
            db.execute("""
                UPDATE phone_alert_worker SET lease_until=0, last_success=?, failed_areas=?
                WHERE id=1 AND token=?
            """, (time.time(), failed_areas, token))

    def worker_health(self) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT last_success,failed_areas,next_poll FROM phone_alert_worker WHERE id=1").fetchone()
            return dict(row)

    def provider_health(self) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT retry_at,last_success,failures,error FROM phone_alert_provider WHERE id=1").fetchone()
            return dict(row)

    def provider_success(self, started_at: float) -> None:
        with self._write() as db:
            db.execute("""
                UPDATE phone_alert_provider SET last_success=?, failures=0, retry_at=0, error=''
                WHERE id=1 AND retry_at<=?
            """, (time.time(), started_at))

    def provider_failure(self, error: str, minimum: float) -> None:
        with self._write() as db:
            now = time.time()
            db.execute("""
                UPDATE phone_alert_provider
                SET retry_at=MAX(retry_at,?+MAX(?,MIN(900,5*(1 << MIN(failures,8))))),
                    failures=failures+1, error=?
                WHERE id=1
            """, (now, minimum, error))


def _gates(row) -> list[SavedGateAlert]:
    return [SavedGateAlert.model_validate(gate) for gate in json.loads(row["gates"])]


def eligible_predictions(scan: dict, now: datetime) -> dict[str, list[AlertCandidate]]:
    eligible = defaultdict(list)
    if scan.get("error") or scan.get("map_warning") or not scan.get("generated_at"):
        return eligible
    checked = datetime.fromisoformat(scan["generated_at"]).timestamp()
    now = now.timestamp()
    for row in scan.get("gate_predictions", []):
        train = row["train"]
        closes_in = row["closes_in"]
        age = row["age_min"]
        anchor = row.get("timing_as_of", checked)
        if (not isinstance(train, str) or not train.isascii() or not train.isdecimal()
                or row.get("observed") is not True or row.get("timing_confident") is not True
                or row.get("at_station") or not math.isfinite(closes_in) or closes_in <= 0
                or not math.isfinite(anchor) or not 0 <= age + (now - anchor) / 60 < REPORT_FRESH_MIN):
            continue
        declared = row.get("start_date")
        run = f"undated:{train}"
        if declared:
            try:
                run = f"{train}:{date.fromisoformat(declared).isoformat()}"
            except ValueError:
                pass
        closes_at = row.get("alert_closes_at", anchor + closes_in * 60)
        fresh_until = row.get("alert_fresh_until", anchor + (REPORT_FRESH_MIN - age) * 60)
        if math.isfinite(closes_at) and math.isfinite(fresh_until) and now < min(closes_at, fresh_until):
            eligible[row["id"]].append(AlertCandidate(row["id"], train, run, closes_at, fresh_until))
    return eligible


class PhoneAlertPoller:
    def __init__(self, service, store: PhoneAlertStore, delivery, config: PhoneAlertConfig):
        self.service = service
        self.store = store
        self.delivery = delivery
        self.config = config
        self._delivery_slots = asyncio.Semaphore(_DELIVERY_CONCURRENCY)
        self._ready = False

    async def run(self) -> None:
        while True:
            token = None
            started = time.monotonic()
            try:
                token = await asyncio.to_thread(self.store.start_poll, self.config)
                if token is not None:
                    failed = await self.poll()
                    await asyncio.to_thread(self.store.finish_poll, token, failed)
            except asyncio.CancelledError:
                # A claim left by a shutdown recovers at lease expiry.
                raise
            except Exception:
                _log.exception("Saved crossing alert poll failed")
                if token is not None:
                    await asyncio.to_thread(self.store.finish_poll, token, 1)
            delay = self.config.interval - (time.monotonic() - started) if token is not None else 15
            await asyncio.sleep(max(1, delay))

    async def poll(self) -> int:
        phones = await asyncio.to_thread(self.store.active)
        now = datetime.now(INDIA)
        admitted_areas = {gate.area for phone in phones for gate in phone.gates}
        if not self.config.fits(len(phones), len(admitted_areas), sum(len(phone.gates) for phone in phones)):
            _log.warning("Saved crossing alert coverage exceeds the configured capacity")
            return len(admitted_areas)
        areas = defaultdict(list)
        for phone in phones:
            for gate in phone.gates:
                if gate.allows(now):
                    areas[gate.area].append((phone, gate))
        if not areas and not self._ready:
            # Obtain an authorized token even on an idle installation. The
            # provider's first successful send verifies delivery permissions.
            self._ready = await self.delivery.probe()
        deadline = now.timestamp() + self.config.interval - 2
        tasks = [self._check_area(area, subscriptions, deadline) for area, subscriptions in areas.items()]
        return sum(await asyncio.gather(*tasks))

    async def _check_area(self, area, subscriptions: list[tuple[RegisteredPhone, SavedGateAlert]], cutoff: float) -> int:
        try:
            async with asyncio.timeout(self.config.scan_seconds):
                gate_ids = frozenset(gate.crossing_id for _, gate in subscriptions)
                scan = await self.service.predict_here(
                    *area, budget_seconds=self.config.scan_seconds - 3, alert_gate_ids=gate_ids,
                )
            if scan.get("error") or scan.get("map_warning"):
                return 1
            predictions = eligible_predictions(scan, datetime.now(INDIA))
            sends = [self._deliver_gate(phone, gate, predictions[gate.crossing_id], cutoff)
                     for phone, gate in subscriptions if gate.crossing_id in predictions]
            if sends:
                async with asyncio.timeout(max(0.01, cutoff - time.time() + 0.01)):
                    await asyncio.gather(*sends)
            return int(bool(scan.get("unmapped_gate_count") or scan.get("coverage", {}).get("deadline_exhausted")))
        except Exception as exc:
            _log.warning("Saved crossing alert area check failed (%s)", type(exc).__name__)
            return 1

    async def _deliver_gate(self, phone, gate, candidates, cutoff) -> None:
        for candidate in sorted(candidates, key=lambda row: row.closes_at):
            if await self._deliver(phone, gate, candidate, cutoff):
                return

    async def _deliver(self, phone, gate, candidate, cutoff) -> bool:
        async with self._delivery_slots:
            now = datetime.now(INDIA)
            scheduled_until = gate.active_until(now)
            if scheduled_until is None or not 0 < candidate.closes_at - now.timestamp() <= gate.lead_minutes * 60:
                return False
            deadline = min(candidate.closes_at, candidate.fresh_until, scheduled_until.timestamp())
            if min(deadline, cutoff) <= now.timestamp():
                return False
            token = await asyncio.to_thread(self.store.claim, phone, candidate, deadline)
            if token is None:
                return False
            # The transport obtains OAuth first, then reserves a bounded
            # dispatch. A confirmed opt-out waits for an earlier dispatch
            # reservation to drain. An unreserved send must see the new state.
            async def authorize():
                return await asyncio.to_thread(self.store.begin_dispatch, phone, token, min(deadline, cutoff))

            outcome = await self.delivery.send(phone.fid, gate, candidate, deadline, authorize)
            await asyncio.to_thread(self.store.finish, phone, candidate, token, outcome)
            return True
