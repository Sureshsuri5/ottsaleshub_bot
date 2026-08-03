"""Settlement + delivery.

`settle()` is the single funnel every payment path goes through — Stars,
on-chain, webhook, admin approval, wallet balance. It is idempotent: calling it
twice for the same order delivers once.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile

import flair
import keyboards as k
import texts

import db
from config import cfg

log = logging.getLogger(__name__)


async def settle(bot: Bot, oid: int, ref: str | None = None) -> bool:
    o = await db.order(oid)
    if not o:
        return False
    if o["status"] in {"paid", "delivered"}:
        return True                      # already handled — never double-deliver
    if o["status"] in {"cancelled", "rejected"}:
        return False

    await db.set_order(oid, status="paid", paid_at=db.now(),
                       external_ref=ref or o["external_ref"])

    if o["kind"] == "topup":
        await db.add_balance(o["user_id"], o["amount"])
        if cfg.ref_on_deposit:
            await _pay_referrer(bot, o, deposit=True)
        await db.set_order(oid, status="delivered")
        user = await db.get_user(o["user_id"])
        await _safe(bot, o["user_id"],
                    await texts.t("topup_confirmed",
                                  amount=cfg.money(o["amount"]),
                                  balance=cfg.money(user["balance"])),
                    reply_markup=k.home_kb())
        if await db.setting("admin_sale_alerts", "0") == "1":
            await notify_admins(bot, f"💰 Top-up #{oid} — {cfg.money(o['amount'])} "
                                     f"by <code>{o['user_id']}</code> via {o['provider']}")
        return True

    return await deliver(bot, oid)


async def deliver(bot: Bot, oid: int) -> bool:
    o = await db.order(oid)
    p = await db.product(o["product_id"]) if o["product_id"] else None

    if p is None:
        await _refund(bot, o, "the product is no longer available")
        return False

    if p["infinite"]:
        payloads = [p["static_payload"]] * o["qty"]
    else:
        payloads = await db.allocate_stock(p["id"], o["qty"], oid)
        if payloads is None:
            await _refund(bot, o, "it went out of stock while your payment was confirming")
            return False

    body = "\n".join(payloads)
    await db.set_order(oid, status="delivered", delivered_text=body)
    await db.ex("UPDATE products SET sold_count = sold_count + ? WHERE id = ?", (o["qty"], p["id"]))

    header = await texts.t("delivered_body", oid=o["code"] or oid,
                           product=_esc(p["name"]), qty=o["qty"],
                           amount=cfg.money(o["amount"]), method=_esc(o["provider"]))

    if len(payloads) > 20 or len(body) > 3000:
        file = BufferedInputFile(body.encode(), filename=f"order_{oid}.txt")
        await _safe_doc(bot, o["user_id"], file, header)
    else:
        await _safe(bot, o["user_id"], header + f"\n<pre>{_esc(body)}</pre>",
                    reply_markup=k.order_kb())

    # Per-sale DMs are off by default: a working shop makes this noise all day,
    # and the sales feed plus the admin panel already carry the same facts.
    # Turn back on with Settings → Sale alerts.
    if await db.setting("admin_sale_alerts", "0") == "1":
        await notify_admins(
            bot, f"🛒 <b>Sale</b> #{oid}\n{p['name']} ×{o['qty']} — "
                 f"{cfg.money(o['amount'])}\nBuyer: <code>{o['user_id']}</code> · "
                 f"{o['provider']}", skip=o["user_id"])

    # public feed — anonymised, never carries the buyer or the delivered items
    await flair.announce_sale(bot, o, p)
    await _pay_referrer(bot, o)

    if not p["infinite"]:
        left = await db.stock_count(p["id"])
        if left <= cfg.low_stock:
            await notify_admins(bot, f"⚠️ Low stock: <b>{p['name']}</b> — {left} left",
                                skip=o["user_id"])
    return True


async def _pay_referrer(bot: Bot, o, deposit: bool = False) -> None:
    """Credit the inviter after a completed purchase.

    The joining bonus lands on the invitee's FIRST delivered purchase, not on
    signup — a fake account costs the referrer real money to farm, so there is
    nothing to gain from mass-registering.
    """
    buyer = await db.get_user(o["user_id"])
    if not buyer or not buyer["referred_by"]:
        return
    inviter = buyer["referred_by"]

    reward, why = 0.0, []
    if not deposit and cfg.ref_bonus and await db.delivered_purchases(o["user_id"]) == 1:
        reward += cfg.ref_bonus
        why.append("first purchase bonus")
    if cfg.ref_percent:
        cut = round(o["amount"] * cfg.ref_percent / 100, 2)
        if cut > 0:
            reward += cut
            why.append(f"{cfg.ref_percent:g}% of {cfg.money(o['amount'])}")
    if reward <= 0:
        return

    await db.credit_referral(inviter, round(reward, 2))
    r = await db.referral_stats(inviter)
    await _safe(bot, inviter,
                f"⭐ <b>Referral reward</b>\n\n"
                f"+{cfg.money(reward)} — {', '.join(why)}.\n"
                f"Available to transfer: <b>{cfg.money(r['available'])}</b>",
                reply_markup=k.home_kb())


async def _refund(bot: Bot, o, reason: str) -> None:
    await db.add_balance(o["user_id"], o["amount"])
    await db.set_order(o["id"], status="cancelled")
    await _safe(bot, o["user_id"],
                await texts.t("refund_notice", oid=o["id"], reason=reason,
                              amount=cfg.money(o["amount"])),
                reply_markup=k.home_kb())
    await notify_admins(bot, f"🚨 Auto-refund on order #{o['id']} — {reason}")


async def notify_admins(bot: Bot, text: str, skip: int | None = None) -> None:
    """`skip` is the buyer: when an admin buys from their own shop they'd
    otherwise receive the operational alerts alongside their delivery."""
    for aid in cfg.admin_ids:
        if skip is not None and aid == skip:
            continue
        await _safe(bot, aid, text)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _safe(bot: Bot, chat_id: int, text: str, **kw) -> None:
    try:
        u = await db.get_user(chat_id)
        if u and not u["notify_orders"] and chat_id not in cfg.admin_ids:
            return                      # user muted order updates
    except Exception:
        pass
    try:
        await bot.send_message(chat_id, text, **kw)
    except Exception as e:
        log.warning("send to %s failed: %s", chat_id, e)


async def _safe_doc(bot: Bot, chat_id: int, file, caption: str) -> None:
    try:
        await bot.send_document(chat_id, file, caption=caption,
                                reply_markup=k.order_kb())
    except Exception as e:
        log.warning("doc to %s failed: %s", chat_id, e)
