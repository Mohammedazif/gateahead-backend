from __future__ import annotations

import html
import re

from smartrail.clusters import cluster_crossings, maps_link, place_label
from smartrail.models import Crossing


def h(value: object) -> str:
    return html.escape(str(value))


def friendly_error(message: str | None) -> str:
    if not message:
        return "Something went wrong. Please try again in a moment."
    if re.search(r"\b(429|502|504|timeout|timed out|busy|map_busy)\b", message, re.I):
        return "Map data is busy right now. Please wait a minute and try again."
    if re.search(r"\b(406|OpenStreetMap|overpass|map_unavailable)\b", message, re.I):
        return "Could not load the map for this place. Please try again shortly."
    if "Could not reach" in message or "invalid data" in message:
        return "Could not load the map for this place. Please try again shortly."
    return message


def start_text() -> str:
    return (
        "<b>SmartRail</b>\n\n"
        "Alerts you before a railway crossing nearby is likely to close.\n"
        "No train number needed.\n\n"
        "/setlocation  your position\n"
        "/nearby  crossings around you\n"
        "/predict  trains that may close them\n"
        "/subscribe  turn on closing alerts\n"
        "/markgate  add a missing crossing\n\n"
        "/help for details."
    )


def help_text() -> str:
    return (
        "<b>Place</b>\n"
        "/setlocation\n"
        "Save your position with a Telegram location or a Google Maps link.\n\n"
        "/nearby\n"
        "Show railway crossings within 10 km, with map links.\n\n"
        "<b>Trains</b>\n"
        "/predict\n"
        "Show live trains and which of those crossings they are heading toward.\n\n"
        "/subscribe\n"
        "Notify you when a nearby crossing is about to close. Stays on until /unsubscribe.\n\n"
        "/unsubscribe\n"
        "Stop notifications.\n\n"
        "<b>Map</b>\n"
        "/markgate\n"
        "Add a crossing that is not listed, using a Telegram location or a Google Maps link. "
        "You will be told if it is already on the map."
    )


def nearby_text(items: list[tuple[Crossing, float]], error: str | None = None) -> str:
    if error:
        return friendly_error(error)
    if not items:
        return (
            "No railway crossings found near this place.\n\n"
            "To add one, send /markgate and drop a pin on the crossing "
            "or paste a Google Maps link."
        )
    clusters = cluster_crossings([c for c, _d in items])
    by_id = {c.id: d for c, d in items}
    lines = [f"<b>Crossings within 10 km</b> ({len(clusters)})"]
    for cluster in clusters:
        nearest = min(by_id[c.id] for c in cluster.crossings if c.id in by_id)
        label = place_label(cluster.name, cluster.lat, cluster.lon)
        lines.append(f"• {h(label)}: {nearest:.1f} km")
        lines.append(f"  {maps_link(cluster.lat, cluster.lon)}")
    return "\n".join(lines)


def predict_text(scan: dict) -> str:
    if scan.get("error"):
        return friendly_error(str(scan["error"]))
    approaching = scan.get("approaching") or []
    sightings = [s for s in (scan.get("sightings") or []) if s.get("age", 0) < 45]
    live = [s for s in sightings if not s.get("passed_local")]
    grouped: dict[str, list] = {}
    for row in approaching:
        grouped.setdefault(row["train"], []).append(row)
    lines = ["<b>Live trains</b>"]
    if grouped:
        lines.append("")
        for number, rows in grouped.items():
            rows = sorted(rows, key=lambda r: r["eta"])
            title = f"{h(number)} {h(rows[0]['name'])}".strip()
            lines.append(f"<b>{title}</b>")
            if rows[0].get("at_station"):
                lines.append(f"At {h(rows[0].get('current') or 'the station')}")
            for row in rows:
                label = row["cluster"]
                if row.get("lat") is not None:
                    label = place_label(row["cluster"], row["lat"], row["lon"])
                lines.append(
                    f"• {h(label)}: {_eta_label(row['eta'])}, {row['from_you_km']:.1f} km from you"
                )
            lines.append("")
    elif live:
        lines.append("No train is close enough to close a crossing near you right now.")
        lines.append("")
    else:
        lines.append("No live trains reported on this stretch right now.")
        return "\n".join(lines)
    if live:
        lines.append("<b>On this line</b>")
        for s in live[:6]:
            delay = f", {s['delay']} min late" if s.get("delay") else ""
            where = h(s.get("current") or "unknown")
            if s.get("age", 0) >= 8:
                lines.append(f"• {h(s['number'])} {h(s['name'])}: last reported at {where}{delay}")
            else:
                lines.append(f"• {h(s['number'])} {h(s['name'])}: at {where}{delay}")
        lines.append("")
    lines.append("/nearby for map links.")
    return "\n".join(lines).rstrip()


def _eta_label(minutes: float | None) -> str:
    if minutes is None:
        return ""
    if minutes < -1:
        return "already passed"
    if minutes <= 1:
        return "closing now"
    if minutes >= 60:
        hours = int(minutes // 60)
        mins = int(round(minutes - hours * 60))
        if mins == 60:
            hours += 1
            mins = 0
        if mins == 0:
            return f"closes in {hours} hr"
        return f"closes in {hours} hr {mins} min"
    return f"closes in {minutes:.0f} min"
