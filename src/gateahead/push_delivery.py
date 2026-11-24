from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from urllib.parse import urlencode

import aiohttp

from gateahead.clock import INDIA
from gateahead.phone_alerts import AlertCandidate, PhoneAlertStore, SavedGateAlert

_log = logging.getLogger(__name__)
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{4,61}[a-z0-9]")


def _retry_after(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return 0
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        seconds = instant.timestamp() - time.time()
    return max(0, seconds) if math.isfinite(seconds) else 0


class FCMPushDelivery:
    def __init__(self, session: aiohttp.ClientSession, project: str, credentials, store: PhoneAlertStore):
        self.session = session
        self.url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
        self.credentials = credentials
        self.store = store
        self.refresh_lock = asyncio.Lock()

    @classmethod
    async def from_environment(cls, session: aiohttp.ClientSession, store: PhoneAlertStore) -> FCMPushDelivery | None:
        project = os.getenv("GATEAHEAD_FCM_PROJECT_ID", "").strip()
        if not project:
            return None
        if not _FCM_PROJECT_ID.fullmatch(project):
            _log.error("Saved crossing alerts require a valid Firebase project ID")
            return None
        try:
            credentials = await asyncio.to_thread(cls._load_credentials)
        except Exception as exc:
            _log.error("Saved crossing alerts could not load Firebase credentials (%s)", type(exc).__name__)
            return None
        return cls(session, project, credentials, store)

    @staticmethod
    def _load_credentials():
        import google.auth
        credentials, _ = google.auth.default(scopes=[_FCM_SCOPE])
        return credentials

    async def _access_token(self) -> str:
        from google.auth.credentials import TokenState
        from google.auth.transport.requests import Request

        async with self.refresh_lock:
            if self.credentials.token_state != TokenState.FRESH:
                await asyncio.to_thread(self.credentials.refresh, partial(Request(), timeout=10))
            return self.credentials.token

    async def probe(self) -> bool:
        health = await asyncio.to_thread(self.store.provider_health)
        if health["retry_at"] > time.time():
            return False
        try:
            return bool(await self._access_token())
        except Exception as exc:
            _log.warning("Saved crossing alert credentials are unavailable (%s)", type(exc).__name__)
            await asyncio.to_thread(self.store.provider_failure, "oauth", 30)
            return False

    async def send(self, fid: str, gate: SavedGateAlert, candidate: AlertCandidate, deadline: float,
                   authorize: Callable[[], Awaitable[bool]]) -> str:
        def message():
            return self._message(fid, gate, candidate, deadline)
        return await self._send(message, deadline, authorize)

    async def challenge(self, fid: str, challenge_id: str, response_value: str, deadline: float) -> str:
        def message():
            ttl = max(0, math.floor(deadline - time.time()))
            return {
                "fid": fid,
                "data": {
                    "phone_alert_challenge_id": challenge_id,
                    "phone_alert_challenge": response_value,
                },
                "android": {"priority": "HIGH", "ttl": f"{ttl}s"},
                # A proof is handled by the foreground app. It must never
                # appear as a visible OS notification.
                "apns": {"headers": {"apns-push-type": "background", "apns-priority": "5",
                                     "apns-expiration": str(math.floor(deadline))},
                         "payload": {"aps": {"content-available": 1}}},
            }
        return await self._send(message, deadline)

    async def _send(self, message: Callable[[], dict], deadline: float,
                    authorize: Callable[[], Awaitable[bool]] | None = None) -> str:
        health = await asyncio.to_thread(self.store.provider_health)
        if health["retry_at"] > time.time():
            return "retry"
        if deadline - time.time() <= 1:
            return "dropped"
        try:
            access = await self._access_token()
        except Exception as exc:
            _log.warning("Saved crossing alert credentials are unavailable (%s)", type(exc).__name__)
            await asyncio.to_thread(self.store.provider_failure, "oauth", 30)
            return "retry"
        health = await asyncio.to_thread(self.store.provider_health)
        if health["retry_at"] > time.time():
            return "retry"
        if deadline - time.time() <= 1 or (authorize is not None and not await authorize()):
            return "dropped"
        started = time.time()
        if deadline - started <= 1:
            return "dropped"
        try:
            async with self.session.post(
                self.url, json={"message": message()}, headers={"Authorization": f"Bearer {access}"},
                timeout=aiohttp.ClientTimeout(total=min(10, deadline - started)), allow_redirects=False,
            ) as response:
                if response.status == 200:
                    await asyncio.to_thread(self.store.provider_success, started)
                    return "sent"
                status = response.status
                retry_after = _retry_after(response.headers.get("Retry-After", ""))
                try:
                    data = await response.json(content_type=None)
                    details = data.get("error", {}).get("details", ()) if isinstance(data, dict) else ()
                    codes = {detail.get("errorCode") for detail in details if isinstance(detail, dict)}
                except (ValueError, TypeError, AttributeError):
                    codes = set()
                if "UNREGISTERED" in codes or "SENDER_ID_MISMATCH" in codes:
                    return "unregistered"
                if status == 400:
                    # Deadlines and coordinates are generated here. A data
                    # error for one installation should not stop its peers.
                    _log.warning("Firebase rejected a saved crossing message (HTTP 400)")
                    return "rejected"
                if status == 401:
                    async with self.refresh_lock:
                        if self.credentials.token == access:
                            self.credentials.token = None
                minimum = 60 if status == 429 else 30 if status in (401, 403, 404) else 0
                await asyncio.to_thread(self.store.provider_failure, f"http_{status}", max(retry_after, minimum))
                _log.warning("Firebase declined a saved crossing message (HTTP %s)", status)
                return "retry"
        except (aiohttp.ClientError, TimeoutError) as exc:
            _log.warning("Firebase saved crossing delivery failed (%s)", type(exc).__name__)
            await asyncio.to_thread(self.store.provider_failure, "transport", 0)
            return "uncertain"

    @staticmethod
    def _message(fid: str, gate: SavedGateAlert, candidate: AlertCandidate, deadline: float) -> dict:
        expiration = min(math.floor(deadline), math.floor(time.time()) + 120)
        ttl = max(0, expiration - math.floor(time.time()))
        closure = datetime.fromtimestamp(candidate.closes_at, INDIA).strftime("%H:%M")
        identity = hashlib.sha256(f"{gate.crossing_id}:{candidate.run_id}".encode()).digest()
        tag = str((int.from_bytes(identity[:4], "big") & 0x7fffffff) or 1)
        link = "https://gateahead.vercel.app/?" + urlencode({
            "lat": gate.lat, "lon": gate.lon, "gate": gate.crossing_id,
        })
        title = "GateAhead crossing alert"
        body = f"Estimated closure around {closure} IST for train {candidate.train}. Open for the latest estimate."
        return {
            "fid": fid,
            "data": {"crossing_link": link, "title": title, "body": body,
                     "notification_id": tag, "expires_at": str(expiration)},
            # A top-level notification payload makes unrelated Android
            # messages collapsible. The Android app displays the data message.
            "android": {"priority": "HIGH", "ttl": f"{ttl}s"},
            "apns": {"headers": {"apns-push-type": "alert", "apns-priority": "10",
                                 "apns-expiration": str(expiration), "apns-collapse-id": identity.hex()},
                     "payload": {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}},
        }
