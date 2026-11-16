from __future__ import annotations

import logging

from telegram.ext import ContextTypes

from gateahead.bot.formatters import gate_label
from gateahead.service import GateAhead

logger = logging.getLogger(__name__)


async def poll_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    service: GateAhead = context.application.bot_data["service"]
    users = service.store.subscribed_users()
    for user in users:
        if user.lat is None:
            continue
        try:
            scan = await service.predict_here(user.lat, user.lon)
        except Exception:
            logger.exception("alert poll failed for %s", user.user_id)
            continue
        grouped: dict[str, list] = {}
        for row in scan.get("approaching") or []:
            if row["status"] not in {"closed", "closing_soon"}:
                continue
            grouped.setdefault(row.get("run_id") or row["train"], []).append(row)
        for run_id, rows in grouped.items():
            rows = sorted(rows, key=lambda r: r["eta"])
            number = rows[0]["train"]
            if not service.store.should_alert(
                user.user_id, "near", run_id, service.settings.alert_cooldown_min
            ):
                continue
            head = f"Train {number} {rows[0]['name']}"
            if rows[0].get("start_date"):
                head += f" (started {rows[0]['start_date']})"
            lines = [head, "Closing near you:"]
            for row in rows[:6]:
                eta = gate_label(row["eta"], row.get("closes_in"))
                label = row["cluster"]
                if row.get("lat") is not None:
                    label = f"{row['cluster']} ({row['lat']:.4f}, {row['lon']:.4f})"
                lines.append(f"• {label}: {eta}")
            lines.append("/nearby for the map.")
            try:
                await context.bot.send_message(
                    chat_id=user.user_id,
                    text="\n".join(lines),
                    disable_web_page_preview=True,
                )
                service.store.mark_alert(user.user_id, "near", run_id)
            except Exception:
                logger.exception("failed to send alert to %s", user.user_id)
