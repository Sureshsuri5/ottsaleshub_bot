"""Answer product mentions in groups.

The bot stays silent in groups except for this: when someone names something
you actually have in stock, it replies with a Buy button per match.

Buttons are deep links rather than callbacks — checkout happens in a private
chat, and Telegram won't allow a Mini App button in a group at all.
"""
from __future__ import annotations

import re
import time

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import flair
import texts
from config import cfg

router = Router(name="group")
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

COOLDOWN = 60          # per chat, per set of matches
MAX_HITS = 5           # never carpet the chat with buttons
_last: dict[int, float] = {}

# Words too generic to identify a product on their own. Without this, "pro"
# or "premium" would match half the catalogue on any passing message.
STOP = {
    "the", "and", "for", "you", "your", "with", "any", "one", "get", "buy", "sell",
    "need", "want", "have", "has", "available", "avail", "stock", "price", "cost",
    "how", "much", "who", "can", "hai", "bro", "plz", "please", "dm", "pm",
    "pro", "premium", "plus", "max", "ultra", "new", "old", "month", "months",
    "year", "years", "day", "days", "acc", "account", "accounts", "key", "keys",
    "ai", "app", "apps", "id", "ids",
}
MIN_TOKEN = 3


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) >= MIN_TOKEN}


def product_tokens(p) -> set[str]:
    """Words that identify this product — its name plus any keywords you add."""
    return tokens(f"{p['name']} {p['keywords'] or ''}") - STOP


def match_products(text: str, products) -> list:
    """Every in-stock product the message names, best match first.

    A single distinctive word is enough ("gemini" finds Jio Gemini AI Pro 18m),
    which is how people actually ask, but generic words are ignored so a
    passing "premium" doesn't return the whole catalogue.
    """
    said = tokens(text) - STOP
    if not said:
        return []
    scored = []
    for p in products:
        overlap = said & product_tokens(p)
        if overlap:
            scored.append((len(overlap), -len(p["name"]), p))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [p for _, _, p in scored[:MAX_HITS]]


def _cooled(chat_id: int) -> bool:
    now = time.time()
    if now - _last.get(chat_id, 0) < COOLDOWN:
        return False
    _last[chat_id] = now
    return True


def buy_kb(hits) -> InlineKeyboardMarkup:
    rows = []
    for p in hits:
        label = flair.label("g_buy", f"Buy {p['name']}")
        rows.append([InlineKeyboardButton(
            text=label, url=f"https://t.me/{flair.BOT_USERNAME}?start=p_{p['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text)
async def mention(m: Message) -> None:
    if await db.setting("group_autoreply", "1") != "1":
        return
    products = await db.products(None, only_active=True)
    if not products:
        return

    # only ever answer for something someone can actually buy right now
    live = [p for p in products
            if p["infinite"] or await db.available(p["id"]) > 0]
    hits = match_products(m.text or "", live)
    if not hits or not _cooled(m.chat.id):
        return

    names = ", ".join(f"{(p['emoji'] or '').strip()} {esc(p['name'])}".strip()
                      for p in hits)
    body = await texts.t("group_hit", products=names, count=len(hits),
                         shop=esc(cfg.shop_name))
    try:
        await m.reply(await flair.render(body), reply_markup=buy_kb(hits))
    except Exception:
        pass                      # no reply permission, or the message vanished
