"""Back up and consolidate an older database into the configured persistent store."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from gateahead.config import load_settings
from gateahead.models import RouteStop
from gateahead.store import Store


def consolidate(target: Path, source: Path) -> dict:
    target, source = target.resolve(), source.resolve()
    if target == source:
        raise ValueError("Source and destination must be different databases")
    if not source.is_file():
        raise ValueError("Source database does not exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    backups = target.parent / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backups.mkdir(parents=True)
    old = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    try:
        # Read a consistent backup so an active source writer cannot change a route halfway through import.
        snapshot = sqlite3.connect(backups / "source.db")
        old.backup(snapshot)
        snapshot.row_factory = sqlite3.Row
    finally:
        old.close()
    store = Store(target)
    with sqlite3.connect(backups / "destination.db") as backup:
        store._conn.backup(backup)
    tables = {r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    imported = {}
    try:
        # Route points must move as a unit, retaining the provider retrieval time.
        count = 0
        if {"train_routes", "train_stops"} <= tables:
            for route in snapshot.execute("SELECT * FROM train_routes"):
                existing = store._conn.execute("SELECT fetched_at FROM train_routes WHERE train_number=?", (route["train_number"],)).fetchone()
                if existing and existing[0] >= route["fetched_at"]:
                    continue
                stops = [RouteStop(**{f.name: row[f.name] for f in fields(RouteStop)})
                         for row in snapshot.execute("SELECT * FROM train_stops WHERE train_number=? ORDER BY seq", (route["train_number"],))]
                if len(stops) < 2:
                    continue
                store.save_route(route["train_number"], stops, route["run_days"] if "run_days" in route.keys() else "",
                                 fetched_at=route["fetched_at"])
                count += 1
        imported["train_routes"] = count
        # Timestamped records use their newest version; untimestamped conflicts keep the destination.
        for table, stamp in {"users": "updated_at", "crossings": None, "alerts": "sent_at", "halts": None,
                             "train_names": None, "train_catalog": None, "station_scans": "checked_at",
                             "gate_reports": "created_at"}.items():
            if table not in tables:
                continue
            definition = snapshot.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            if not store._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                store._conn.execute(definition)
            columns = [r[1] for r in store._conn.execute(f'PRAGMA table_info("{table}")')]
            source_columns = {r[1] for r in snapshot.execute(f'PRAGMA table_info("{table}")')}
            columns = [c for c in columns if c in source_columns]
            keys = [r[1] for r in store._conn.execute(f'PRAGMA table_info("{table}")') if r[5]]
            names = ",".join(f'"{c}"' for c in columns)
            updates = ",".join(f'"{c}"=excluded."{c}"' for c in columns if c not in keys)
            suffix = "DO NOTHING"
            if stamp and stamp in columns and updates:
                suffix = f'DO UPDATE SET {updates} WHERE COALESCE(excluded."{stamp}",0)>COALESCE("{table}"."{stamp}",0)'
            before = store._conn.total_changes
            with store._conn:
                store._conn.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)}) ON CONFLICT {suffix}',
                                        [tuple(row[c] for c in columns) for row in snapshot.execute(f'SELECT * FROM "{table}"')])
            imported[table] = store._conn.total_changes - before
        store.meta_set("database_consolidated_from", str(source))
        return {"database": str(target), "backups": str(backups), "imported": imported}
    finally:
        snapshot.close()
        store._conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(consolidate(load_settings().db_path, args.source))


if __name__ == "__main__":
    main()
