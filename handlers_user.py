from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
)

import db
import delivery
import flair
import keyboards as k
import payments
import pricing
import texts
import timefmt
from config import cfg

router = Router()


class Buy(StatesGroup):
    waiting_deposit = State()
    waiting_wd_address = State()
    waiting_wd_amount = State()
    waiting_ref = State()
    waiting_topup = State()
    waiting_qty = State()


class FakeCb:
    """Lets a typed reply reuse the callback-driven checkout path."""

    def __init__(self, m: Message):
        self.message = m
        self.from_user = m.from_user
        self.bot = m.bot
        self.data = ""

    def model_copy(self, update: dict):
        clone = FakeCb(self.message)
        clone.data = update.get("data", self.data)
        return clone

    def as_(self, bot):
        self.bot = bot
        return self

    async def answer(self, *a, **kw):
        return None


def with_data(c, data: str):
    """A copy of the callback carrying different data.

    aiogram 3 models are frozen, so reusing a handler means copying the event
    rather than rewriting it in place. `.as_(c.bot)` keeps the bot bound so the
    copy can still send.
    """
    return c.model_copy(update={"data": data}).as_(c.bot)


def name_of(p) -> str:
    """Product title with its emoji, without a stray space when there isn't one."""
    emoji = (p["emoji"] or "").strip()
    return f"{emoji} {esc(p['name'])}" if emoji else esc(p["name"])


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def show(c: CallbackQuery, text: str, markup=None) -> bool:
    """Edit the current screen in place. Returns True if anything changed.

    Falls back to a new message when the current one can't hold the text (e.g.
    it's the QR photo). An identical edit is not an error — Telegram rejects it
    with "message is not modified", which just means the screen is already
    current, so we swallow that one rather than posting a duplicate."""
    msg = c.message
    try:
        if msg.photo or msg.document:
            await msg.edit_caption(caption=text, reply_markup=markup)
        else:
            await msg.edit_text(text, reply_markup=markup)
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            return False
        try:
            await msg.answer(text, reply_markup=markup)
        except Exception:
            pass
        return True


DEFAULT_WELCOME = (
    "🏪 <b>Welcome to {shop}!</b>\n\n"
    "Hey <b>{name}</b>! 👋\n\n"
    "We offer premium digital products at the best prices. Fast, secure, "
    "and fully automated delivery.\n\n"
    "<blockquote>"
    "🏪 <b>Shop</b> — Browse &amp; buy products\n"
    "💵 <b>Deposit</b> — Add funds to your wallet\n"
    "👤 <b>My Profile</b> — Balance, orders &amp; settings\n"
    "🆘 <b>Support</b> — Get help\n"
    "⭐ <b>Refer &amp; Earn</b> — Invite friends &amp; earn rewards"
    "</blockquote>"
)


async def menu_text(uid: int) -> str:
    u = await db.get_user(uid)
    body = await texts.t("welcome", shop=esc(cfg.shop_name),
                         name=esc((u["first_name"] if u else "") or "there"))

    links = []
    if cfg.channel_url:
        links.append(f'{{{{w_channel}}}} Channel: '
                     f'<a href="{cfg.channel_url}">{esc(cfg.channel_url.split("/")[-1])}</a>')
    if cfg.group_url:
        links.append(f'{{{{w_group}}}} Group: '
                     f'<a href="{cfg.group_url}">{esc(cfg.group_url.split("/")[-1])}</a>')

    parts = [body]
    if links:
        parts.append("\n".join(links))
    # balance lives behind My Profile — a figure on the public welcome screen is
    # the one thing a buyer might not want visible while screen-sharing
    parts.append(await texts.t("menu_footer"))
    return await flair.render("\n\n".join(parts))


def referral_link(uid: int) -> str:
    return f"https://t.me/{flair.BOT_USERNAME}?start=ref_{db.ref_code(uid)}"


# ------------------------------------------------------------------ start
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)

    # deep link: /start ref_<code>.
    # Don't gate on "row didn't exist" — the ban middleware creates the row
    # before any handler runs, so that test is always false. set_referrer()
    # already refuses a second binding and refuses self-referral; the extra
    # guard here is that an existing customer can't be claimed by someone else.
    parts = (m.text or "").split(maxsplit=1)

    # /start p_<id> — arrived from a group reply, so open that product
    if len(parts) > 1 and parts[1].startswith("p_") and parts[1][2:].isdigit():
        p = await db.product(int(parts[1][2:]))
        if p and p["is_active"]:
            await m.answer(await product_text(p, m.from_user.id),
                           reply_markup=k.product_kb(p["id"],
                                                     await db.available(p["id"]) > 0))
            return

    if len(parts) > 1 and parts[1].startswith("ref_"):
        row = await db.user_by_ref_code(parts[1][4:])
        inviter = row["tg_id"] if row else 0
        already_a_customer = await db.delivered_purchases(m.from_user.id) > 0
        if inviter and not already_a_customer \
                and await db.set_referrer(m.from_user.id, inviter):
            try:
                await m.bot.send_message(
                    inviter, f"⭐ <b>{esc(m.from_user.first_name or 'Someone')}</b> joined "
                             f"through your link.\nYou earn when they make their first purchase.")
            except Exception:
                pass
    # Render the welcome *while* the placeholder is being sent, then let the
    # placeholder clean itself up in the background. Nothing between /start and
    # the welcome is serial any more except the two sends themselves.
    text = asyncio.create_task(menu_text(m.from_user.id))
    intro_id = await flair.intro(m.bot, m.chat.id)
    delay = await flair.intro_delay() if intro_id else 0.0
    await send_menu(m, await text)
    if intro_id:
        asyncio.create_task(flair.clear_intro(m.bot, m.chat.id, intro_id, delay))


async def send_menu(m: Message, text: str | None = None) -> None:
    """Send the welcome, and survive an admin having saved markup Telegram
    rejects — the shop stays open, and the admin is told which message broke."""
    markup = k.main_menu(cfg.is_admin(m.from_user.id))
    if text is None:
        text = await menu_text(m.from_user.id)
    try:
        return await m.answer(text, reply_markup=markup)
    except Exception as e:
        if "parse" not in str(e).lower() and "entity" not in str(e).lower() \
                and "tag" not in str(e).lower():
            raise
        await db.ex("DELETE FROM settings WHERE key = 'text:welcome'")
        await delivery.notify_admins(
            m.bot, "⚠️ Your <b>Welcome message</b> contained markup Telegram "
                   f"rejected, so it has been reset to the default.\n\n"
                   f"<code>{esc(str(e)[:200])}</code>")
        await m.answer(await menu_text(m.from_user.id), reply_markup=markup)


@router.message(Command("menu"))
async def menu_cmd(m: Message, state: FSMContext):
    await state.clear()
    await send_menu(m)


@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, await menu_text(c.from_user.id), k.main_menu(cfg.is_admin(c.from_user.id)))
    await c.answer()


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "fsmcancel")
async def fsm_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "Cancelled.", k.home_kb())
    await c.answer()


DEFAULT_SUPPORT = "Contact our support team directly:"


@router.callback_query(F.data == "menu:support")
async def support(c: CallbackQuery, state: FSMContext):
    await state.clear()
    body = await texts.t("support_title") + "\n\n" + await texts.t("support")
    if not cfg.support_url:
        body += "\n\n" + await texts.t("support_unset")
    await show(c, body, k.support_kb())
    await c.answer()


# ----------------------------------------------------------------- browse
@router.callback_query(F.data == "shop")
@router.callback_query(F.data.startswith("shop:"))
async def shop(c: CallbackQuery):
    page = int(c.data.split(":")[1]) if ":" in c.data else 0
    prods = await db.products(None, only_active=True)
    if not prods:
        await show(c, await texts.t("shop_empty"), k.home_kb())
        return await c.answer()
    counts = {p["id"]: await db.stock_count(p["id"]) for p in prods}
    live = sum(1 for p in prods if p["infinite"] or counts[p["id"]])
    # the clock makes every refresh a distinct edit, so Telegram never rejects
    # it as "message is not modified" and the tap always feels like it did work
    stamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
    await show(c,
               await texts.t("shop_header") + "\n\n"
               + await texts.t("shop_meta", live=live, total=len(prods), time=stamp),
               k.shop_kb(prods, counts, page))
    await c.answer()


@router.callback_query(F.data.startswith("prod:"))
async def open_product(c: CallbackQuery, state: FSMContext):
    await _render_product(c, int(c.data.split(":")[1]), state)


def _unit_plural(unit: str) -> str:
    """'key' -> 'keys' for the quantity question. Naive on purpose: it only has
    to read naturally for short nouns an admin types as a price unit."""
    unit = (unit or "").strip()
    if not unit:
        return "units"
    if unit.endswith(("s", "x", "z", "ch", "sh")):
        return unit + "es"
    if unit.endswith("y") and unit[-2:-1] not in "aeiou":
        return unit[:-1] + "ies"
    return unit + "s"


async def product_text(p, uid: int) -> str:
    """The product screen body. Shared by the in-bot screen and the deep link
    someone follows from a group reply, so both always read the same."""
    avail = await db.available(p["id"])
    unit = (p["unit"] or "").strip()
    price = await pricing.price_for(p, uid)
    tier = await pricing.label(uid)

    line = await texts.t("product_price", price=cfg.money(price),
                         unit=f" / {esc(unit)}" if unit else "")
    if price < p["price"] - 1e-9:
        line += await texts.t("product_was", was=cfg.money(p["price"]))
    lines = [f"<b>{name_of(p)}</b>", "", line]
    if tier:
        lines.append(await texts.t("product_tier", tier=esc(tier)))
    if p["description"].strip():
        lines += ["", await texts.t("product_desc",
                                    description=esc(p["description"]))]

    # stock is already on every row of the list, so only mention it when it
    # changes what the buyer can do right now
    if not p["infinite"]:
        if avail == 0:
            lines += ["", await texts.t("product_soldout")]
        elif avail <= 5:
            lines += ["", await texts.t("product_low", left=avail)]
    if avail or p["infinite"]:
        lines += ["", await texts.t("product_delivery")]
    return "\n".join(lines)


async def _render_product(c: CallbackQuery, pid: int, state: FSMContext):
    p = await db.product(pid)
    if not p or not p["is_active"]:
        return await c.answer("Unavailable.", show_alert=True)
    await state.update_data(pid=pid)
    avail = await db.available(pid)
    await show(c, await product_text(p, c.from_user.id),
               k.product_kb(pid, avail > 0 or bool(p["infinite"])))
    await c.answer()


@router.callback_query(F.data.startswith("buy:"))
async def choose_qty(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    p = await db.product(pid)
    if not p:
        return await c.answer("Unavailable.", show_alert=True)
    avail = await db.available(pid)
    if avail < 1:
        return await c.answer("Sold out.", show_alert=True)

    unit = (p["unit"] or "").strip()
    unit_price = await pricing.price_for(p, c.from_user.id)
    price_line = cfg.money(unit_price) + (f" / {esc(unit)}" if unit else "")
    await show(c,
               await texts.t("qty_title") + "\n\n"
               f"{name_of(p)}\n{price_line}\n\n"
               + await texts.t("qty_question", unit=_unit_plural(unit)),
               k.qty_kb(pid, min(avail, 99)))
    await c.answer()


@router.callback_query(F.data.startswith("q:"))
async def qty_chosen(c: CallbackQuery, state: FSMContext):
    _, pid, qty = c.data.split(":")
    await _to_payment(c, state, int(pid), int(qty))


@router.callback_query(F.data.startswith("qcustom:"))
async def qty_custom(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    p = await db.product(pid)
    avail = await db.available(pid)
    await state.set_state(Buy.waiting_qty)
    await state.update_data(qty_pid=pid)
    await show(c,
               f"<b>Custom quantity</b>\n\n"
               f"How many {_unit_plural(p['unit'])} of "
               f"<b>{esc(p['name'])}</b> do you want?\n"
               f"<i>1 to {min(avail, 99)}</i>",
               k.cancel_kb())
    await c.answer()


@router.message(Buy.waiting_qty)
async def qty_typed(m: Message, state: FSMContext):
    pid = (await state.get_data()).get("qty_pid")
    avail = await db.available(pid)
    try:
        qty = int((m.text or "").strip())
    except ValueError:
        return await m.answer("Send a whole number, e.g. 12", reply_markup=k.cancel_kb())
    if not 1 <= qty <= min(avail, 99):
        return await m.answer(f"Pick a number between 1 and {min(avail, 99)}.",
                              reply_markup=k.cancel_kb())
    await state.clear()
    p = await db.product(pid)
    await state.update_data(**{f"q{pid}": qty})
    u = await db.get_user(m.from_user.id)
    bal = u["balance"] if u else 0.0
    unit_price = await pricing.price_for(p, m.from_user.id)
    await m.answer(await order_summary(p, qty, bal, unit_price),
                   reply_markup=k.providers_kb(pid, qty, balance=bal,
                                               total=round(unit_price * qty, 2)))


async def order_summary(p, qty: int, balance: float | None = None,
                        unit_price: float | None = None) -> str:
    unit = (p["unit"] or "").strip()
    each = f"per {esc(unit)}" if unit else "each"
    title = await texts.t("summary_title")
    prompt = await texts.t("pay_choose")
    price = p["price"] if unit_price is None else unit_price
    total = round(price * qty, 2)
    wallet = ""
    if balance is not None:
        short = total - balance
        wallet = (f"\n👛 Wallet: <b>{cfg.money(balance)}</b>"
                  + (f" · {cfg.money(short)} short" if short > 1e-9 else " · covers this"))
    return (title + "\n\n"
            f"{name_of(p)}\n"
            f"🔢 Qty: <b>{qty}</b>\n"
            f"💰 Price: <b>{cfg.money(price)}</b> {each}"
            + (f"  <s>{cfg.money(p['price'])}</s>" if price < p["price"] - 1e-9 else "")
            + "\n"
            f"💵 Total: <b>{cfg.money(total)}</b>{wallet}\n\n"
            + prompt)


async def _to_payment(c: CallbackQuery, state: FSMContext, pid: int, qty: int) -> None:
    p = await db.product(pid)
    avail = await db.available(pid)
    qty = max(1, min(qty, max(1, avail)))
    await state.update_data(**{f"q{pid}": qty})
    u = await db.get_user(c.from_user.id)
    bal = u["balance"] if u else 0.0
    unit_price = await pricing.price_for(p, c.from_user.id)
    await show(c, await order_summary(p, qty, bal, unit_price),
               k.providers_kb(pid, qty, balance=bal, total=round(unit_price * qty, 2)))
    await c.answer()


@router.callback_query(F.data.startswith("pgroup:"))
async def payment_rails(c: CallbackQuery, state: FSMContext):
    _, kind, pid, qty, group = c.data.split(":")
    pid, qty = int(pid), int(qty)
    if kind == "topup":
        head = f"Top up <b>{cfg.money(qty / 100 if qty > 1000 else qty)}</b>"
    else:
        p = await db.product(pid)
        head = (f"{name_of(p)} × {qty}\n"
                f"Total: <b>{cfg.money(p['price'] * qty)}</b>")
    await show(c,
               f"💳 <b>Select Payment Method</b>\n\n{head}",
               k.rails_kb(pid, qty, kind, group))
    await c.answer()


@router.callback_query(F.data.startswith("needfunds:"))
async def need_funds(c: CallbackQuery, state: FSMContext):
    """Wallet chosen but short — send them to Deposit instead of a dead end."""
    u = await db.get_user(c.from_user.id)
    await c.answer(f"Your wallet has {cfg.money(u['balance'])}. Top up to pay this way.",
                   show_alert=True)
    await balance(c, state)


@router.callback_query(F.data == "cancelbuy")
async def cancel_before_paying(c: CallbackQuery, state: FSMContext):
    """Abandon checkout before an order exists — nothing to refund or expire."""
    await state.clear()
    await c.answer("Order cancelled")
    await shop(with_data(c, "shop"))


@router.callback_query(F.data.startswith("pay:"))
async def create_order(c: CallbackQuery, state: FSMContext):
    _, kind, pid, qty, code = c.data.split(":")
    pid, qty = int(pid), int(qty)
    prov = payments.get(code)
    if not prov:
        return await c.answer("That method is unavailable.", show_alert=True)

    if kind == "purchase":
        p = await db.product(pid)
        if not p or not p["is_active"]:
            return await c.answer("Product unavailable.", show_alert=True)
        if not p["infinite"] and await db.stock_count(pid) < qty:
            return await c.answer("Not enough stock left.", show_alert=True)
        # recomputed from the buyer's tier at the moment of purchase
        amount = round(await pricing.price_for(p, c.from_user.id) * qty, 2)
        name = p["name"]
    else:
        amount = float(pid) / 100
        name = "Wallet top-up"
        pid, qty = None, 1

    # ---- wallet balance settles immediately -------------------------------
    if code == "balance":
        user = await db.get_user(c.from_user.id)
        if user["balance"] + 1e-9 < amount:
            return await c.answer(
                f"Balance too low. You have {cfg.money(user['balance'])}.", show_alert=True)
        oid = await db.create_order(
            user_id=c.from_user.id, kind="purchase", product_id=pid, product_name=name,
            qty=qty, amount=amount, provider="balance", pay_amount=amount, pay_unit=cfg.fiat)
        await db.add_balance(c.from_user.id, -amount)
        await show(c, "⏳ Processing…")
        if not await delivery.settle(c.bot, oid):
            await c.message.answer("Something went wrong — you have not been charged.",
                                   reply_markup=k.home_kb())
        return await c.answer()

    # ---- everything else creates a pending order --------------------------
    # A unique amount is what lets an incoming transfer be tied to one order.
    # Every chain rail needs it, not just TRON — without it two buyers paying
    # the same price produce transfers nothing can tell apart.
    if hasattr(prov, "unique_amount"):
        pay_amount = await prov.unique_amount(amount)
        pay_unit = prov.quote(amount)[1]
    else:
        pay_amount, pay_unit = prov.quote(amount)

    oid = await db.create_order(
        user_id=c.from_user.id, kind=kind, product_id=pid, product_name=name, qty=qty,
        amount=amount, provider=code, pay_amount=pay_amount, pay_unit=pay_unit,
        # Only an open-amount DEPOSIT is timerless — the buyer is switching apps
        # and there's no reserved amount to protect. A purchase always gets a
        # clock, or abandoned checkouts pile up in the open-orders list forever.
        expires_at=None if (kind == "topup" and payments.is_variable(code))
        else db.in_minutes(cfg.order_ttl))

    inv = await prov.create(await db.order(oid))
    if inv.pay_address:
        await db.set_order(oid, pay_address=inv.pay_address)

    try:
        await c.message.delete()
    except Exception:
        pass

    # Telegram Stars: hand off to Telegram's own invoice + verification
    if inv.native_stars:
        await c.bot.send_invoice(
            chat_id=c.from_user.id,
            title=name[:32],
            description=f"Order #{oid} · {cfg.money(amount)}",
            payload=f"order:{oid}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=name[:32], amount=inv.native_stars)],
        )
        return await c.answer()

    # a manual rail waits on a pasted reference, so open that input immediately
    #
    # This used to ask `is_variable(code)`, which is true for every crypto rail
    # because they can all take an open-ended deposit. That is a property of the
    # rail, not of this order — so a normal BEP20 purchase was treated as though
    # the bot were waiting for a typed reference and got the stripped-down
    # keyboard: Back only, no Cancel, no Check payment. What actually matters is
    # whether *this* invoice wants a reference, which is what manual_ref means.
    awaiting = bool(inv.manual_ref)
    if inv.manual_ref:
        await state.set_state(Buy.waiting_ref)
        await state.update_data(review_oid=oid)

    header = (f"<b>{prov.title}</b>\n\n" if awaiting or kind == "topup"
              else f"🧾 <b>Order #{oid}</b> — {esc(name)} ×{qty}\n\n")
    back_to = "menu:balance" if kind == "topup" else "shop"
    if inv.qr_payload:
        photo = BufferedInputFile(payments.qr_png(inv.qr_payload), filename="pay.png")
        await c.bot.send_photo(c.from_user.id, photo,
                               caption=await flair.render(header + inv.text),
                               reply_markup=k.invoice_kb(oid, inv.manual_ref, awaiting,
                                                           back_to, inv.pay_url))
    else:
        await c.bot.send_message(c.from_user.id, await flair.render(header + inv.text),
                                 reply_markup=k.invoice_kb(oid, inv.manual_ref, awaiting,
                                                           back_to, inv.pay_url))
    await c.answer()


@router.callback_query(F.data.startswith("chk:"))
async def check_now(c: CallbackQuery, state: FSMContext):
    oid = int(c.data.split(":")[1])
    o = await db.order(oid)
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Unknown order.", show_alert=True)
    if o["status"] in {"paid", "delivered"}:
        return await c.answer("Already paid ✅", show_alert=True)
    if o["status"] != "pending":
        return await c.answer(f"Order is {o['status']}.", show_alert=True)

    if o["provider"] == "upi" and not cfg.webhook_enabled:
        await state.set_state(Buy.waiting_ref)
        await state.update_data(review_oid=oid)
        await c.message.answer(
            "Send the 12-digit UTR / transaction reference from your UPI app.",
            reply_markup=k.cancel_kb())
        return await c.answer()

    found = await payments.get(o["provider"]).poll([o])
    if found:
        for _oid, ref in found:
            await delivery.settle(c.bot, _oid, ref)
        return await c.answer("Payment confirmed ✅", show_alert=True)
    await c.answer("No payment seen yet. It can take a minute — I'm also checking "
                   "automatically in the background.", show_alert=True)


@router.message(Buy.waiting_ref)
async def got_ref(m: Message, state: FSMContext):
    ref = (m.text or "").strip()
    if not texts.valid_ref(ref):
        return await m.answer(
            "That doesn't look like a transaction reference.\n\n"
            "Paste the <b>transaction hash</b> or <b>ID</b> from your wallet or "
            "exchange — one value, no spaces.",
            reply_markup=k.cancel_kb())
    oid = (await state.get_data()).get("review_oid")
    await state.clear()
    if not await db.mark_seen(f"utr:{ref}", oid):
        return await m.answer("That reference has already been submitted.",
                              reply_markup=k.home_kb())
    o = await db.order(oid)
    prov = payments.get(o["provider"])
    if hasattr(prov, "verify_ref"):
        found = await prov.verify_ref(ref)
        if found:
            await db.set_order(oid, amount=found if not o["amount"] else o["amount"])
            await db.set_order(oid, status="pending")
            if await delivery.settle(m.bot, oid, ref=ref):
                return
    await db.set_order(oid, status="awaiting_review", external_ref=ref)
    o = await db.order(oid)
    note = (await texts.t("ref_received_topup") if o["kind"] == "topup" and not o["amount"]
            else await texts.t("ref_received", oid=oid))
    await m.answer(note, reply_markup=k.home_kb())
    amt = cfg.money(o["amount"]) if o["amount"] else "<i>amount to be entered</i>"
    # explain why the automatic check didn't settle it — otherwise every manual
    # review looks the same and you can't tell a misconfiguration from a typo
    hint = ""
    if hasattr(prov, "diagnose"):
        try:
            hint = f"\n<i>Auto-check: {await prov.diagnose()}</i>"
        except Exception:
            hint = ""
    await delivery.notify_admins(
        m.bot, f"🔎 <b>Review needed</b> — order #{oid} ({o['provider']})\n"
               f"User <code>{m.from_user.id}</code> · {amt}\n"
               f"Ref: <code>{esc(ref)}</code>{hint}")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_order(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = await db.order(oid)
    if o and o["user_id"] == c.from_user.id and o["status"] == "pending":
        await db.set_order(oid, status="cancelled")
        try:
            await c.message.delete()
        except Exception:
            pass
        await c.message.answer(await texts.t("order_cancelled", oid=oid),
                               reply_markup=k.home_kb())
    await c.answer()


# -------------------------------------------------------- telegram stars
@router.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    try:
        oid = int(pcq.invoice_payload.split(":")[1])
    except Exception:
        return await pcq.answer(ok=False, error_message="Malformed order.")
    o = await db.order(oid)
    if not o or o["status"] != "pending":
        return await pcq.answer(ok=False, error_message="This order is no longer open.")
    if o["kind"] == "purchase" and o["product_id"]:
        p = await db.product(o["product_id"])
        if not p or (not p["infinite"] and await db.stock_count(p["id"]) < o["qty"]):
            return await pcq.answer(ok=False, error_message="Just went out of stock, sorry.")
    await pcq.answer(ok=True)


@router.message(F.successful_payment)
async def stars_paid(m: Message):
    sp = m.successful_payment
    await delivery.settle(m.bot, int(sp.invoice_payload.split(":")[1]),
                          ref=sp.telegram_payment_charge_id)


# --------------------------------------------------------------- account
@router.callback_query(F.data.startswith("dep:"))
async def deposit_rail(c: CallbackQuery, state: FSMContext):
    """A chain rail asks for the amount first — a stated figure gets its own
    unique value, which is what lets the transfer be matched automatically with
    nothing for the buyer to paste. Manual rails take whatever arrives."""
    code = c.data.split(":")[1]
    prov = payments.get(code)
    if not getattr(prov, "asks_amount", False):
        return await create_order(with_data(c, f"pay:topup:0:1:{code}"), state)

    await state.set_state(Buy.waiting_deposit)
    await state.update_data(dep_code=code)
    unit = prov.quote(1)[1]
    await show(c,
               f"{prov.title} <b>(Auto-Verify)</b>\n\n"
               f"💱 Enter the amount to deposit in <b>{esc(unit)}</b> "
               f"(example: <code>10</code>).\n"
               f"<i>Minimum: {cfg.money(cfg.min_deposit)}</i>\n\n"
               "You'll get an exact amount to send. It's credited automatically "
               "once the transfer confirms — nothing to paste.",
               k.cancel_kb())
    await c.answer()


@router.message(Buy.waiting_deposit)
async def deposit_amount_typed(m: Message, state: FSMContext):
    code = (await state.get_data()).get("dep_code", "")
    prov = payments.get(code)
    try:
        amount = round(float((m.text or "").strip().replace(",", "")), 2)
    except ValueError:
        return await m.answer("Send a number, e.g. 10", reply_markup=k.cancel_kb())
    if amount < cfg.min_deposit:
        return await m.answer(f"Minimum deposit is {cfg.money(cfg.min_deposit)}.",
                              reply_markup=k.cancel_kb())
    await state.clear()
    await m.answer("Preparing your payment details…")
    fake = with_data(FakeCb(m), f"pay:topup:{int(amount * 100)}:1:{code}")
    await create_order(fake, state)


@router.callback_query(F.data == "menu:balance")
async def balance(c: CallbackQuery, state: FSMContext):
    await state.clear()
    # no balance here — it's on My Profile, and a figure on the deposit screen
    # is the one thing a buyer might not want visible while screen-sharing
    await show(c, await texts.t("deposit_title") + "\n\n"
               + await texts.t("deposit_sub"),
               k.deposit_kb())
    await c.answer()


@router.callback_query(F.data.startswith("depamt:"))
async def deposit_amount(c: CallbackQuery, state: FSMContext):
    """Rails that need a figure up front (Stars, UPI)."""
    code = c.data.split(":")[1]
    prov = payments.get(code)
    await state.clear()

    # Say which currency this rail actually moves. Amounts are chosen in the
    # shop currency, but UPI settles in rupees and the chains in USDT — showing
    # the conversion here stops the invoice being a surprise.
    unit, rate, symbol = cfg.fiat, 1.0, cfg.symbol
    if prov:
        sample, unit = prov.quote(1.0)
        rate = sample or 1.0
        symbol = "₹" if unit.upper() == "INR" else ""

    foreign = unit.upper() != cfg.fiat.upper() and rate > 0
    note = (f"\n\n<i>Credited to your wallet in {cfg.fiat} — "
            f"{symbol}{rate:,.0f} is about {cfg.money(1)}.</i>" if foreign else "")
    await show(c, f"{prov.title if prov else 'Deposit'}\n\n"
                  f"How much would you like to add?\n"
                  f"Choose or type an amount in <b>{esc(unit)}</b>.{note}",
               k.deposit_amount_kb(code, unit, rate, symbol))
    await c.answer()


@router.callback_query(F.data.startswith("top:"))
async def topup_preset(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    paise, code = int(parts[1]), (parts[2] if len(parts) > 2 else "")
    if code:
        return await create_order(with_data(c, f"pay:topup:{paise}:1:{code}"), state)
    await show(c, f"Top up {cfg.money(paise / 100)} — choose a method:",
               k.providers_kb(paise, 1, kind="topup"))
    await c.answer()


@router.callback_query(F.data.startswith("topup"))
async def topup_custom(c: CallbackQuery, state: FSMContext):
    code = c.data.split(":")[1] if ":" in c.data else ""
    prov = payments.get(code) if code else None
    unit = prov.quote(1.0)[1] if prov else cfg.fiat
    await state.set_state(Buy.waiting_topup)
    await state.update_data(topup_code=code)
    await show(c, f"How much would you like to add?\n"
                  f"Send an amount in <b>{esc(unit)}</b>.", k.cancel_kb())
    await c.answer()


def _to_shop_currency(amount: float, code: str) -> float:
    """A figure typed in the rail's currency, expressed in the shop's.

    Kept in one place so the preset buttons and a typed amount can't ever
    disagree about what a buyer just asked for.
    """
    prov = payments.get(code) if code else None
    if not prov:
        return amount
    rate, unit = prov.quote(1.0)
    if unit.upper() == cfg.fiat.upper() or not rate:
        return amount
    return round(amount / rate, 2)


@router.message(Buy.waiting_topup)
async def topup_amount(m: Message, state: FSMContext):
    try:
        amount = round(float((m.text or "").strip().replace(",", "")), 2)
        assert amount > 0
    except Exception:
        return await m.answer("Send a number, e.g. 500", reply_markup=k.cancel_kb())
    code = (await state.get_data()).get("topup_code", "")
    await state.clear()
    if code:
        # they typed it in the rail's currency; the wallet is in the shop's
        shop_amount = _to_shop_currency(amount, code)
        prov = payments.get(code)
        unit = prov.quote(1.0)[1]
        line = (f"Top up {cfg.money(shop_amount)} via {prov.title}."
                if unit.upper() == cfg.fiat.upper() else
                f"Paying {amount:,.2f} {esc(unit)} via {prov.title} — "
                f"<b>{cfg.money(shop_amount)}</b> will be added to your wallet.")
        await m.answer(line)
        return await m.answer("Opening payment…",
                              reply_markup=k.kb([k.btn("Continue",
                                  f"pay:topup:{int(round(shop_amount * 100))}:1:{code}")]))
    await m.answer(f"Top up {cfg.money(amount)} — choose a method:",
                   reply_markup=k.providers_kb(int(amount * 100), 1, kind="topup"))


@router.callback_query(F.data == "menu:profile")
async def profile(c: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await db.get_user(c.from_user.id)
    await show(c, await texts.t("profile_body",
                                id=u["tg_id"], balance=cfg.money(u["balance"]),
                                joined=esc(u["created_at"][:10]),
                                name=esc(u["first_name"] or "")),
               k.profile_kb())
    await c.answer()


@router.callback_query(F.data == "pf:stats")
async def my_stats(c: CallbackQuery):
    uid = c.from_user.id
    u = await db.get_user(uid)
    orders = await db.user_orders(uid, 1000)
    done = [o for o in orders if o["status"] == "delivered" and o["kind"] == "purchase"]
    spent = sum(o["amount"] for o in done)
    units = sum(o["qty"] for o in done)
    tops = sum(o["amount"] for o in orders
               if o["kind"] == "topup" and o["status"] == "delivered")
    ref = await db.referral_stats(uid)
    locked = await db.locked_balance(uid)
    fav = max({o["product_name"] for o in done},
              key=lambda n: sum(o["qty"] for o in done if o["product_name"] == n),
              default=None)
    favourite = (await texts.t("stats_favourite", product=esc(fav)) if fav
                 else await texts.t("stats_none"))
    await show(c, await texts.t(
        "stats_body",
        orders=len(done), items=units, spent=cfg.money(spent),
        deposited=cfg.money(tops), balance=cfg.money(u["balance"]),
        held=f" ({cfg.money(locked)} on hold)" if locked else "",
        invited=ref["invited"], buyers=ref["buyers"],
        earnings=cfg.money(ref["earned"]), favourite=favourite),
        k.back_to("menu:profile"))
    await c.answer()


@router.callback_query(F.data == "pf:notify")
async def notifications(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    await show(c, "🔔 <b>Notifications</b>\n\n"
                  "Order updates cover deliveries, refunds and payment confirmations.\n"
                  "Announcements are shop news and offers.",
               k.notify_kb(u))
    await c.answer()


@router.callback_query(F.data.startswith("pf:toggle:"))
async def notify_toggle(c: CallbackQuery):
    field = c.data.split(":")[2]
    u = await db.get_user(c.from_user.id)
    await db.set_notify(c.from_user.id, field, not u[field])
    u = await db.get_user(c.from_user.id)
    await show(c, "🔔 <b>Notifications</b>\n\n"
                  "Order updates cover deliveries, refunds and payment confirmations.\n"
                  "Announcements are shop news and offers.",
               k.notify_kb(u))
    await c.answer("Saved")


@router.callback_query(F.data == "pf:api")
async def api_screen(c: CallbackQuery):
    if not cfg.api_enabled:
        return await c.answer("The API is turned off.", show_alert=True)
    u = await db.get_user(c.from_user.id)
    key = u["api_key"]
    base = cfg.webapp_url or "https://your-shop-domain"
    body = (f"🔑 <b>Developer API</b>\n\n"
            "Buy from your own scripts using your wallet balance.\n\n")
    if key:
        body += (f"Your key:\n<code>{esc(key)}</code>\n"
                 "👆 <i>Tap to copy · treat it like a password</i>\n\n")
    else:
        body += "You don't have a key yet.\n\n"
    body += (f"<blockquote>GET  {base}/api/v1/products\n"
             f"GET  {base}/api/v1/balance\n"
             f"POST {base}/api/v1/purchase\n"
             "     {\"product_id\": 1, \"qty\": 2}\n\n"
             "Header: X-API-Key: your_key</blockquote>")
    await show(c, body, k.api_kb(bool(key)))
    await c.answer()


@router.callback_query(F.data == "pf:apikey")
async def api_rotate(c: CallbackQuery):
    await db.issue_api_key(c.from_user.id)
    await c.answer("New key issued — the old one stopped working.", show_alert=True)
    await api_screen(with_data(c, "pf:api"))


@router.callback_query(F.data == "menu:refer")
async def refer(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    r = await db.referral_stats(uid)
    link = referral_link(uid)

    terms = []
    if cfg.ref_percent:
        terms.append(await texts.t(
            "refer_percent", percent=f"{cfg.ref_percent:g}",
            what="purchase/deposit" if cfg.ref_on_deposit else "purchase"))
    if cfg.ref_bonus:
        terms.append(await texts.t("refer_bonus", bonus=cfg.money(cfg.ref_bonus)))
    terms.append(await texts.t("refer_transfer"))

    await show(c,
               await texts.t("refer_body",
                             day=r["day"], week=r["week"], total=r["invited"],
                             earned=cfg.money(r["earned"]),
                             available=cfg.money(r["available"]),
                             transferred=cfg.money(r["transferred"]),
                             terms="\n\n".join(terms), link=link),
               k.refer_kb(link, r["available"] > 0))
    await c.answer()


@router.callback_query(F.data == "refer:transfer")
async def refer_transfer(c: CallbackQuery, state: FSMContext):
    moved = await db.transfer_referral(c.from_user.id)
    if not moved:
        return await c.answer("Nothing to transfer yet.", show_alert=True)
    u = await db.get_user(c.from_user.id)
    await c.answer(f"{cfg.money(moved)} moved to your wallet.", show_alert=True)
    await refer(c, state)


@router.callback_query(F.data == "refer:none")
async def refer_nothing(c: CallbackQuery):
    await c.answer("No referral earnings available yet.", show_alert=True)


# ------------------------------------------------------------- withdraw
@router.callback_query(F.data == "pf:withdraw")
async def withdraw(c: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await db.get_user(c.from_user.id)
    locked = await db.locked_balance(c.from_user.id)
    free = round(u["balance"] - locked, 2)
    can = free >= cfg.min_withdrawal
    body = (f"💸 <b>Withdraw</b>\n\n"
            f"Available: <b>{cfg.money(free)}</b>\n")
    if locked:
        body += f"On hold in open requests: {cfg.money(locked)}\n"
    body += f"Minimum: {cfg.money(cfg.min_withdrawal)}\n\n"
    body += ("Choose where to send it." if can else
             "Not enough available to withdraw yet.")
    await show(c, body, k.withdraw_kb(cfg.withdraw_methods, can))
    await c.answer()


@router.callback_query(F.data.startswith("wd:"))
async def withdraw_method(c: CallbackQuery, state: FSMContext):
    idx = int(c.data.split(":")[1])
    method = cfg.withdraw_methods[idx]
    await state.set_state(Buy.waiting_wd_address)
    await state.update_data(wd_method=method)
    await show(c, f"💸 <b>{esc(method)}</b>\n\nSend the address or ID to pay you at.\n"
               + await texts.t("withdraw_note"), k.cancel_kb())
    await c.answer()


@router.message(Buy.waiting_wd_address)
async def withdraw_address(m: Message, state: FSMContext):
    addr = (m.text or "").strip()
    if not 6 <= len(addr) <= 120:
        return await m.answer("That doesn't look like a valid address.",
                              reply_markup=k.cancel_kb())
    await state.update_data(wd_address=addr)
    await state.set_state(Buy.waiting_wd_amount)
    u = await db.get_user(m.from_user.id)
    free = round(u["balance"] - await db.locked_balance(m.from_user.id), 2)
    await m.answer(f"How much? Available <b>{cfg.money(free)}</b>, minimum "
                   f"{cfg.money(cfg.min_withdrawal)}.", reply_markup=k.cancel_kb())


@router.message(Buy.waiting_wd_amount)
async def withdraw_amount(m: Message, state: FSMContext):
    uid = m.from_user.id
    try:
        amount = round(float((m.text or "").strip().replace(",", "")), 2)
    except ValueError:
        return await m.answer("Send a number.", reply_markup=k.cancel_kb())
    u = await db.get_user(uid)
    free = round(u["balance"] - await db.locked_balance(uid), 2)
    if amount < cfg.min_withdrawal:
        return await m.answer(f"Minimum is {cfg.money(cfg.min_withdrawal)}.",
                              reply_markup=k.cancel_kb())
    if amount > free:
        return await m.answer(f"You only have {cfg.money(free)} available.",
                              reply_markup=k.cancel_kb())
    d = await state.get_data()
    await state.clear()
    # the amount is held, not deducted — a rejected request must return it intact
    wid = await db.create_withdrawal(uid, amount, d["wd_method"], d["wd_address"])
    await m.answer(f"💸 <b>Request #{wid} submitted</b>\n\n"
                   f"{cfg.money(amount)} via {esc(d['wd_method'])}\n"
                   f"<code>{esc(d['wd_address'])}</code>\n\n"
                   "It's on hold until an admin pays it out.",
                   reply_markup=k.back_to("menu:profile"))
    await delivery.notify_admins(
        m.bot, f"💸 <b>Withdrawal #{wid}</b>\n{cfg.money(amount)} via "
               f"{esc(d['wd_method'])}\nUser <code>{uid}</code>\n"
               f"<code>{esc(d['wd_address'])}</code>")


@router.callback_query(F.data == "pf:wdlist")
async def withdraw_list(c: CallbackQuery):
    rows = await db.user_withdrawals(c.from_user.id)
    if not rows:
        await show(c, "💸 No withdrawal requests yet.", k.back_to("pf:withdraw"))
        return await c.answer()
    icon = {"pending": "⏳", "paid": "✅", "rejected": "❌"}
    body = "\n".join(
        f"{icon.get(w['status'], '•')} #{w['id']} · {cfg.money(w['amount'])} · "
        f"{esc(w['method'])} · {w['status']}" for w in rows)
    await show(c, f"💸 <b>My requests</b>\n\n{body}", k.back_to("pf:withdraw"))
    await c.answer()


@router.callback_query(F.data == "menu:orders")
@router.callback_query(F.data.startswith("orders:"))
async def my_orders(c: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int(c.data.split(":")[1]) if c.data.startswith("orders:") else 0
    total = await db.count_user_orders(c.from_user.id, delivered_only=True)
    if not total:
        await show(c, await texts.t("orders_empty"), k.home_kb())
        return await c.answer()
    rows = await db.user_orders(c.from_user.id, k.ORDERS_PAGE, page * k.ORDERS_PAGE,
                                delivered_only=True)
    await show(c,
               await texts.t("orders_title") + "\n\n" + await texts.t("orders_intro"),
               k.orders_kb(rows, page, total))
    await c.answer()


@router.callback_query(F.data.startswith("ord:"))
async def open_order(c: CallbackQuery):
    o = await db.order(int(c.data.split(":")[1]))
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Order not found.", show_alert=True)
    await show(c, await _order_text(o), k.order_kb(o["id"], bool(o["delivered_text"])))
    await c.answer()


STATUS_LABEL = {"delivered": "Completed", "paid": "Paid",
                "pending": "Awaiting payment", "awaiting_review": "In review",
                "cancelled": "Cancelled", "expired": "Expired", "rejected": "Rejected"}


async def _order_text(o) -> str:
    p = await db.product(o["product_id"]) if o["product_id"] else None
    head = await texts.t(
        "order_detail",
        code=o["code"] or o["id"],
        product=esc(o["product_name"]),
        qty=o["qty"],
        total=cfg.money(o["amount"]),
        date=timefmt.local_dt(o["paid_at"] or o["created_at"]),
        status=STATUS_LABEL.get(o["status"], o["status"]),
    )
    if not o["delivered_text"]:
        return head

    items = [ln for ln in o["delivered_text"].split("\n") if ln.strip()]
    title = f"{name_of(p) if p else esc(o['product_name'])} × {o['qty']}"
    # numbered so a buyer can say "the third one didn't work"
    shown = items[:20]
    body = "\n".join(f"{i}. {esc(v)}" for i, v in enumerate(shown, 1))
    more = (f"\n\n<i>…and {len(items) - len(shown)} more — use the downloads below.</i>"
            if len(items) > len(shown) else "")
    return f"{head}\n\n{title}\n\n{body}{more}"


@router.message(Command("order"))
async def reopen(m: Message):
    """Look an order up by its short code."""
    parts = (m.text or "").split()
    if len(parts) < 2:
        return await m.answer("Usage: <code>/order A12B</code>",
                              reply_markup=k.back_to("menu:orders"))
    ref = parts[1].lstrip("#")
    o = await db.order_by_code(ref, m.from_user.id)
    if not o and ref.isdigit():                # numeric ids still work
        o = await db.order(int(ref))
        if o and o["user_id"] != m.from_user.id:
            o = None
    if o and o["status"] != "delivered":
        o = None                               # not history until it's received
    if not o:
        return await m.answer(
            "No delivered order with that ID. Check <b>My Orders</b> for the exact code.",
            reply_markup=k.back_to("menu:orders"))
    await m.answer(await _order_text(o),
                   reply_markup=k.order_kb(o["id"], bool(o["delivered_text"])))


@router.callback_query(F.data.startswith("dl:"))
async def download_order(c: CallbackQuery):
    _, fmt, oid = c.data.split(":")
    o = await db.order(int(oid))
    if not o or o["user_id"] != c.from_user.id or not o["delivered_text"]:
        return await c.answer("Nothing to download.", show_alert=True)

    items = [ln for ln in o["delivered_text"].split("\n") if ln.strip()]
    code = o["code"] or o["id"]
    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["order", "product", "quantity", "total", "date", "index", "item"])
        for i, v in enumerate(items, 1):
            wr.writerow([f"#{code}", o["product_name"], o["qty"], o["amount"],
                         o["paid_at"] or o["created_at"], i, v])
        data, name = buf.getvalue().encode(), f"order_{code}.csv"
    else:
        header = (f"Order #{code}\nProduct: {o['product_name']}\nQuantity: {o['qty']}\n"
                  f"Total: {cfg.money(o['amount'])}\n"
                  f"Date: {timefmt.local_dt(o['paid_at'] or o['created_at'])}\n"
                  + "-" * 40 + "\n")
        data = (header + "\n".join(items)).encode()
        name = f"order_{code}.txt"

    await c.message.answer_document(BufferedInputFile(data, filename=name),
                                    caption=f"Order #{code} · {len(items)} item(s)")
    await c.answer()


# Any stray text outside a form just re-opens the menu.
@router.message(F.text)
async def fallback(m: Message):
    await send_menu(m)
