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
import keyboards as k
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
    # two-letter noise, now that tokens that short are considered at all
    "is", "in", "it", "of", "on", "or", "to", "up", "we", "me", "my", "so",
    "at", "be", "by", "do", "go", "if", "no", "ok", "hi",
}
# Two, not three: short names are often the distinctive part — "tv" is the
# only thing separating Apple TV from Apple Music, and dropping it made every
# Apple product match equally. Noise at this length is handled by STOP above,
# which is a list that can be corrected; a length rule can't tell "tv" from
# "is".
MIN_TOKEN = 2


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) >= MIN_TOKEN}


def product_tokens(p) -> set[str]:
    """Words that identify this product — its name plus any keywords you add."""
    return tokens(f"{p['name']} {p['keywords'] or ''}") - STOP


def match_products(text: str, products) -> list:
    """Every in-stock product the message names, in the order it names them.

    A single distinctive word is enough ("gemini" finds Jio Gemini AI Pro 18m),
    which is how people actually ask, but generic words are ignored so a
    passing "premium" doesn't return the whole catalogue.

    Order follows the message, not the match strength. Someone who asks for
    "gemini, google and notion" is reading the reply against what they typed,
    and a list that comes back in a different order looks like the bot answered
    a different question. Ties — two products matching the same word — fall
    back to the more strongly matched, then the shorter name.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
             if len(w) >= MIN_TOKEN]
    said = set(words) - STOP
    if not said:
        return []
    hits = []
    for p in products:
        overlap = said & product_tokens(p)
        if overlap:
            hits.append((overlap, p))

    # "apple tv" matches Apple TV on {apple, tv} and Apple Music on {apple}
    # alone. The second is riding on a word the first already accounts for, so
    # it isn't a separate thing the buyer asked about — drop any match whose
    # words are wholly contained in another's.
    #
    # Deliberately not "keep only the strongest": someone asking for
    # "gemini, notion and google" names three products with no shared words,
    # and all three must survive.
    kept = [(o, p) for o, p in hits
            if not any(o < other for other, _ in hits)]

    scored = []
    for overlap, p in kept:
        first = min(words.index(w) for w in overlap)
        scored.append((first, -len(overlap), len(p["name"]), p))
    scored.sort(key=lambda t: t[:3])
    return [p for *_, p in scored[:MAX_HITS]]


def _cooled(chat_id: int) -> bool:
    now = time.time()
    if now - _last.get(chat_id, 0) < COOLDOWN:
        return False
    _last[chat_id] = now
    return True


def buy_kb(hits) -> InlineKeyboardMarkup:
    """One deep link per match, blue so they read as the action in a busy chat.

    A group reply competes with everything else on screen; a neutral button
    disappears into the conversation.

    Built through keyboards.url_btn so the `buy` slot's custom icon is attached
    the same way the product page does it. Constructing the button by hand here
    meant no icon was ever sent — and because label() strips the plain emoji
    whenever a custom icon is configured, the button ended up with neither.
    """
    rows = []
    for p in hits:
        # the same slot the product page's Buy button uses, so the icon a
        # buyer sees in the group is the icon they see after tapping through
        label = flair.label("buy", f"Buy {p['name']}")
        rows.append([k.url_btn(
            label, f"https://t.me/{flair.BOT_USERNAME}?start=p_{p['id']}",
            style="primary", icon_slot="buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text)
async def mention(m: Message) -> None:
    if await db.setting("group_autoreply", "1") != "1":
        return
    # Don't answer the shop's own staff. An admin posting a stock list or
    # price update names half the catalogue, and the bot replying underneath
    # with Buy buttons reads as spam in the shop's own group.
    if m.from_user and cfg.is_admin(m.from_user.id):
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

    # product_tag() renders each product's own premium emoji when one is set,
    # so a name looks the same in a group as it does in the shop list. Building
    # the string by hand here meant the plain fallback every time.
    names = ", ".join(flair.product_tag(p) for p in hits)
    body = await texts.t("group_hit", products=names, count=len(hits),
                         shop=esc(cfg.shop_name))
    try:
        await m.reply(await flair.render(body), reply_markup=buy_kb(hits))
    except Exception:
        pass                      # no reply permission, or the message vanished
