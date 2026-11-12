from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

from smartrail.geo import km
from smartrail.models import Crossing, Halt, RouteStop, SavedRoute, User


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id INTEGER PRIMARY KEY,
              lat REAL,
              lon REAL,
              subscribed INTEGER NOT NULL DEFAULT 0,
              crossing_ids TEXT NOT NULL DEFAULT '[]',
              updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crossings (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              lat REAL NOT NULL,
              lon REAL NOT NULL,
              source TEXT NOT NULL,
              status TEXT NOT NULL,
              nearest_station TEXT NOT NULL DEFAULT '',
              osm_id TEXT NOT NULL DEFAULT '',
              added_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS alerts (
              key TEXT PRIMARY KEY,
              sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS halts (
              code TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              lat REAL NOT NULL,
              lon REAL NOT NULL,
              kind TEXT NOT NULL,
              zone TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS halts_lat_lon ON halts(lat, lon);
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS train_routes (
              train_number TEXT PRIMARY KEY,
              fetched_at TEXT NOT NULL,
              run_days TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS train_stops (
              train_number TEXT NOT NULL,
              seq INTEGER NOT NULL,
              code TEXT NOT NULL,
              name TEXT NOT NULL,
              km REAL NOT NULL,
              arrive_min INTEGER NOT NULL,
              depart_min INTEGER NOT NULL,
              halt_min INTEGER NOT NULL,
              stops INTEGER NOT NULL,
              lat REAL,
              lon REAL,
              PRIMARY KEY (train_number, seq)
            );
            """
        )
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(train_routes)")}
        if "run_days" not in columns:
            self._conn.execute("ALTER TABLE train_routes ADD COLUMN run_days TEXT NOT NULL DEFAULT ''")
        self._conn.commit()

    def get_user(self, user_id: int) -> User:
        row = self._conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return User(user_id=user_id)
        return User(
            user_id=user_id,
            lat=row["lat"],
            lon=row["lon"],
            subscribed=bool(row["subscribed"]),
            crossing_ids=json.loads(row["crossing_ids"] or "[]"),
        )

    def upsert_user(self, user: User) -> User:
        self._conn.execute(
            """
            INSERT INTO users (user_id, lat, lon, subscribed, crossing_ids, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              lat=excluded.lat,
              lon=excluded.lon,
              subscribed=excluded.subscribed,
              crossing_ids=excluded.crossing_ids,
              updated_at=excluded.updated_at
            """,
            (
                user.user_id,
                user.lat,
                user.lon,
                int(user.subscribed),
                json.dumps(user.crossing_ids),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()
        return user

    def set_location(self, user_id: int, lat: float, lon: float) -> User:
        user = self.get_user(user_id)
        user.lat = lat
        user.lon = lon
        return self.upsert_user(user)

    def set_subscribed(self, user_id: int, subscribed: bool, crossing_ids: list[str]) -> User:
        user = self.get_user(user_id)
        user.subscribed = subscribed
        user.crossing_ids = crossing_ids if subscribed else []
        return self.upsert_user(user)

    def subscribed_users(self) -> list[User]:
        rows = self._conn.execute("SELECT user_id FROM users WHERE subscribed = 1").fetchall()
        return [self.get_user(row["user_id"]) for row in rows]

    def save_crossing(self, crossing: Crossing, added_by: int | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO crossings (id, name, lat, lon, source, status, nearest_station, osm_id, added_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              status=excluded.status
            """,
            (
                crossing.id,
                crossing.name,
                crossing.lat,
                crossing.lon,
                crossing.source,
                crossing.status,
                crossing.nearest_station,
                crossing.osm_id,
                added_by,
            ),
        )
        self._conn.commit()

    def public_crossings(self) -> list[Crossing]:
        rows = self._conn.execute(
            "SELECT * FROM crossings WHERE status IN ('public', 'pending')"
        ).fetchall()
        return [_crossing(row) for row in rows]

    def should_alert(self, user_id: int, cluster_id: str, train_number: str, cooldown_min: int) -> bool:
        key = f"{user_id}:{cluster_id}:{train_number}"
        row = self._conn.execute("SELECT sent_at FROM alerts WHERE key = ?", (key,)).fetchone()
        if row is None:
            return True
        try:
            previous = datetime.fromisoformat(row["sent_at"])
        except ValueError:
            return True
        return (datetime.now() - previous).total_seconds() >= cooldown_min * 60

    def meta(self, key: str) -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return "" if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def upsert_halts(self, halts: list[Halt]) -> None:
        self._conn.executemany(
            """
            INSERT INTO halts (code, name, lat, lon, kind, zone, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
              name=excluded.name,
              lat=excluded.lat,
              lon=excluded.lon,
              kind=excluded.kind,
              zone=CASE WHEN excluded.zone = '' THEN halts.zone ELSE excluded.zone END,
              source=excluded.source
            """,
            [
                (halt.code, halt.name, halt.lat, halt.lon, halt.kind, halt.zone, halt.source)
                for halt in halts
                if halt.code
            ],
        )
        self._conn.commit()

    def remember_stop_halts(self, stops: list[RouteStop]) -> int:
        from smartrail.halts import clean_code, halt_kind

        rows = []
        seen: set[str] = set()
        for stop in stops:
            code = clean_code(stop.code)
            if not code or code in seen or stop.lat is None or stop.lon is None:
                continue
            if not (6.0 <= stop.lat <= 37.5 and 68.0 <= stop.lon <= 98.0):
                continue
            seen.add(code)
            name = (stop.name or code).strip() or code
            rows.append((code, name, stop.lat, stop.lon, halt_kind(name), "", "timetable"))
        if not rows:
            return 0
        before = self.halt_count()
        self._conn.executemany(
            """
            INSERT INTO halts (code, name, lat, lon, kind, zone, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO NOTHING
            """,
            rows,
        )
        added = self.halt_count() - before
        self._conn.commit()
        return added

    def fill_blank_zones(self, zones: dict[str, str]) -> int:
        before = self._conn.execute("SELECT COUNT(*) AS n FROM halts WHERE zone = ''").fetchone()["n"]
        self._conn.executemany(
            "UPDATE halts SET zone = ? WHERE code = ? AND zone = ''",
            [(zone.strip(), code.upper()) for code, zone in zones.items() if code and zone and zone.strip()],
        )
        self._conn.commit()
        after = self._conn.execute("SELECT COUNT(*) AS n FROM halts WHERE zone = ''").fetchone()["n"]
        return int(before) - int(after)

    def halt_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM halts").fetchone()
        return int(row["n"])

    def halt_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT kind, COUNT(*) AS n FROM halts GROUP BY kind").fetchall()
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def halt_coords(self, codes: list[str]) -> dict[str, tuple[float, float]]:
        wanted = [code.upper() for code in codes if code]
        if not wanted:
            return {}
        slots = ",".join("?" * len(wanted))
        rows = self._conn.execute(
            f"SELECT code, lat, lon FROM halts WHERE code IN ({slots})",
            wanted,
        ).fetchall()
        return {str(row["code"]): (row["lat"], row["lon"]) for row in rows}

    def save_route(self, number: str, stops: list[RouteStop], run_days: str = "") -> None:
        self._conn.execute("DELETE FROM train_stops WHERE train_number = ?", (number,))
        self._conn.executemany(
            """
            INSERT INTO train_stops (
              train_number, seq, code, name, km, arrive_min, depart_min, halt_min, stops, lat, lon
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    number,
                    stop.seq,
                    stop.code,
                    stop.name,
                    stop.km,
                    stop.arrive_min,
                    stop.depart_min,
                    stop.halt_min,
                    int(stop.stops),
                    stop.lat,
                    stop.lon,
                )
                for stop in stops
            ],
        )
        self._conn.execute(
            """
            INSERT INTO train_routes (train_number, fetched_at, run_days) VALUES (?, ?, ?)
            ON CONFLICT(train_number) DO UPDATE SET
              fetched_at=excluded.fetched_at,
              run_days=CASE
                WHEN excluded.run_days = '' THEN train_routes.run_days
                ELSE excluded.run_days
              END
            """,
            (number, datetime.now().isoformat(timespec="seconds"), run_days or ""),
        )
        self.remember_stop_halts(stops)
        self._conn.commit()

    def load_route(self, number: str, max_age_hours: float) -> SavedRoute | None:
        row = self._conn.execute(
            "SELECT fetched_at, run_days FROM train_routes WHERE train_number = ?",
            (number,),
        ).fetchone()
        if row is None:
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return None
        if (datetime.now() - fetched).total_seconds() > max_age_hours * 3600:
            return None
        rows = self._conn.execute(
            "SELECT * FROM train_stops WHERE train_number = ? ORDER BY seq",
            (number,),
        ).fetchall()
        return SavedRoute([_route_stop(item) for item in rows], str(row["run_days"] or ""))

    def halt(self, code: str) -> Halt | None:
        row = self._conn.execute("SELECT * FROM halts WHERE code = ?", (code.upper(),)).fetchone()
        return None if row is None else _halt(row)

    def halts_page(self, limit: int, offset: int, kind: str = "") -> list[Halt]:
        rows = self._conn.execute(
            """
            SELECT * FROM halts
            WHERE (? = '' OR kind = ?)
            ORDER BY code
            LIMIT ? OFFSET ?
            """,
            (kind, kind, limit, offset),
        ).fetchall()
        return [_halt(row) for row in rows]

    def search_halts(self, query: str, limit: int, kind: str = "") -> list[Halt]:
        text = query.strip()
        code = text.upper()
        like = f"%{_like_escape(text)}%"
        rows = self._conn.execute(
            """
            SELECT * FROM halts
            WHERE (? = '' OR kind = ?)
              AND (code = ? OR name LIKE ? ESCAPE '\\')
            ORDER BY CASE WHEN code = ? THEN 0 ELSE 1 END, name
            LIMIT ?
            """,
            (kind, kind, code, like, code, limit),
        ).fetchall()
        return [_halt(row) for row in rows]

    def halts_near(self, lat: float, lon: float, radius_km: float, limit: int, kind: str = "") -> list[tuple[Halt, float]]:
        delta_lat = radius_km / 110.57
        delta_lon = radius_km / (111.32 * max(0.2, abs(math.cos(math.radians(lat)))))
        rows = self._conn.execute(
            """
            SELECT * FROM halts
            WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
              AND (? = '' OR kind = ?)
            """,
            (lat - delta_lat, lat + delta_lat, lon - delta_lon, lon + delta_lon, kind, kind),
        ).fetchall()
        found: list[tuple[Halt, float]] = []
        for row in rows:
            halt = _halt(row)
            distance = km((lat, lon), halt.coords)
            if distance <= radius_km:
                found.append((halt, distance))
        found.sort(key=lambda item: item[1])
        return found[:limit]

    def mark_alert(self, user_id: int, cluster_id: str, train_number: str) -> None:
        key = f"{user_id}:{cluster_id}:{train_number}"
        self._conn.execute(
            "INSERT INTO alerts (key, sent_at) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET sent_at=excluded.sent_at",
            (key, datetime.now().isoformat(timespec="seconds")),
        )
        self._conn.commit()


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _route_stop(row: sqlite3.Row) -> RouteStop:
    return RouteStop(
        seq=row["seq"],
        code=row["code"],
        name=row["name"],
        km=row["km"],
        arrive_min=row["arrive_min"],
        depart_min=row["depart_min"],
        halt_min=row["halt_min"],
        stops=bool(row["stops"]),
        lat=row["lat"],
        lon=row["lon"],
    )


def _halt(row: sqlite3.Row) -> Halt:
    return Halt(
        code=row["code"],
        name=row["name"],
        lat=row["lat"],
        lon=row["lon"],
        kind=row["kind"],
        zone=row["zone"],
        source=row["source"],
    )


def _crossing(row: sqlite3.Row) -> Crossing:
    return Crossing(
        id=row["id"],
        name=row["name"],
        lat=row["lat"],
        lon=row["lon"],
        source=row["source"],
        status=row["status"],
        nearest_station=row["nearest_station"],
        osm_id=row["osm_id"],
    )
