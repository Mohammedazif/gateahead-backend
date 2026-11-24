from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)
_ACTIVE_LIMIT = 4
_RETAINED_LIMIT = 48
_REUSE_SECONDS = 15
_RESULT_SECONDS = 120


@dataclass
class PredictionCheck:
    id: str
    area: tuple[float, float]
    revision: int = 0
    result: dict | None = None
    finished_at: float | None = None
    task: asyncio.Task[None] | None = None

    def publish(self, result: dict) -> None:
        self.result = result
        self.revision += 1

    def response(self, after: int = 0) -> dict:
        return {
            "check_id": self.id,
            "revision": self.revision,
            "done": self.finished_at is not None,
            **({"result": self.result} if self.revision > after else {}),
        }


class PredictionChecks:
    def __init__(self) -> None:
        self._checks: dict[str, PredictionCheck] = {}

    def _prune(self, now: float) -> None:
        expired = [key for key, check in self._checks.items()
                   if check.finished_at is not None and now - check.finished_at >= _RESULT_SECONDS]
        for key in expired:
            del self._checks[key]

    def start(
        self, lat: float, lon: float, run: Callable[[Callable[[dict], None]], Awaitable[dict]],
    ) -> PredictionCheck | None:
        now = time.monotonic()
        self._prune(now)
        area = (lat, lon)
        for check in self._checks.values():
            if check.area == area and (
                check.finished_at is None or
                (check.result and not check.result.get("error") and now - check.finished_at < _REUSE_SECONDS)
            ):
                return check
        if sum(check.finished_at is None for check in self._checks.values()) >= _ACTIVE_LIMIT:
            return None
        if len(self._checks) >= _RETAINED_LIMIT:
            oldest = min((check for check in self._checks.values() if check.finished_at is not None),
                         key=lambda check: check.finished_at)
            del self._checks[oldest.id]
        check = PredictionCheck(secrets.token_urlsafe(18), area)
        self._checks[check.id] = check
        check.task = asyncio.create_task(self._run(check, run))
        return check

    async def _run(
        self, check: PredictionCheck, run: Callable[[Callable[[dict], None]], Awaitable[dict]],
    ) -> None:
        try:
            async with asyncio.timeout(89):
                check.publish(await run(check.publish))
        except TimeoutError:
            if check.result is None:
                check.publish({"error": "train_check_timeout"})
            else:
                coverage = {**check.result.get("coverage", {}), "status": "partial", "checking": False,
                            "deadline_exhausted": True}
                check.publish({**check.result, "coverage": coverage})
        except Exception:
            _log.exception("Train check failed")
            check.publish({"error": "train_check_failed"})
        finally:
            check.finished_at = time.monotonic()

    def get(self, check_id: str) -> PredictionCheck | None:
        self._prune(time.monotonic())
        return self._checks.get(check_id)

    async def shutdown(self) -> None:
        active = [check.task for check in self._checks.values() if check.task and not check.task.done()]
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        self._checks.clear()
