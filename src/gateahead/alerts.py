from __future__ import annotations

import logging

from telegram.ext import ContextTypes

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
            grouped.setdefault(row["train"], []).append(row)
        for number, rows in grouped.items():
            rows = sorted(rows, key=lambda r: r["eta"])
            if not service.store.should_alert(
                user.user_id, "near", number, service.settings.alert_cooldown_min
            ):
                continue
            head = f"Train {number} {rows[0]['name']}"
            lines = [head, "Closing near you:"]
            for row in rows[:6]:
                eta = "now" if row["eta"] <= 1 else f"{row['eta']:.0f} min"
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
                service.store.mark_alert(user.user_id, "near", number)
            except Exception:
                logger.exception("failed to send alert to %s", user.user_id)
