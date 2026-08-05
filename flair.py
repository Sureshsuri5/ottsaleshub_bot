"""Sales feed + premium emoji / sticker decoration.

Two Telegram limits shape this module:

1. Custom (premium) emoji entities are only accepted from bots that bought a
   Fragment username, OR in messages sent directly to private/group/supergroup
   chats when the bot owner has Telegram Premium. Channels are excluded.
2. Inline keyboard buttons carry no entities, so a custom emoji can never appear
   in a button label — only in message text. Buttons get plain Unicode.

Rather than make you find out which case you're in, `send()` tries the decorated
version first and permanently falls back to plain emoji the moment Telegram
refuses one. Nothing breaks if you have neither Premium nor Fragment.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

import db
from config import cfg

log = logging.getLogger(__name__)

BOT_USERNAME = ""
# Premium emoji are switched off when Telegram refuses them, and switched back
# on after a cooldown. It used to be permanent for the run: one refusal — a
# single chat, a single malformed id — left the whole shop in plain emoji until
# the next deploy, with nothing on screen to say why. Retrying costs one failed
# send per half hour and recovers by itself.
_custom_off_until = 0.0
_custom_reason = ""
CUSTOM_RETRY_AFTER = 1800

# Bot API 9.4 button styling. `style` needs nothing special; `icon_custom_emoji_id`
# needs the bot owner to have Telegram Premium (or the bot to own a Fragment
# username), so icons stay unset until an admin fills them in and tests them.
ICONS: dict[str, str] = {}


def custom_ok() -> bool:
    """Whether premium emoji are usable right now."""
    import time
    return time.monotonic() >= _custom_off_until


def custom_state() -> str:
    """One line for /status: working, or why not and for how long."""
    import time
    if custom_ok():
        return "✅ working"
    mins = int((_custom_off_until - time.monotonic()) / 60) + 1
    return f"⚠️ refused ({_custom_reason or 'no reason given'}) — retrying in {mins} min"


def icon(slot: str | None) -> str | None:
    """Custom emoji id for a button icon, or None. Sync — read from cache."""
    if not custom_ok() or not slot:
        return None
    return ICONS.get(slot)


def icon_id(raw: str | None) -> str | None:
    """Gate a per-product icon id through the same kill switch."""
    raw = (raw or "").strip()
    return raw if (custom_ok() and raw.isdigit()) else None


def disable_icons(reason: str = "") -> None:
    """One refusal turns off both button icons and in-text premium emoji —
    they need the same permission, so if one is rejected the other will be.

    Temporary: cleared automatically after CUSTOM_RETRY_AFTER seconds.
    """
    global _custom_off_until, _custom_reason
    import time
    _custom_off_until = time.monotonic() + CUSTOM_RETRY_AFTER
    _custom_reason = (reason or "")[:120]
    log.warning("premium emoji disabled for %s min: %s",
                CUSTOM_RETRY_AFTER // 60, reason)


CUSTOM_TAG = re.compile(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', re.S)


def strip_custom(text: str) -> str:
    """Replace <tg-emoji> wrappers with the plain emoji inside them."""
    return CUSTOM_TAG.sub(r"\1", text)


async def reload() -> None:
    """Refresh the sync icon cache from the settings table."""
    ICONS.clear()
    ICONS.update(await slot_ids())

# slot -> plain Unicode fallback. A custom emoji id can be attached to any slot
# from the admin panel; until then the fallback is used.
# slot -> (plain fallback emoji, human label shown in the admin screen).
# Named after the button they sit on, so picking one needs no guesswork.
# Every inline button that can carry an icon, in one registry.
# slot -> (plain fallback, label, section). The admin screen is generated from
# this, so adding a button here is the only step needed to make it configurable.
_STATIC_SLOTS = {
    # --- Main menu ---
    "menu_app":      ("🎁", "Open Mini App",   "Main menu"),
    "menu_shop":     ("🏪", "Shop",            "Main menu"),
    "menu_deposit":  ("💵", "Deposit",         "Main menu"),
    "menu_profile":  ("👤", "My Profile",      "Main menu"),
    "menu_support":  ("🆘", "Support",         "Main menu"),
    "menu_refer":    ("⭐", "Refer & Earn",    "Main menu"),
    "menu_admin":    ("🛠", "Admin panel",     "Main menu"),
    # --- Welcome text --- (separate from the buttons on purpose, so the
    # message and the menu can carry different icons)
    "w_title":       ("🏪", "Welcome heading",   "Welcome text"),
    "w_shop":        ("🏪", "Line: Shop",        "Welcome text"),
    "w_deposit":     ("💵", "Line: Deposit",     "Welcome text"),
    "w_profile":     ("👤", "Line: My Profile",  "Welcome text"),
    "w_support":     ("🆘", "Line: Support",     "Welcome text"),
    "w_refer":       ("⭐", "Line: Refer & Earn", "Welcome text"),
    "w_wave":        ("👋", "Greeting wave",     "Welcome text"),
    "w_channel":     ("📢", "Line: Channel",     "Welcome text"),
    "w_group":       ("💬", "Line: Group",       "Welcome text"),
    "w_point":       ("👇", "Choose an option below", "Welcome text"),
    # --- Message icons --- shared across every bot message, so one icon
    # change reaches every screen that uses that meaning
    "m_ok":          ("✅", "Success / confirmed",  "Message icons"),
    "m_warn":        ("⚠️", "Warning",              "Message icons"),
    "m_error":       ("❌", "Error / rejected",     "Message icons"),
    "m_info":        ("💡", "Tip / note",           "Message icons"),
    "m_money":       ("💰", "Money",                "Message icons"),
    "m_box":         ("📦", "Product / order",      "Message icons"),
    "m_cart":        ("🛒", "Shop / catalogue",     "Message icons"),
    "m_card":        ("💳", "Payment",              "Message icons"),
    "m_clock":       ("⏳", "Waiting / expired",    "Message icons"),
    "m_search":      ("🔍", "Search / lookup",      "Message icons"),
    "m_mail":        ("📨", "Received",             "Message icons"),
    "m_receipt":     ("🧾", "Receipt / summary",    "Message icons"),
    "m_pin":         ("📌", "Detail label",         "Message icons"),
    "m_num":         ("🔢", "Quantity",             "Message icons"),
    "m_date":        ("📅", "Date",                 "Message icons"),
    "m_ban":         ("🚫", "Banned",               "Message icons"),
    "m_point":       ("👇", "Pointer",              "Message icons"),
    # --- Shop & product ---
    "refresh":       ("🔄", "Refresh Stock",   "Shop"),
    "buy":           ("🛒", "Buy Now",         "Shop"),
    "back":          ("◀️", "Back buttons",    "Shop"),
    "qty_custom":    ("✍️", "Custom Amount",   "Shop"),
    "cancel":        ("✖", "Cancel (forms)",   "Shop"),
    "page_prev":     ("⬅️", "Prev page",        "Shop"),
    "page_next":     ("➡️", "Next page",        "Shop"),
    # --- Checkout ---
    "pay":           ("💳", "Pay Directly",    "Checkout"),
    "check":         ("🔄", "Check payment",   "Checkout"),
    "paid":          ("✅", "I've paid",       "Checkout"),
    "cancel_order":  ("❌", "Cancel Order",    "Checkout"),
    # --- Profile ---
    "p_stats":       ("📊", "My Stats",        "Profile"),
    "p_notify":      ("🔔", "Notifications",   "Profile"),
    "p_orders":      ("🧾", "My Orders",       "Profile"),
    "p_withdraw":    ("💸", "Withdraw",        "Profile"),
    "p_api":         ("🔑", "Developer API",   "Profile"),
    "r_copy":        ("📋", "Copy referral link", "Profile"),
    "r_transfer":    ("🎁", "Transfer to Wallet", "Profile"),
    "pd_price":      ("💵", "Product: price line", "Product page"),
    "pd_was":        ("🏷", "Product: list price",  "Product page"),
    "pd_tier":       ("⭐", "Product: your pricing", "Product page"),
    "pd_desc":       ("📝", "Product: description", "Product page"),
    "g_title":       ("🛍", "Group reply: product", "Groups"),
    "g_price":       ("💵", "Group reply: price",  "Groups"),
    "g_stock":       ("📦", "Group reply: stock",  "Groups"),
    "g_buy":         ("🛒", "Group reply: Buy button", "Groups"),
    "tu_title":      ("✅", "Top-up: confirmed",  "Deposit"),
    "tu_added":      ("➕", "Top-up: amount added", "Deposit"),
    "tu_balance":    ("👛", "Top-up: new balance", "Deposit"),
    "pay_now":       ("💳", "Pay now (hosted checkout)", "Deposit"),
    "dep_title":     ("💰", "Deposit: heading",   "Deposit"),
    "dep_tip":       ("💡", "Deposit: any-amount tip", "Deposit"),
    "dep_bank":      ("🏦", "Deposit: method name", "Deposit"),
    "dep_amount":    ("💵", "Deposit: amount",     "Deposit"),
    "dep_clock":     ("⏳", "Deposit: expires",    "Deposit"),
    "dep_net":       ("🌐", "Deposit: address",    "Deposit"),
    "dep_point":     ("👆", "Deposit: tap to copy", "Deposit"),
    "dep_ok":        ("✅", "Deposit: accepted note", "Deposit"),
    "dep_warn":      ("⚠️", "Deposit: warning",    "Deposit"),
    "dep_box":       ("📦", "Deposit: product line", "Deposit"),
    "dep_num":       ("🔢", "Deposit: quantity line", "Deposit"),
    "sup_title":     ("🆘", "Support: heading",   "Support"),
    "sup_button":    ("🆘", "Contact Support button", "Support"),
    "pf_title":      ("👤", "Profile: heading",   "Profile text"),
    "pf_id":         ("🆔", "Profile: ID",        "Profile text"),
    "pf_balance":    ("💰", "Profile: balance",   "Profile text"),
    "pf_joined":     ("📅", "Profile: joined",    "Profile text"),
    "st_title":      ("📊", "Stats: heading",     "Profile text"),
    "st_orders":     ("🧾", "Stats: orders",      "Profile text"),
    "st_spent":      ("💸", "Stats: spent",       "Profile text"),
    "st_deposit":    ("💰", "Stats: deposited",   "Profile text"),
    "st_wallet":     ("👛", "Stats: balance",     "Profile text"),
    "st_refer":      ("⭐", "Stats: referrals",   "Profile text"),
    "st_gift":       ("🎁", "Stats: referral earnings", "Profile text"),
    "st_fav":        ("❤️", "Stats: most bought", "Profile text"),
    "rf_title":      ("⭐", "Refer: heading",     "Refer & Earn"),
    "rf_users":      ("👥", "Refer: referred count", "Refer & Earn"),
    "rf_earned":     ("💰", "Refer: total earned", "Refer & Earn"),
    "rf_available":  ("🪙", "Refer: available",   "Refer & Earn"),
    "rf_moved":      ("⭐", "Refer: transferred", "Refer & Earn"),
    "rf_link":       ("🔗", "Refer: your link",   "Refer & Earn"),
    "p_requests":    ("📜", "My withdrawal requests", "Profile"),
    "dl_txt":        ("📄", "TXT download",     "Profile"),
    "dl_csv":        ("📊", "CSV download",     "Profile"),
    # --- Admin panel ---
    "a_stats":       ("📊", "Stats",           "Admin"),
    "a_reviews":     ("🧾", "Reviews",         "Admin"),
    "a_withdrawals": ("💸", "Withdrawals",     "Admin"),
    "a_cats":        ("🗂", "Categories",      "Admin"),
    "a_users":       ("👤", "Users",           "Admin"),
    "a_broadcast":   ("📣", "Broadcast",       "Admin"),
    "a_settings":    ("⚙️", "Settings",        "Admin"),
    "box":           ("📦", "Add stock",       "Admin"),
    "ok":            ("✅", "Approve",         "Admin"),
    # --- Other ---
    "money":         ("💰", "Money / balance", "Other"),
    "sale":          ("🛒", "Sales feed post", "Other"),
    "star":          ("⭐", "Highlights",      "Other"),
    "welcome":       ("⏳", "Intro beat (before /start welcome)", "Other"),
}


def _build_slots() -> dict:
    slots = dict(_STATIC_SLOTS)
    # one slot per payment rail, so each can carry its own brand mark
    import payments
    # only the rails this shop actually offers — a slot for a payment method
    # you don't take is just another row to scroll past
    for code, prov in payments.REGISTRY.items():
        if code not in cfg.providers and code != "balance":
            continue
        # no fallback: each rail's title already carries its own mark, so a
        # generic 💳 in front would double it up
        slots[f"pay_{code}"] = ("", prov.title, "Payment methods")
    return slots


SLOTS_META = _build_slots()
SLOTS = {k: v[0] for k, v in SLOTS_META.items()}


def slot_label(slot: str) -> str:
    return SLOTS_META.get(slot, ("", slot, ""))[1]


def slot_section(slot: str) -> str:
    return SLOTS_META.get(slot, ("", "", "Other"))[2]


def sections() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for slot, meta in SLOTS_META.items():
        out.setdefault(meta[2], []).append(slot)
    return out


# A leading brand mark is any run of non-ASCII characters before the first
# ASCII word — symbols like ◈ ₮ 💳 but also letter-like marks such as Ł.
_LEADING_MARK = re.compile(r"^[^\x20-\x7E]+\s*")


def label(slot: str, text: str) -> str:
    """Button text, with any leading symbol dropped when a custom icon is set.

    Payment rails carry their own mark inside the title ("◈ Binance Pay"), so
    stripping only the slot fallback isn't enough — the title's own symbol has
    to go too, or the button shows two icons side by side.
    """
    if not icon(slot):
        fallback = SLOTS.get(slot, "")
        return f"{fallback} {text}".strip()
    stripped = _LEADING_MARK.sub("", text).strip()
    # only accept the strip if a readable label survives it
    return stripped if any(ch.isascii() and ch.isalnum() for ch in stripped) else text
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
# A premium emoji written as plain text: {{e:5368324170671202286:🛒}}
# Copying a message only ever yields plain characters, so storing emoji in this
# form is what lets an admin copy, edit and paste a message back without the
# emoji ids being silently stripped along the way.
EMOJI_TOKEN = re.compile(r"\{\{e:(\d+):(.*?)\}\}", re.S)
TG_EMOJI = re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', re.S)


def expand_slots(text: str) -> str:
    """Turn {{slot}} references into concrete emoji.

    Used when handing a message to an admin to edit: they should see the actual
    emoji they can replace, not a name they'd have to look up in another menu.
    A slot with a premium icon becomes a {{e:id:char}} token, which survives
    copy-paste; one without becomes the plain character.
    """
    def sub(m: re.Match) -> str:
        name = m.group(1)
        plain = SLOTS.get(name, "")
        eid = ICONS.get(name)
        return "{{e:%s:%s}}" % (eid, plain) if eid else plain

    return PLACEHOLDER.sub(sub, text)


def tokenise(html: str) -> str:
    """Turn <tg-emoji> tags into copy-safe {{e:id:char}} tokens."""
    return TG_EMOJI.sub(lambda m: "{{e:%s:%s}}" % (m.group(1), m.group(2)), html)


async def slot_ids() -> dict[str, str]:
    """Custom emoji id per slot, from the settings table.

    One read for all 116 slots. This used to be a lookup per slot, which meant
    every rendered message cost 116 queries — unnoticeable on local SQLite,
    several seconds on a managed Postgres.
    """
    saved = await db.settings_prefix("flair:emoji:")
    return {name: v.strip() for name, v in saved.items()
            if name in SLOTS and v.strip().isdigit()}


async def render(text: str, decorated: bool = True) -> str:
    """Expand {{slot}} placeholders, and drop premium emoji if they're refused."""
    live = decorated and custom_ok()
    ids = await slot_ids() if live else {}

    def sub(m: re.Match) -> str:
        name = m.group(1)
        plain = SLOTS.get(name, "")
        eid = ids.get(name)
        return f'<tg-emoji emoji-id="{eid}">{plain}</tg-emoji>' if eid else plain

    def sub_token(m: re.Match) -> str:
        eid, plain = m.group(1), m.group(2)
        return f'<tg-emoji emoji-id="{eid}">{plain}</tg-emoji>' if live else plain

    out = EMOJI_TOKEN.sub(sub_token, text)
    out = PLACEHOLDER.sub(sub, out)
    return out if live else strip_custom(out)


async def send(bot: Bot, chat_id: int | str, text: str, **kw):
    """Send with premium emoji, degrading to plain emoji if they're rejected."""
    if custom_ok():
        try:
            return await bot.send_message(chat_id, await render(text, True), **kw)
        except TelegramBadRequest as e:
            if "emoji" not in str(e).lower():
                raise
            # The exact wording matters — "emoji is invalid" means a bad id,
            # while a permission error means Premium or a Fragment username is
            # missing. Losing it made this impossible to diagnose.
            disable_icons(str(e))
    return await bot.send_message(chat_id, await render(text, False), **kw)


async def send_sticker(bot: Bot, chat_id: int | str, slot: str, markup=None):
    """Send a configured sticker. Returns its message id, or False if none is set."""
    file_id = await db.setting(f"flair:sticker:{slot}", "")
    if not file_id:
        return False
    try:
        msg = await bot.send_sticker(chat_id, file_id, reply_markup=markup)
        return msg.message_id
    except Exception as e:
        log.warning("sticker %s failed: %s", slot, e)
        return False


async def intro_delay() -> float:
    """How long the placeholder lingers before it is deleted. Never blocks the
    welcome — the caller clears the beat in the background."""
    try:
        return max(0.0, min(float(await db.setting("flair:welcome_delay", "0")), 5))
    except ValueError:
        return 0.0


async def intro(bot: Bot, chat_id: int) -> int | None:
    """The beat before the welcome message.

    Sends a sticker if one is configured, otherwise a lone emoji — Telegram
    renders a single-emoji message large and animated, which is what gives the
    pause its weight. Returns the message id so the caller can clear it once the
    welcome has landed. Set the emoji to '-' in the panel to turn this off.

    This returns as soon as the placeholder is on screen. It does not sleep:
    holding the beat here put the delay directly in front of the welcome, which
    is the one message a buyer is waiting on. `clear_intro()` owns the wait now.
    """
    emoji = (await db.setting("flair:welcome_emoji", SLOTS["welcome"])).strip()
    # '-' (stored as empty) is the documented off switch, so it turns the whole
    # beat off — a sticker left on the slot no longer resurrects it
    if not emoji or emoji == "-":
        return None

    sent = await send_sticker(bot, chat_id, "welcome")
    msg_id = sent
    if not sent:
        if not emoji or emoji == "-":
            return None
        # a premium emoji set for the `welcome` slot wins: sent on its own it
        # renders large and animated, which is the whole point of the beat
        eid = icon("welcome")
        body = f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>' if eid else emoji
        try:
            msg = await bot.send_message(chat_id, body)
            msg_id = msg.message_id
        except Exception as e:
            if eid and ("emoji" in str(e).lower() or "entity" in str(e).lower()):
                disable_icons(str(e))
                try:
                    msg = await bot.send_message(chat_id, emoji)
                    msg_id = msg.message_id
                except Exception:
                    return None
            else:
                log.warning("intro emoji failed: %s", e)
                return None
    return msg_id if isinstance(msg_id, int) else None


async def clear_intro(bot: Bot, chat_id: int, msg_id: int | None,
                      delay: float = 0.0) -> None:
    """Remove the placeholder once the real message is on screen.

    Run this as a background task: the wait happens *after* the welcome has
    landed, so the beat costs the buyer nothing.

    Telegram only lets a bot delete its own messages for 48 hours; failing
    silently is fine because the worst case is a leftover emoji.
    """
    if not msg_id:
        return
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception as e:
        log.debug("could not delete intro message: %s", e)


# ------------------------------------------------------------- sales feed
def buyer_code(user_id: int) -> str:
    """Short, stable, non-reversible tag for a buyer.

    HMAC'd with the bot token, so the group sees that the same person bought
    twice without the id, username or name ever leaving the bot. Not reversible
    back to an account, and useless to anyone without the token.
    """
    mac = hmac.new(cfg.bot_token.encode(), str(user_id).encode(), hashlib.sha256)
    return mac.hexdigest()[:4].upper()


async def sales_chat() -> str:
    """Panel setting wins; SALES_CHAT_ID in .env is the bootstrap default."""
    return (await db.setting("flair:sales_chat", cfg.sales_chat)).strip()


def shop_button():
    """URL button — a WebApp button can't be used outside a private chat."""
    from keyboards import kb, url_btn
    if not BOT_USERNAME:
        return None
    return kb([url_btn("🛍 Open shop", f"https://t.me/{BOT_USERNAME}?start=shop",
               style="primary", icon_slot="sale")])


async def style_demo(bot: Bot, chat_id: int) -> str:
    """Send one message showing every button style + configured icon.

    Colours work for everyone. Icons only render if the account behind the bot
    has Premium, so this is the safe place to find out — it reports the exact
    Telegram error instead of breaking a live checkout button.
    """
    from keyboards import kb, btn
    await reload()
    markup = kb(
        [btn("Primary", "noop", style="primary", icon_slot="star")],
        [btn("Success", "noop", style="success", icon_slot="ok")],
        [btn("Danger", "noop", style="danger", icon_slot="sale")],
        [btn("No style", "noop")],
    )
    text = ("{{star}} <b>Button styles</b>\n\n"
            "Colours apply to every account. Icons need Premium on the bot's owner.")
    try:
        await send(bot, chat_id, text, reply_markup=markup)
        return "ok"
    except TelegramBadRequest as e:
        msg = str(e)
        if "emoji" in msg.lower() or "icon" in msg.lower():
            ICONS.clear()
            await send(bot, chat_id, text, reply_markup=kb(
                [btn("Primary", "noop", style="primary")],
                [btn("Success", "noop", style="success")],
                [btn("Danger", "noop", style="danger")]))
            return ("Colours work. Icons were refused — the account that owns the bot needs "
                    "an active Telegram Premium subscription.")
        raise


DEFAULT_SALE_TEMPLATE = "{{sale}} Someone just bought {qty}× {product}!"


def product_tag(product) -> str:
    """Product name with its own emoji, as a premium emoji when one is set."""
    if product is None:
        return ""
    name = _esc(product["name"])
    icon = icon_id(product["icon_emoji_id"])
    plain = (product["emoji"] or "").strip()
    if icon:
        return f'<tg-emoji emoji-id="{icon}">{plain or "🛍"}</tg-emoji> <b>{name}</b>'
    return f"{plain} <b>{name}</b>".strip()


async def announce_sale(bot: Bot, order, product) -> None:
    """Post a sale to the public feed.

    Anonymous by default: the template mentions the product and quantity and
    nothing that identifies the buyer. {amount} and {buyer} exist for shops that
    want them, but a feed that runs every few minutes reads better short.
    """
    chat = await sales_chat()
    if not chat:
        return

    name = product["name"] if product is not None else str(order["product_name"])
    tpl = await db.setting("flair:sale_template", DEFAULT_SALE_TEMPLATE)
    text = (tpl
            .replace("{qty}", str(order["qty"]))
            .replace("{product}", product_tag(product) or f"<b>{_esc(name)}</b>")
            .replace("{name}", _esc(name))
            .replace("{amount}", cfg.money(order["amount"]))
            .replace("{provider}", _esc(order["provider"]))
            .replace("{buyer}", buyer_code(order["user_id"])))

    markup = shop_button() if (await db.setting("flair:sale_button", "0")) == "1" else None
    try:
        await send_sticker(bot, chat, "sale")
        await send(bot, chat, text, reply_markup=markup, disable_notification=True)
    except Exception as e:
        log.warning("sales feed post failed for chat %s: %s", chat, e)


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
