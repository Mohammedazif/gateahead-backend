from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, ValidationError

from gateahead.clock import INDIA
from gateahead.geo import km
from gateahead.models import Access, Crossing, CrossingModes

_log = logging.getLogger(__name__)
_MAX_CROSSING_OFFSET_KM = 0.05
_PILOT_CATALOG = Path(__file__).with_name("data") / "crossing-details.json"
VERIFICATION_MAX_DAYS = 365
FLAG_RETENTION_SECONDS = 90 * 24 * 60 * 60


class VerifiedPlace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


class VerifiedAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cars: Access = "unknown"
    two_wheelers: Access = "unknown"
    pedestrians: Access = "unknown"
    verified_on: date
    evidence: HttpUrl


class VerifiedBypass(VerifiedPlace):
    kind: Literal["flyover", "underpass"]
    name: str = Field(min_length=1, max_length=120)
    replacement: bool = False
    verified_on: date
    evidence: HttpUrl


class VerifiedCrossing(VerifiedPlace):
    access: VerifiedAccess | None = None
    bypasses: list[VerifiedBypass] = Field(default_factory=list, max_length=5)


_catalog = TypeAdapter(dict[str, VerifiedCrossing])


class CrossingDetails:
    def __init__(self, data_dir: Path):
        path = data_dir / "crossing-details.json"
        try:
            if not path.exists():
                path = _PILOT_CATALOG
            self.verified = _catalog.validate_json(path.read_bytes())
        except FileNotFoundError:
            self.verified = {}
        except (OSError, ValidationError):
            self.verified = {}
            _log.exception("Unable to read verified crossing details from %s", path)

    def for_crossing(self, crossing: Crossing) -> dict:
        record = self.verified.get(crossing.id)
        if record and km((record.lat, record.lon), crossing.coords) > _MAX_CROSSING_OFFSET_KM:
            record = None
        access = record.access if record else None
        today = datetime.now(INDIA).date()
        oldest = today - timedelta(days=VERIFICATION_MAX_DAYS)
        if access and not oldest <= access.verified_on <= today:
            access = None
        bypasses = []
        seen = set()
        for bypass in record.bypasses if record else ():
            key = (bypass.kind, bypass.lat, bypass.lon)
            if key in seen or not oldest <= bypass.verified_on <= today:
                continue
            seen.add(key)
            bypasses.append(bypass.model_dump(mode="json", exclude={"evidence"}, exclude_defaults=True))
        if access:
            details = access.model_dump(mode="json", exclude={"evidence"})
            source = "verified"
        else:
            details = asdict(crossing.modes or CrossingModes())
            source = "mapped" if crossing.modes and crossing.modes != CrossingModes() else "unavailable"
        return {**details, "source": source, "bypasses": bypasses}


def record_crossing_flag(path: Path, crossing_id: str, installation_id: str) -> None:
    reporter = hashlib.sha256(installation_id.encode()).hexdigest()
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute(
            "INSERT INTO crossing_detail_flags (crossing_id, reporter, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(crossing_id, reporter) DO UPDATE SET created_at=excluded.created_at",
            (crossing_id, reporter, time.time()),
        )
