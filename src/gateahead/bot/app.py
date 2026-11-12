from __future__ import annotations

import logging
import sys

import aiohttp
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from gateahead.alerts import poll_alerts
from gateahead.bot import handlers
from gateahead.bot.formatters import friendly_error
from gateahead.config import load_settings
from gateahead.halts import ensure_catalog
from gateahead.service import GateAhead
from gateahead.store import Store


def build_application() -> Application:
    settings = load_settings()
    if not settings.telegram_bot_token or settings.telegram_bot_token.startswith("YOUR_"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=settings.log_level,
    )
    store = Store(settings.db_path)

    async def post_init(app: Application) -> None:
        session = aiohttp.ClientSession(
            headers={"User-Agent": "GateAhead/0.3 (https://github.com/Mohammedazif/gateahead-tele)"}
        )
        app.bot_data["http"] = session
        app.bot_data["service"] = GateAhead(settings, store, session)
        await ensure_catalog(session, store)
        app.job_queue.run_repeating(
            poll_alerts,
            interval=settings.alert_poll_seconds,
            first=20,
            name="gate-alerts",
        )

    async def post_shutdown(app: Application) -> None:
        session = app.bot_data.get("http")
        if session is not None:
            await session.close()

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(10)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_command))
    app.add_handler(CommandHandler("setlocation", handlers.setlocation))
    app.add_handler(CommandHandler("markgate", handlers.markgate))
    app.add_handler(CommandHandler("nearby", handlers.nearby))
    app.add_handler(CommandHandler("predict", handlers.predict))
    app.add_handler(CommandHandler("subscribe", handlers.subscribe))
    app.add_handler(CommandHandler("unsubscribe", handlers.unsubscribe))
    app.add_handler(MessageHandler(filters.LOCATION, handlers.location_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_handler))
    app.add_handler(CallbackQueryHandler(handlers.choose_pin))
    app.add_error_handler(on_error)
    return app


async def on_error(update: object, context) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logging.getLogger(__name__).warning("telegram network error: %s", err)
        return
    logging.getLogger(__name__).exception("update failed")
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text(
            friendly_error(str(err or "")),
            disable_web_page_preview=True,
        )
    except Exception:
        logging.getLogger(__name__).exception("failed to send error reply")


def main() -> None:
    try:
        application = build_application()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
    print("GateAhead (main) running. Ctrl+C to stop.")
    application.run_polling()
