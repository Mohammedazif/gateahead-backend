"""Back up and consolidate an older database into the configured persistent store."""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from gateahead.config import load_settings
from gateahead.models import RouteStop
from gateahead.phone_alerts import PhoneAlertStore, _DELIVERY_TTL
from gateahead.store import Store, decode_route_variants


def consolidate(target: Path, source: Path) -> dict:
    target, source = target.resolve(), source.resolve()
    if target == source:
        raise ValueError("Source and destination must be different databases")
    if not source.is_file():
        raise ValueError("Source database does not exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    backups = target.parent / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backups.mkdir(parents=True, mode=0o700)
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
                                 fetched_at=route["fetched_at"],
                                 variants=decode_route_variants(route["variants"]) if "variants" in route.keys() else ())
                count += 1
        imported["train_routes"] = count
        # Timestamped records use their newest version; untimestamped conflicts keep the destination.
        for table, stamp in {"users": "updated_at", "crossings": None, "alerts": "sent_at", "halts": None,
                             "train_names": None, "train_catalog": None, "station_scans": "checked_at",
                             "gate_reports": "created_at", "crossing_detail_flags": "created_at",
                             "crossing_aliases": None}.items():
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
        if {"phone_registrations", "phone_alert_revisions", "phone_alert_deliveries"} & tables:
            imported.update(_restore_phone_alerts(target, snapshot, tables))
        store.migrate_crossing_ids()
        store.meta_set("database_consolidated_from", str(source))
        return {"database": str(target), "backups": str(backups), "imported": imported}
    finally:
        snapshot.close()
        store._conn.close()


def _restore_phone_alerts(target: Path, source: sqlite3.Connection, tables: set[str]) -> dict:
    """Merge the revision and binding together, including revocation tombstones."""
    PhoneAlertStore(target)
    saved = {}
    revisions = {}
    if "phone_registrations" in tables:
        saved = {row["device"]: row for row in source.execute("SELECT * FROM phone_registrations")}
    if "phone_alert_revisions" in tables:
        revisions = {row["device"]: row for row in source.execute("SELECT * FROM phone_alert_revisions")}
    counts = {"phone_registrations": 0, "phone_alert_revisions": 0, "phone_alert_deliveries": 0}
    with closing(sqlite3.connect(target)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        for key in saved.keys() | revisions.keys():
            enrolled = saved.get(key)
            incoming = revisions.get(key)
            local = db.execute("SELECT revision FROM phone_alert_revisions WHERE device=?", (key,)).fetchone()
            if local and (not incoming or incoming["revision"] < local[0]
                          or (incoming["revision"] == local[0] and enrolled is not None)):
                continue
            if incoming:
                counts["phone_alert_revisions"] += db.execute("""
                    INSERT INTO phone_alert_revisions(device, revision, request_digest, updated_at, anonymous)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(device) DO UPDATE SET revision=excluded.revision,
                        request_digest=excluded.request_digest, updated_at=excluded.updated_at,
                        anonymous=excluded.anonymous
                """, (key, incoming["revision"], incoming["request_digest"], incoming["updated_at"],
                      incoming["anonymous"] if "anonymous" in incoming.keys() else 0)).rowcount
            if enrolled is None:
                db.execute("DELETE FROM phone_registrations WHERE device=?", (key,))
                continue
            verified_at = enrolled["verified_at"] if incoming and "verified_at" in enrolled.keys() else 0
            counts["phone_registrations"] += db.execute("""
                INSERT INTO phone_registrations(device,fid,gates,updated_at,verified_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device) DO UPDATE SET fid=excluded.fid, gates=excluded.gates,
                    updated_at=excluded.updated_at, verified_at=excluded.verified_at
                WHERE ? OR (phone_registrations.verified_at=0 AND excluded.updated_at>phone_registrations.updated_at)
            """, (key, enrolled["fid"], enrolled["gates"], enrolled["updated_at"], verified_at,
                  incoming is not None)).rowcount

        # Two backups can contain different proven owners of one FID. Keep the
        # latest proof and its tombstone history without re-enrolling an old owner.
        db.execute("""
            DELETE FROM phone_registrations AS old WHERE old.verified_at>0 AND EXISTS (
                SELECT 1 FROM phone_registrations AS newer
                WHERE newer.fid=old.fid AND
                    (newer.verified_at,newer.updated_at,newer.device)>(old.verified_at,old.updated_at,old.device)
            )
        """)

        if "phone_alert_deliveries" in tables:
            for row in source.execute("SELECT * FROM phone_alert_deliveries"):
                if "accepted_at" in row.keys():
                    accepted, expires = row["accepted_at"], row["expires_at"]
                elif ":" in row["run_id"]:
                    accepted, expires = row["created_at"], row["created_at"] + _DELIVERY_TTL
                else:
                    continue
                if accepted is None:
                    continue
                counts["phone_alert_deliveries"] += db.execute("""
                    INSERT INTO phone_alert_deliveries(device,crossing_id,run_id,created_at,accepted_at,expires_at)
                    SELECT device, ?, ?, ?, ?, ? FROM phone_registrations WHERE device=?
                    ON CONFLICT(device,crossing_id,run_id) DO UPDATE SET accepted_at=excluded.accepted_at,
                        expires_at=excluded.expires_at, lease_until=0, lease_token=''
                    WHERE phone_alert_deliveries.accepted_at IS NULL
                """, (row["crossing_id"], row["run_id"], row["created_at"], accepted,
                      expires, row["device"])).rowcount
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(consolidate(load_settings().db_path, args.source))


if __name__ == "__main__":
    main()
