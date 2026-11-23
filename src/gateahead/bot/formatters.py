from __future__ import annotations

import html
import re

from telegram.constants import MessageLimit

from gateahead.clusters import cluster_crossings, maps_link, place_label
from gateahead.models import Crossing
from gateahead.predict import PASS_GRACE_MIN


def h(value: object) -> str:
    return html.escape(str(value))


def reply_pages(text: str) -> list[str]:
    pages, lines = [], []
    length = 0
    limit = int(MessageLimit.MAX_TEXT_LENGTH)
    for line in text.split("\n"):
        if len(line) > limit:
            plain = html.unescape(re.sub(r"</?b>", "", line))
            line = h(plain[:600]) + "…"
        if length + len(line) > limit:
            pages.append("\n".join(lines))
            lines, length = [], 0
        lines.append(line)
        length += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


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
        "<b>GateAhead</b>\n\n"
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
        "/clearlocation\n"
        "Remove your saved location and stop alerts.\n\n"
        "/nearby\n"
        "Show railway crossings within 10 km, with map links.\n\n"
        "<b>Trains</b>\n"
        "/predict\n"
        "Show live trains and which of those crossings they are heading toward.\n\n"
        "/subscribe\n"
        "Notify you when a nearby crossing is about to close. Stays on until /unsubscribe.\n\n"
        "/unsubscribe\n"
        "Stop notifications and keep your saved location.\n\n"
        "<b>Map</b>\n"
        "/markgate\n"
        "Submit a missing crossing for review using a Telegram location or a maps link. "
        "Pending pins do not appear on the public map."
    )


def nearby_text(items: list[tuple[Crossing, float]], error: str | None = None) -> str:
    if error and not items:
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
    if error:
        lines.append("Showing saved crossings because the map update is unavailable.")
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
    passed = scan.get("passed") or []
    awaiting = scan.get("awaiting_update") or []
    def run_key(row):
        return row.get("run_id") or f"{row.get('train') or row.get('number')}:{row.get('start_date', '')}"

    def run_date(row):
        return f" (started {h(row['start_date'])})" if row.get("start_date") else ""

    gone = {run_key(row) for row in passed}
    sightings = [s for s in (scan.get("sightings") or []) if s.get("age", 0) < 45]
    live = [s for s in sightings if not s.get("passed_local") and run_key(s) not in gone]
    grouped: dict[str, list] = {}
    for row in approaching:
        grouped.setdefault(run_key(row), []).append(row)
    lines = ["<b>Train checks and crossing estimates</b>"]
    for update in scan.get("service_updates") or []:
        state = {"cancelled": "Cancelled", "diverted": "Diverted", "partially_cancelled": "Partially cancelled"}.get(update.get("status"), "Update unavailable")
        lines.append(f"• {h(update['number'])}{run_date(update)}: {state}.")
        if update.get("status") != "cancelled":
            lines.append("Crossing timing is unavailable until this journey’s route is confirmed.")
    coverage = scan.get("coverage") or {}
    pending = coverage.get("pending_live", 0)
    discovery_unavailable = coverage.get("unavailable_discovery", 0)
    incomplete = coverage.get("status") == "partial" or any(coverage.get(key, 0) for key in (
        "unavailable_discovery", "pending_live", "missing_or_stale_routes", "unavailable_live"))
    if scan.get("map_warning"):
        lines.append("Showing saved map data. Some crossings or train estimates may be missing.")
    elif coverage.get("deadline_exhausted"):
        lines.append("The check timed out before all sources could reply.")
    if discovery_unavailable:
        lines.append("Some train searches could not be completed. Coverage is incomplete.")
    if pending:
        lines.append(f"{coverage.get('live_checked', 0)} of {coverage.get('live_candidates', 0)} train checks completed. {pending} still pending; check again to continue.")
    elif coverage.get("missing_or_stale_routes"):
        lines.append("Some train timetables are missing or need refreshing. Coverage is incomplete.")
    elif coverage.get("unavailable_live"):
        lines.append("Some services have no current running report. Coverage is incomplete.")
    if awaiting:
        lines.append("<b>Position update needed</b>")
        for train in awaiting:
            reason = train.get("reason")
            stopped = " stopped" if reason == "stopped_report" else ""
            age = "time unconfirmed" if reason == "unknown_report_age" else f"{round(train.get('age_min') or 0)} min old"
            lines.append(f"• {h(train['number'])} {h(train.get('name') or '')}{run_date(train)}: last reported{stopped} at "
                         f"{h(train.get('current') or 'the station')}, {age}.")
        lines.append("Passage has not been confirmed. The barrier’s position is unknown.")
    if grouped:
        lines.append("")
        for rows in grouped.values():
            rows = sorted(rows, key=lambda r: r["eta"])
            title = f"{h(rows[0]['train'])} {h(rows[0]['name'])}{run_date(rows[0])}".strip()
            lines.append(f"<b>{title}</b>")
            if rows[0].get("at_station"):
                lines.append(f"At {h(rows[0].get('current') or 'the station')}")
            for row in rows:
                label = row["cluster"]
                if row.get("lat") is not None:
                    label = place_label(row["cluster"], row["lat"], row["lon"])
                lines.append(
                    f"• {h(label)}: {gate_label(row['eta'], row.get('closes_in'))}, {row['from_you_km']:.1f} km from you"
                )
            lines.append("")
    elif awaiting:
        lines.append("")
    elif live or passed:
        lines.append("Other train estimates may be missing. The barrier’s position is unknown." if incomplete
                     else "No nearby crossing closure was estimated from the completed checks.")
        lines.append("")
    elif scan.get("service_updates"):
        lines.append("No crossing estimates are available for these journeys. The barrier’s position is unknown.")
        return "\n".join(lines)
    else:
        lines.append("No train estimates are available from the completed checks. The barrier’s position is unknown." if incomplete
                     else "No live trains reported on this stretch right now.")
        return "\n".join(lines)
    if passed:
        lines.append("<b>Estimated recent passage</b>")
        for row in passed[:6]:
            title = f"{h(row['train'])} {h(row.get('name') or '')}{run_date(row)}".strip()
            lines.append(f"• {title} {passed_label(row['ago'])} at {h(row['cluster'])}")
        lines.append("")
    if live:
        lines.append("<b>On this line</b>")
        for s in live[:6]:
            delay = f", {s['delay']} min late" if s.get("delay") else ""
            where = h(s.get("current") or "unknown")
            if s.get("age", 0) >= 8:
                lines.append(f"• {h(s['number'])} {h(s['name'])}{run_date(s)}: last reported at {where}{delay}")
            else:
                lines.append(f"• {h(s['number'])} {h(s['name'])}{run_date(s)}: at {where}{delay}")
        lines.append("")
    lines.append("/nearby for map links.")
    return "\n".join(lines).rstrip()


def gate_label(eta: float | None, closes: float | None = None) -> str:
    """What a person at the gate needs: when the barrier comes down, or that it is down."""
    if eta is None:
        return ""
    if eta < -PASS_GRACE_MIN:
        return "estimated passage"
    down_in = eta if closes is None else closes
    if down_in <= 0:
        return "estimated closed, train passing" if eta <= 1 else f"estimated closed, train in {eta:.0f} min"
    if down_in < 1:
        return "estimated to be closing now"
    return f"closes in {_span_label(down_in)} (estimate)"


def passed_label(ago: float) -> str:
    if ago < 1:
        return "passed just now"
    return f"passed {ago:.0f} min ago"


def _span_label(minutes: float) -> str:
    if minutes >= 60:
        hours = int(minutes // 60)
        mins = int(round(minutes - hours * 60))
        if mins == 60:
            hours += 1
            mins = 0
        if mins == 0:
            return f"{hours} hr"
        return f"{hours} hr {mins} min"
    return f"{minutes:.0f} min"
