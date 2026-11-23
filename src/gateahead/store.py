from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from gateahead.geo import km
from gateahead.models import Crossing, Halt, RouteStop, SavedRoute, User


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
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
            CREATE INDEX IF NOT EXISTS crossings_lat ON crossings(lat);
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
            CREATE TABLE IF NOT EXISTS train_names (
              train_number TEXT PRIMARY KEY,
              name TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS train_catalog (
              train_number TEXT PRIMARY KEY,
              discovered_at REAL NOT NULL,
              source TEXT NOT NULL,
              retry_after REAL NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0,
              live_checked_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS train_stops_code ON train_stops(code, train_number);
            CREATE TABLE IF NOT EXISTS station_scans (
              code TEXT PRIMARY KEY,
              checked_at REAL,
              retry_after REAL NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS catalog_lock (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              owner TEXT NOT NULL,
              expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_health (
              provider TEXT PRIMARY KEY, checks INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0, last_attempt REAL, last_success REAL, last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS run_checks (run_id TEXT PRIMARY KEY, checked_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS route_segments (
              id INTEGER PRIMARY KEY,
              train_number TEXT NOT NULL,
              seq INTEGER NOT NULL,
              UNIQUE(train_number, seq)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS route_segment_bounds USING rtree(
              id, min_lat, max_lat, min_lon, max_lon
            );
            CREATE TRIGGER IF NOT EXISTS delete_route_segment AFTER DELETE ON route_segments
            BEGIN
              DELETE FROM route_segment_bounds WHERE id=old.id;
            END;
            """
        )
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(train_routes)")}
        if "run_days" not in columns:
            self._conn.execute("ALTER TABLE train_routes ADD COLUMN run_days TEXT NOT NULL DEFAULT ''")
        scan_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(station_scans)")}
        for column in ("last_error", "source"):
            if column not in scan_columns:
                self._conn.execute(f"ALTER TABLE station_scans ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self._conn.execute("INSERT OR IGNORE INTO meta VALUES ('database_id',?)", (uuid.uuid4().hex,))
        self._conn.execute(
            "INSERT OR IGNORE INTO train_catalog (train_number, discovered_at, source) "
            "SELECT train_number, ?, 'cache' FROM train_routes", (time.time(),),
        )
        if self.meta("route_segment_index") != "1":
            for row in self._conn.execute("SELECT train_number FROM train_routes").fetchall():
                stops = self._conn.execute("SELECT * FROM train_stops WHERE train_number=? ORDER BY seq", (row[0],)).fetchall()
                self._index_route(row[0], [_route_stop(stop) for stop in stops])
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('route_segment_index', '1')")
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

    def reconcile_crossing_access(
        self, public_ids: set[str], excluded_ids: set[str], *,
        public_points: Iterable[tuple[float, float]] = (),
        excluded_points: Iterable[tuple[float, float]] = (),
    ) -> None:
        """Remember exact OSM access evidence without changing user pins.

        Restricted rows stay hidden during map outages and process restarts.
        Only this reversible status is restored by later public map evidence;
        moderation rejections remain intact.
        """
        with self._conn:
            self._conn.executemany(
                """UPDATE crossings SET status='public'
                   WHERE source != 'user' AND status='restricted'
                     AND (id=? OR osm_id=?)""",
                [(key, key) for key in public_ids],
            )
            # Imported records can have the same coordinates without an OSM ID.
            # Allow coordinate rounding and keep neighbouring tracks distinct.
            self._conn.executemany(
                """UPDATE crossings SET status=?
                   WHERE source='irgeoportal'
                     AND lat BETWEEN ? - 0.000001 AND ? + 0.000001
                     AND lon BETWEEN ? - 0.000001 AND ? + 0.000001
                     AND status=?""",
                [
                    (new, lat, lat, lon, lon, old)
                    for new, old, points in (
                        ("public", "restricted", public_points),
                        ("restricted", "public", excluded_points),
                    )
                    for lat, lon in points
                ],
            )
            self._conn.executemany(
                """UPDATE crossings SET status='restricted'
                   WHERE source != 'user' AND status IN ('public', 'pending')
                     AND (id=? OR osm_id=?)""",
                [(key, key) for key in excluded_ids],
            )

    def crossings_near(self, lat: float, lon: float, radius_m: float) -> list[Crossing]:
        radius_km = max(0.0, radius_m) / 1000.0
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
        rows = self._conn.execute(
            """
            SELECT * FROM crossings
            WHERE status IN ('public', 'pending')
              AND lat BETWEEN ? AND ?
              AND lon BETWEEN ? AND ?
            """,
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()
        origin = (lat, lon)
        found = [
            _crossing(row)
            for row in rows
            if km(origin, (row["lat"], row["lon"])) * 1000 <= radius_m
        ]
        found.sort(key=lambda crossing: km(origin, crossing.coords))
        return found

    def insert_crossings(self, crossings: list[Crossing]) -> int:
        rows = [
            (
                crossing.id,
                crossing.name,
                crossing.lat,
                crossing.lon,
                crossing.source,
                crossing.status,
                crossing.nearest_station,
                crossing.osm_id,
            )
            for crossing in crossings
            if crossing.id
        ]
        if not rows:
            return 0
        before = self._conn.execute("SELECT COUNT(*) AS n FROM crossings").fetchone()["n"]
        self._conn.executemany(
            """
            INSERT INTO crossings (id, name, lat, lon, source, status, nearest_station, osm_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            rows,
        )
        self._conn.commit()
        after = self._conn.execute("SELECT COUNT(*) AS n FROM crossings").fetchone()["n"]
        return int(after) - int(before)

    def insert_missing_halts(self, halts: list[Halt]) -> int:
        rows = [
            (halt.code, halt.name, halt.lat, halt.lon, halt.kind, halt.zone, halt.source)
            for halt in halts
            if halt.code
        ]
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

    def remember_stop_halts(self, stops: list[RouteStop], *, commit: bool = True) -> int:
        from gateahead.halts import clean_code, halt_kind

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
        if commit:
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

    def remember_train_names(self, names: dict[str, str]) -> None:
        rows = [(number, " ".join(name.split())) for number, name in names.items() if name and name.strip() and name.strip() != number]
        if not rows:
            return
        self._conn.executemany(
            "INSERT INTO train_names (train_number, name) VALUES (?, ?) "
            "ON CONFLICT(train_number) DO UPDATE SET name=excluded.name", rows,
        )
        self._conn.commit()

    def train_names(self, numbers: list[str]) -> dict[str, str]:
        if not numbers:
            return {}
        slots = ",".join("?" for _ in numbers)
        rows = self._conn.execute(
            f"SELECT train_number, name FROM train_names WHERE train_number IN ({slots})", numbers,
        ).fetchall()
        return {row["train_number"]: row["name"] for row in rows}

    def save_route(self, number: str, stops: list[RouteStop], run_days: str = "", *, fetched_at: str | None = None) -> None:
        if len(stops) < 2:
            raise ValueError("A route needs at least two points")
        with self._conn:
            self._conn.execute("DELETE FROM train_stops WHERE train_number = ?", (number,))
            self._conn.executemany(
                "INSERT INTO train_stops (train_number, seq, code, name, km, arrive_min, depart_min, halt_min, stops, lat, lon) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(number, s.seq, s.code, s.name, s.km, s.arrive_min, s.depart_min,
                  s.halt_min, int(s.stops), s.lat, s.lon) for s in stops],
            )
            self._conn.execute(
                "INSERT INTO train_routes (train_number, fetched_at, run_days) VALUES (?, ?, ?) "
                "ON CONFLICT(train_number) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "run_days=CASE WHEN excluded.run_days='' THEN train_routes.run_days ELSE excluded.run_days END",
                (number, fetched_at or datetime.now().isoformat(timespec="seconds"), run_days or ""),
            )
            self._conn.execute(
                "INSERT INTO train_catalog (train_number, discovered_at, source) VALUES (?, ?, 'timetable') "
                "ON CONFLICT(train_number) DO UPDATE SET retry_after=0, failures=0", (number, time.time()),
            )
            self._index_route(number, stops)
            self.remember_stop_halts(stops, commit=False)

    def _index_route(self, number: str, stops: list[RouteStop]) -> None:
        self._conn.execute("DELETE FROM route_segments WHERE train_number=?", (number,))
        located = [s for s in stops if s.lat is not None and s.lon is not None
                   and math.isfinite(s.lat) and math.isfinite(s.lon)]
        for start, end in zip(located, located[1:]):
            if end.km - start.km < 0.05:
                continue
            cursor = self._conn.execute(
                "INSERT INTO route_segments (train_number, seq) VALUES (?, ?)", (number, start.seq),
            )
            self._conn.execute(
                "INSERT INTO route_segment_bounds VALUES (?, ?, ?, ?, ?)",
                (cursor.lastrowid, min(start.lat, end.lat), max(start.lat, end.lat),
                 min(start.lon, end.lon), max(start.lon, end.lon)),
            )

    def route_numbers_near(self, lat: float, lon: float, radius_km: float, max_age_hours: float | None) -> list[str]:
        delta_lat = radius_km / 110.57
        delta_lon = radius_km / (111.32 * max(0.2, abs(math.cos(math.radians(lat)))))
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds") if max_age_hours is not None else ""
        rows = self._conn.execute(
            "SELECT DISTINCT r.train_number FROM route_segment_bounds b "
            "CROSS JOIN route_segments s ON s.id=b.id CROSS JOIN train_routes r ON r.train_number=s.train_number "
            "WHERE r.fetched_at >= ? AND b.min_lat <= ? AND b.max_lat >= ? "
            "AND b.min_lon <= ? AND b.max_lon >= ? ORDER BY r.train_number",
            (cutoff, lat + delta_lat, lat - delta_lat, lon + delta_lon, lon - delta_lon),
        ).fetchall()
        return [row["train_number"] for row in rows]

    def load_route(self, number: str, max_age_hours: float | None) -> SavedRoute | None:
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
        if max_age_hours is not None and (datetime.now() - fetched).total_seconds() > max_age_hours * 3600:
            return None
        rows = self._conn.execute(
            "SELECT * FROM train_stops WHERE train_number = ? ORDER BY seq",
            (number,),
        ).fetchall()
        return SavedRoute([_route_stop(item) for item in rows], str(row["run_days"] or ""))

    def remember_train_candidates(self, numbers: list[str], source: str = "discovery") -> None:
        stamp = time.time()
        with self._conn:
            self._conn.executemany(
                "INSERT INTO train_catalog (train_number, discovered_at, source) VALUES (?, ?, ?) "
                "ON CONFLICT(train_number) DO NOTHING",
                [(n, stamp, source) for n in dict.fromkeys(numbers) if re.fullmatch(r"\d{5}", n)],
            )

    def due_stations(self, limit: int, refresh_hours: float) -> list[Halt]:
        rows = self._conn.execute(
            "SELECT h.* FROM halts h LEFT JOIN station_scans s ON s.code=h.code "
            "WHERE (s.checked_at IS NULL OR s.checked_at < ?) AND COALESCE(s.retry_after,0) <= ? "
            "ORDER BY COALESCE(s.checked_at,0), (s.code IS NOT NULL), "
            "EXISTS(SELECT 1 FROM train_stops t WHERE t.code=h.code) DESC, h.code LIMIT ?",
            (time.time() - refresh_hours * 3600, time.time(), limit),
        ).fetchall()
        return [_halt(row) for row in rows]

    def record_station_scan(self, code: str, success: bool, error: str = "", source: str = "") -> None:
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO station_scans (code) VALUES (?)", (code,))
            self._conn.execute("UPDATE station_scans SET last_error=?, source=? WHERE code=?", (error, source, code))
            if success:
                self._conn.execute(
                    "UPDATE station_scans SET checked_at=?, retry_after=0, failures=0 WHERE code=?", (time.time(), code),
                )
            else:
                self._conn.execute(
                    "UPDATE station_scans SET retry_after=? + MIN(21600,300 * (1 << MIN(failures,6))), "
                    "failures=failures+1 WHERE code=?", (time.time(), code),
                )

    def due_routes(self, limit: int, refresh_hours: float) -> list[str]:
        cutoff = (datetime.now() - timedelta(hours=refresh_hours)).isoformat(timespec="seconds")
        rows = self._conn.execute(
            "SELECT c.train_number FROM train_catalog c LEFT JOIN train_routes r USING(train_number) "
            "WHERE (r.fetched_at IS NULL OR r.fetched_at < ?) AND c.retry_after <= ? "
            "ORDER BY COALESCE(r.fetched_at,''), c.discovered_at, c.train_number LIMIT ?",
            (cutoff, time.time(), limit),
        ).fetchall()
        return [row[0] for row in rows]

    def record_route_failure(self, number: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE train_catalog SET retry_after=? + MIN(21600,300 * (1 << MIN(failures,6))), "
                "failures=failures+1 WHERE train_number=?", (time.time(), number),
            )

    def route_refresh_allowed(self, number: str) -> bool:
        row = self._conn.execute("SELECT retry_after FROM train_catalog WHERE train_number=?", (number,)).fetchone()
        return row is None or row[0] <= time.time()

    def live_check_times(self, numbers: list[str]) -> dict[str, float]:
        found = {}
        for start in range(0, len(numbers), 500):
            batch = numbers[start:start + 500]
            slots = ",".join("?" for _ in batch)
            for row in self._conn.execute(
                f"SELECT train_number, live_checked_at FROM train_catalog WHERE train_number IN ({slots})", batch,
            ):
                found[row[0]] = row[1]
            for row in self._conn.execute(f"SELECT run_id, checked_at FROM run_checks WHERE run_id IN ({slots})", batch):
                found[row[0]] = row[1]
        return found

    def record_live_checks(self, numbers: list[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "UPDATE train_catalog SET live_checked_at=? WHERE train_number=?", [(time.time(), n) for n in numbers],
            )
            self._conn.executemany("INSERT INTO run_checks VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET checked_at=excluded.checked_at",
                                   [(n, time.time()) for n in numbers])

    def claim_catalog(self, owner: str) -> bool:
        with self._conn:
            return bool(self._conn.execute(
                "INSERT INTO catalog_lock VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE "
                "SET owner=excluded.owner, expires=excluded.expires WHERE catalog_lock.expires<=? OR catalog_lock.owner=?",
                (owner, time.time() + 180, time.time(), owner),
            ).rowcount)

    def release_catalog(self, owner: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM catalog_lock WHERE owner=?", (owner,))

    def record_provider(self, provider: str, success: bool, error: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO provider_health (provider,checks,successes,consecutive_failures,last_attempt,last_success,last_error) "
                "VALUES (?,1,?,?,?, ?,?) ON CONFLICT(provider) DO UPDATE SET checks=checks+1, "
                "successes=successes+excluded.successes, consecutive_failures=CASE WHEN excluded.successes=1 THEN 0 ELSE consecutive_failures+1 END, "
                "last_attempt=excluded.last_attempt, last_success=COALESCE(excluded.last_success,last_success), last_error=excluded.last_error",
                (provider, int(success), int(not success), time.time(), time.time() if success else None, error[:300]),
            )

    def provider_status(self) -> list[dict]:
        return [{**dict(row), "state": "unavailable" if row["last_success"] is None else
                 "failing" if row["consecutive_failures"] else
                 "stale" if time.time() - row["last_success"] > 6 * 3600 else "healthy"}
                for row in self._conn.execute("SELECT * FROM provider_health ORDER BY provider")]

    def catalog_status(self, max_age_hours: float = 168) -> dict:
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        row = self._conn.execute(
            "SELECT COUNT(*) AS known_trains, COUNT(r.train_number) AS routes, "
            "COALESCE(SUM(r.fetched_at >= ?),0) AS fresh_routes, "
            "COALESCE(SUM(c.failures>0),0) AS failed_refreshes "
            "FROM train_catalog c LEFT JOIN train_routes r USING(train_number)", (cutoff,),
        ).fetchone()
        found = dict(row)
        found["database_id"] = self.meta("database_id")
        found["stale_routes"] = found["routes"] - found["fresh_routes"]
        found["missing_routes"] = found["known_trains"] - found["routes"]
        daily_cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        found["routes_due_for_daily_refresh"] = self._conn.execute(
            "SELECT COUNT(*) FROM train_catalog c LEFT JOIN train_routes r USING(train_number) "
            "WHERE r.fetched_at IS NULL OR r.fetched_at < ?", (daily_cutoff,),
        ).fetchone()[0]
        found["stations"] = self.halt_count()
        found["stations_scanned"] = self._conn.execute("SELECT COUNT(*) FROM station_scans WHERE checked_at IS NOT NULL").fetchone()[0]
        found["stations_scanned_last_7_days"] = self._conn.execute(
            "SELECT COUNT(*) FROM station_scans WHERE checked_at >= ?", (time.time() - 7 * 86400,),
        ).fetchone()[0]
        found["indexed_segments"] = self._conn.execute("SELECT COUNT(*) FROM route_segments").fetchone()[0]
        found["routes_without_geometry"] = self._conn.execute(
            "SELECT COUNT(*) FROM train_routes r WHERE NOT EXISTS "
            "(SELECT 1 FROM route_segments s WHERE s.train_number=r.train_number)",
        ).fetchone()[0]
        found["fresh_routes_with_geometry"] = self._conn.execute(
            "SELECT COUNT(*) FROM train_routes r WHERE r.fetched_at>=? AND EXISTS "
            "(SELECT 1 FROM route_segments s WHERE s.train_number=r.train_number)", (cutoff,),
        ).fetchone()[0]
        found["last_sync"] = json.loads(self.meta("train_catalog_last_sync") or "null")
        found["last_successful_sync"] = json.loads(self.meta("train_catalog_last_success") or "null")
        found["worker"] = self.worker_status()
        found["providers"] = self.provider_status()
        times = self._conn.execute("SELECT MIN(fetched_at), MAX(fetched_at) FROM train_routes").fetchone()
        found["oldest_route_check"] = times[0]
        found["newest_route_check"] = times[1]
        found["station_source"] = self.meta("catalog_source") or "local"
        found["geometry_basis"] = "legs_between_published_route_points"
        found["route_max_age_hours"] = max_age_hours
        found["national_coverage_verified"] = False
        return found

    def worker_status(self) -> dict:
        last_success = (json.loads(self.meta("train_catalog_last_success") or "null") or {}).get("at")
        last_sync = json.loads(self.meta("train_catalog_last_sync") or "null") or {}
        heartbeat = float(self.meta("train_catalog_heartbeat") or 0) or None
        return {"heartbeat_at": heartbeat, "last_success_at": last_success,
                "state": "never_refreshed" if last_success is None else
                "stalled" if heartbeat is None or time.time() - heartbeat > 300 else
                "stale" if time.time() - last_success > 36 * 3600 else
                "partial" if last_sync.get("status") == "partial" else "healthy"}

    def station_coverage(self, max_age_hours: float = 168):
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        rows = self._conn.execute(
            "SELECT h.code, h.name, h.lat, h.lon, sc.checked_at AS board_checked_at, "
            "COUNT(DISTINCT t.train_number) AS known_stopping_or_passing_trains, "
            "COUNT(DISTINCT CASE WHEN r.fetched_at>=? THEN t.train_number END) AS fresh_routes "
            "FROM halts h LEFT JOIN station_scans sc ON sc.code=h.code "
            "LEFT JOIN train_stops t ON t.code=h.code LEFT JOIN train_routes r ON r.train_number=t.train_number "
            "GROUP BY h.code ORDER BY h.code", (cutoff,),
        )
        for row in rows:
            yield dict(row)

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
