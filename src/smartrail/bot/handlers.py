from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import ContextTypes

from smartrail.bot.formatters import friendly_error, h, help_text, nearby_text, predict_text, start_text
from smartrail.maps import looks_like_maps, resolve_maps_url
from smartrail.service import SmartRail


def svc(context: ContextTypes.DEFAULT_TYPE) -> SmartRail:
    return context.application.bot_data["service"]


def _awaiting(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("awaiting") or ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(start_text(), parse_mode="HTML", disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(help_text(), parse_mode="HTML", disable_web_page_preview=True)


async def setlocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = "here"
    button = KeyboardButton(text="Share my current location", request_location=True)
    markup = ReplyKeyboardMarkup([[button]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Send a location or a Google Maps link.",
        reply_markup=markup,
    )


async def markgate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting"] = "gate"
    if context.args:
        coords = await resolve_maps_url(svc(context).session, " ".join(context.args))
        if coords is None:
            await update.message.reply_text(
                "Could not read that Maps link. Send a Telegram location instead."
            )
            return
        await _save_gate(update, context, coords[0], coords[1])
        return
    await update.message.reply_text(
        "Send a location or a Google Maps link for the crossing.",
    )


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    await _handle_coords(update, context, loc.latitude, loc.longitude)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if looks_like_maps(text):
        coords = await resolve_maps_url(svc(context).session, text)
        if coords is None:
            await update.message.reply_text(
                "Could not read that Maps link. Send a Telegram location instead."
            )
            return
        await _handle_coords(update, context, coords[0], coords[1])
        return
    if _awaiting(context) == "gate":
        await update.message.reply_text("Please send a map pin or a Google Maps link for the crossing.")


async def choose_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        kind, lat_s, lon_s = (query.data or "").split("|")
        lat, lon = float(lat_s), float(lon_s)
    except (ValueError, TypeError):
        return
    context.user_data["awaiting"] = kind
    await _handle_coords(update, context, lat, lon)


async def _handle_coords(update: Update, context: ContextTypes.DEFAULT_TYPE, lat: float, lon: float) -> None:
    mode = _awaiting(context)
    if not mode:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("This is where I am", callback_data=f"here|{lat:.5f}|{lon:.5f}")],
                [InlineKeyboardButton("This is a crossing", callback_data=f"gate|{lat:.5f}|{lon:.5f}")],
            ]
        )
        target = update.effective_message
        await target.reply_text(
            "What should I do with this pin?",
            reply_markup=keyboard,
        )
        return
    context.user_data["awaiting"] = ""
    if mode == "gate":
        await _save_gate(update, context, lat, lon)
        return
    user = svc(context).set_location(update.effective_user.id, lat, lon)
    items, error = await svc(context).nearby(user.lat, user.lon)
    await update.effective_message.reply_text(
        nearby_text(items, error), parse_mode="HTML", disable_web_page_preview=True
    )


async def _save_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, lat: float, lon: float) -> None:
    context.user_data["awaiting"] = ""
    result = await svc(context).mark_gate(update.effective_user.id, lat, lon)
    if result.verdict == "existing" and result.match:
        m = result.match
        text = f"Already on the map: {h(m.area or m.name or 'railway crossing')}."
    elif result.verdict == "reject":
        text = friendly_error(result.reason) if re_map_error(result.reason) else result.reason
    else:
        text = "Added. This pin was not already listed."
    await update.effective_message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


def re_map_error(reason: str) -> bool:
    lower = reason.lower()
    return "map" in lower or "busy" in lower or "unavailable" in lower


async def nearby(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = svc(context).store.get_user(update.effective_user.id)
    if user.lat is None:
        await update.message.reply_text("Set your location first with /setlocation.")
        return
    items, error = await svc(context).nearby(user.lat, user.lon)
    await update.message.reply_text(
        nearby_text(items, error), parse_mode="HTML", disable_web_page_preview=True
    )


async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = svc(context).store.get_user(update.effective_user.id)
    if user.lat is None:
        await update.message.reply_text("Set your location first with /setlocation, then send /predict.")
        return
    status = None
    try:
        status = await update.message.reply_text("Checking trains on this stretch…")
    except TimedOut:
        status = None
    scan = await svc(context).predict_here(user.lat, user.lon)
    text = predict_text(scan)
    if status is not None:
        try:
            await status.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
            return
        except TimedOut:
            pass
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await svc(context).subscribe(update.effective_user.id)
    except ValueError as exc:
        await update.message.reply_text(friendly_error(str(exc)))
        return
    await update.message.reply_text(
        "Alerts are on. You will be notified when a nearby crossing is about to close.\n"
        "/unsubscribe to stop."
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc(context).unsubscribe(update.effective_user.id)
    await update.message.reply_text("Alerts are off.")
