from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def find_data_dir() -> Path:
    env = os.getenv("SMARTRAIL_DATA")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "data"
        if candidate.is_dir() or (parent / "pyproject.toml").exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    path = Path.cwd() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    data_dir: Path
    nearby_radius_m: float
    rail_max_m: float
    duplicate_m: float
    approaching_eta_min: float
    closing_soon_eta_min: float
    closed_eta_min: float
    max_ahead_km: float
    default_speed_kmh: float
    alert_poll_seconds: int
    alert_cooldown_min: int
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "smartrail.db"


def load_settings() -> Settings:
    data_dir = find_data_dir()
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        data_dir=data_dir,
        nearby_radius_m=float(os.getenv("NEARBY_RADIUS_M", "10000")),
        rail_max_m=float(os.getenv("RAIL_MAX_M", "40")),
        duplicate_m=float(os.getenv("DUPLICATE_M", "250")),
        approaching_eta_min=float(os.getenv("APPROACHING_ETA_MIN", "30")),
        closing_soon_eta_min=float(os.getenv("CLOSING_SOON_ETA_MIN", "10")),
        closed_eta_min=float(os.getenv("CLOSED_ETA_MIN", "3")),
        max_ahead_km=float(os.getenv("MAX_AHEAD_KM", "80")),
        default_speed_kmh=float(os.getenv("DEFAULT_SPEED_KMH", "60")),
        alert_poll_seconds=int(os.getenv("ALERT_POLL_SECONDS", "180")),
        alert_cooldown_min=int(os.getenv("ALERT_COOLDOWN_MIN", "15")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
