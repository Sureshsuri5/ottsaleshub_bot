"""Settlement + delivery.

`settle()` is the single funnel every payment path goes through — Stars,
on-chain, webhook, admin approval, wallet balance. It is idempotent: calling it
twice for the same order delivers once.
"""
from __future__ import annotations

import logging
import re

from aiogram import Bot
from aiogram.types import BufferedInputFile, LinkPreviewOptions

import flair
import keyboards as k
import texts
import timefmt

import db
from config import cfg

log = logging.getLogger(__name__)


async def _tx_fields(o, short: bool = True) -> dict:
    """Transaction placeholders shared by the deposit and delivery messages.

    `tx_line` is the whole row, pre-formatted and blank when there's nothing to
    show — a template can't skip a line on its own.

    An order paid from wallet balance has no hash of its own, so the most
    recent verified deposit is shown instead: that is the payment the buyer
    actually made, and it's what they'd quote in a support message. Delete
    {tx_line} from the message in /texts if you'd rather not show it.
    """
    import payments
    txid = (o["external_ref"] or "").strip()
    code = o["provider"]
    if not txid and code == "balance":
        row = await db.q1(
            "SELECT provider, external_ref FROM orders WHERE user_id = ? "
            "AND kind = 'topup' AND status = 'delivered' "
            "AND external_ref IS NOT NULL AND external_ref != '' "
            "ORDER BY id DESC LIMIT 1", (o["user_id"],))
        if row:
            txid, code = (row["external_ref"] or "").strip(), row["provider"]

    link = payments.explorer_url(code, txid)
    shown = (f"{txid[:10]}…{txid[-8:]}" if short and len(txid) > 22 else txid)
    if link and short:
        line = f'\n🔗 Tx: <a href="{link}">{_esc(shown)}</a>'
    elif txid:
        # full hash, on its own line: it wraps rather than being cut off, and
        # tapping it copies the whole thing for a support message
        line = f"\n🔗 TxID:\n<code>{_esc(txid)}</code>"
    else:
        line = ""
    return dict(txid=_esc(txid), tx_link=link, tx_line=line,
                network=_esc(payments.network_label(o["provider"])))


async def settle(bot: Bot, oid: int, ref: str | None = None) -> bool:
    o = await db.order(oid)
    if not o:
        return False
    # `fulfilling` counts as handled: a manual order is mid-conversation, and a
    # replayed webhook must not restart it or ask the buyer for the number twice.
    if o["status"] in {"paid", "delivered", "fulfilling"}:
        return True                      # already handled — never double-deliver
    if o["status"] in {"expired", "cancelled"} and o["pay_address"]:
        # Money that arrived after the order closed. The goods aren't sent —
        # stock may be gone and the price may have moved — but the payment is
        # real and it's theirs, so it becomes wallet balance they can spend
        # immediately on a fresh order.
        return await _credit_late(bot, o, ref)
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
        o = await db.order(oid)          # re-read: external_ref was just set
        tx = await _tx_fields(o)
        # Link previews are off globally, but the explorer card under a deposit
        # is the buyer verifying their own money arrived — worth the exception.
        preview = LinkPreviewOptions(is_disabled=False, url=tx["tx_link"],
                                     show_above_text=False) if tx["tx_link"] else None
        await _safe(bot, o["user_id"],
                    await texts.t("topup_confirmed",
                                  amount=cfg.money(o["amount"]),
                                  balance=cfg.money(user["balance"]), **tx),
                    reply_markup=k.home_kb(), link_preview_options=preview)
        if await db.setting("admin_sale_alerts", "0") == "1":
            await notify_admins(bot, f"💰 Top-up #{oid} — {cfg.money(o['amount'])} "
                                     f"by <code>{o['user_id']}</code> via {o['provider']}")
        return True

    return await deliver(bot, oid)


async def deliver(bot: Bot, oid: int) -> bool:
    o = await db.order(oid)
    p = await db.product(o["product_id"]) if o["product_id"] else None

    # Credit any surplus first, so the confirmation below can state a balance
    # that is true at the moment the buyer reads it. Doing this after delivery
    # made the message say "Remaining Balance: $0.00" to someone who had just
    # been given change.
    extra = await _credit_overpayment(bot, o, notify=False)

    # Confirm the money before touching stock. Allocation is fast, but if it
    # fails the buyer has still seen that their payment landed.
    buyer = await db.get_user(o["user_id"])
    # what they actually sent, which is not the order total when they rounded up
    paid = max(float(o["received"] or 0), float(o["amount"] or 0))
    await _safe(bot, o["user_id"], await texts.t(
        "order_placed",
        amount=cfg.money(paid),
        cost=cfg.money(o["amount"]),
        # what this payment left over, not the wallet total — a buyer reading
        # "Remaining Balance" straight after "Paid" means change from what they
        # just sent, and carrying an older credit into that line reads as an
        # error in the arithmetic
        balance=cfg.money(max(0.0, round(paid - float(o["amount"] or 0), 2))),
        wallet=cfg.money(buyer["balance"] if buyer else 0),
        product=(f"{flair.product_icon(p)} {_esc(p['name'])}".strip()
                 if p else "—"),
        qty=o["qty"], oid=o["code"] or oid))

    if p is None:
        await _refund(bot, o, "the product is no longer available")
        return False

    # Manual products have nothing to hand over yet: the goods are activated by
    # a person, against a number the buyer has not given us. Diverting here —
    # after the payment confirmation, before allocation — means no stock line is
    # consumed and the order lands in a state settle() still counts as handled,
    # so a duplicate webhook can't push it through delivery a second time.
    if "manual" in p.keys() and p["manual"]:
        return await start_fulfilment(bot, oid, p)

    if p["infinite"]:
        payloads = [p["static_payload"]] * o["qty"]
    else:
        payloads = await db.allocate_stock(p["id"], o["qty"], oid)
        if payloads is None:
            await _refund(bot, o, "it went out of stock while your payment was confirming")
            return False

    body = "\n".join(payloads)
    # snapshot the cost price so profit history stays true when it later changes
    await db.set_order(oid, status="delivered", delivered_text=body,
                       unit_cost=float(p["cost"] or 0) if "cost" in p.keys() else 0)
    await db.ex("UPDATE products SET sold_count = sold_count + ? WHERE id = ?", (o["qty"], p["id"]))

    full = round(float(o["amount"] or 0) + float(o["balance_used"] or 0), 2)
    header = await texts.t("delivered_body", oid=o["code"] or oid,
                           product=_esc(p["name"]), qty=o["qty"],
                           emoji=flair.product_icon(p),
                           amount=cfg.money(full), method=_esc(o["provider"]),
                           date=timefmt.local_dt(o["paid_at"] or o["created_at"]),
                           **await _tx_fields(o, short=False))

    # Numbered, one per line, each its own copyable block. A buyer with four
    # keys needs to know which is which; a single <pre> blob makes them count.
    listing = "\n".join(f"{i}. <code>{_esc(line)}</code>"
                        for i, line in enumerate(payloads, 1))

    # No keyboard here. This message arrives on its own, not as a screen the
    # buyer navigated into, so a "Back" button has nowhere meaningful to go —
    # and it sits directly under the delivered items, where the one thing they
    # want to do is copy them. The order stays reachable from My Orders, which
    # keeps its own Back button.
    if len(payloads) >= cfg.file_delivery_from or len(body) > 3000:
        # A file rather than a wall of lines. It carries its own heading, so
        # the buyer can still tell which order and product it belongs to after
        # it has been saved somewhere and the chat is long gone.
        code = o["code"] or oid
        doc = (f"Order #{code}\n"
               f"{p['name']} × {o['qty']}\n"
               f"{timefmt.local_dt(o['paid_at'] or o['created_at'])}\n"
               f"{'-' * 40}\n"
               + "\n".join(f"{i}. {line}" for i, line in enumerate(payloads, 1))
               + "\n")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", p["name"]).strip("_") or "order"
        file = BufferedInputFile(
            doc.encode(), filename=f"{safe_name}_{code}.txt")
        await _safe_doc(bot, o["user_id"], file, header)
    else:
        await _safe(bot, o["user_id"], header + "\n" + listing)

    # Per-sale DMs are off by default: a working shop makes this noise all day,
    # and the sales feed plus the admin panel already carry the same facts.
    # Turn back on with Settings → Sale alerts.
    if await db.setting("admin_sale_alerts", "0") == "1":
        await notify_admins(
            bot, f"🛒 <b>Sale</b> #{oid}\n{p['name']} ×{o['qty']} — "
                 f"{cfg.money(o['amount'])}\nBuyer: <code>{o['user_id']}</code> · "
                 f"{o['provider']}", skip=o["user_id"])

    # public feed — anonymised, never carries the buyer or the delivered items
    await _notify_overpay(bot, o, extra)
    await flair.announce_sale(bot, o, p)
    await _pay_referrer(bot, o)

    if not p["infinite"]:
        left = await db.stock_count(p["id"])
        before = left + (o["qty"] or 0)
        # Only on the way past the line, not on every sale below it. Selling
        # five units one at a time used to send five identical warnings, which
        # is how an alert stops being read. Zero always warns — running out is
        # a different event from getting low.
        crossed = before > cfg.low_stock >= left
        if left == 0 and before > 0:
            await notify_admins(
                bot, f"🚫 <b>Out of stock</b>: {flair.product_tag(p)}\n"
                     f"<i>Buyers can still join the restock waitlist.</i>",
                skip=o["user_id"])
        elif crossed:
            await notify_admins(
                bot, f"⚠️ <b>Low stock</b>: {flair.product_tag(p)} — "
                     f"<b>{left}</b> left", skip=o["user_id"])
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
    # The money is credited either way — only the message is optional.
    inv = await db.get_user(inviter)
    if inv and not inv["notify_referral"]:
        return
    r = await db.referral_stats(inviter)
    await _safe(bot, inviter,
                f"⭐ <b>Referral reward</b>\n\n"
                f"+{cfg.money(reward)} — {', '.join(why)}.\n"
                f"Available to transfer: <b>{cfg.money(r['available'])}</b>",
                reply_markup=k.home_kb())


async def _credit_late(bot: Bot, o, ref: str | None) -> bool:
    """Turn a payment on a closed order into wallet balance."""
    amount = round(float(o["received"] or 0), 2) or float(o["amount"] or 0)
    if amount < 0.01:
        return False
    await db.add_balance(o["user_id"], amount)
    await db.set_order(o["id"], status="credited",
                       external_ref=ref or o["external_ref"])
    user = await db.get_user(o["user_id"])
    await _safe(bot, o["user_id"], await texts.t(
        "late_credited", amount=cfg.money(amount),
        balance=cfg.money(user["balance"] if user else amount),
        oid=o["code"] or o["id"]))
    await notify_admins(
        bot, f"💰 <b>Late payment</b> — order #{o['code'] or o['id']} had "
             f"expired. {cfg.money(amount)} credited to "
             f"<code>{o['user_id']}</code>'s wallet.")
    log.info("late payment on order %s credited: %s", o["id"], amount)
    return True


# --------------------------------------------------------------- fulfilment
# Manual orders: the buyer supplies a number, an operator activates the service
# against it, the buyer relays back the OTP the provider sends them. The whole
# exchange is a free-form chat relayed between Telegram and the web panel; the
# `stage` only records whose turn it is, so nobody has to read the transcript to
# see what the order is waiting on.

NUDGE_AFTER_MIN = 30
"""How long a buyer may sit on an unanswered prompt before being reminded.

Long enough that somebody who stepped away from their phone isn't pestered,
short enough that an order doesn't quietly die overnight. After one reminder
the admins are told instead — chasing twice is nagging, and by then it is
information the operator needs more than the customer does.
"""


async def start_fulfilment(bot: Bot, oid: int, p) -> bool:
    """Payment confirmed on a manual product: open the thread, ask for the number."""
    o = await db.order(oid)
    await db.open_fulfilment(oid, o["user_id"])
    # Snapshot the supplier now. Reading it from the product at display time
    # would silently move every past order the day the product is reassigned,
    # including ones another maker is mid-conversation on.
    mid = p["maker_id"] if "maker_id" in p.keys() else None
    if mid:
        await db.set_fulfil(oid, maker_id=int(mid))
    # cost is snapshotted now, same as an automatic delivery, so profit history
    # stays true if the cost price is edited while the order is still open
    await db.set_order(oid, status="fulfilling",
                       unit_cost=float(p["cost"] or 0) if "cost" in p.keys() else 0)
    code = o["code"] or oid
    await db.fulfil_say(oid, "system",
                        f"Paid — {p['name']} × {o['qty']}. Waiting for the number.")
    await _safe(bot, o["user_id"],
                await texts.t("fulfil_ask_number", oid=code,
                              product=_esc(p["name"]), qty=o["qty"]))
    await notify_admins(
        bot, f"🛠 Manual order <b>#{code}</b> — {_esc(p['name'])} × {o['qty']}\n"
             f"Waiting on the buyer's number. Work it in the admin panel.",
        skip=o["user_id"])
    return True


async def fulfil_from_user(bot: Bot, uid: int, text: str) -> bool:
    """A buyer message that belongs to an open manual order. False if none."""
    f = await db.active_fulfilment(uid)
    if not f:
        return False
    oid = f["order_id"]
    o = await db.order(oid)
    code = (o["code"] if o else None) or oid
    await db.fulfil_say(oid, "user", text)

    if f["stage"] == "awaiting_number":
        # First reply is the activation number. Captured onto the row rather
        # than left in the transcript so the panel can show it beside the order
        # without an operator scrolling the chat for it every time.
        await db.set_fulfil(oid, number=text.strip()[:64], stage="working", nudged=0)
        await _safe(bot, uid, await texts.t("fulfil_got_number", oid=code))
        await notify_admins(bot, f"🔢 <b>#{code}</b> number received — activate it.",
                            skip=uid)
    else:
        if f["stage"] == "awaiting_otp":
            await db.set_fulfil(oid, stage="working", nudged=0)
            await _safe(bot, uid, await texts.t("fulfil_got_otp", oid=code))
        await notify_admins(bot, f"💬 <b>#{code}</b> buyer replied.", skip=uid)
    return True


async def fulfil_to_user(bot: Bot, oid: int, text: str) -> bool:
    """An operator's message, sent from the panel out to the buyer."""
    f = await db.fulfilment(oid)
    if not f:
        return False
    o = await db.order(oid)
    await db.fulfil_say(oid, "admin", text)
    await _safe(bot, f["user_id"],
                await texts.t("fulfil_admin_msg", oid=(o["code"] if o else None) or oid,
                              body=_esc(text)))
    return True


async def fulfil_request_otp(bot: Bot, oid: int) -> bool:
    """Prompt for a one-time code. Repeatable: providers often send a second."""
    f = await db.fulfilment(oid)
    if not f:
        return False
    o = await db.order(oid)
    code = (o["code"] if o else None) or oid
    await db.set_fulfil(oid, stage="awaiting_otp", nudged=0)
    await db.fulfil_say(oid, "system", "Asked the buyer for the OTP.")
    await _safe(bot, f["user_id"], await texts.t("fulfil_ask_otp", oid=code))
    return True


async def fulfil_complete(bot: Bot, oid: int) -> bool:
    """Activated and confirmed. This is the only path that marks it delivered."""
    o = await db.order(oid)
    f = await db.fulfilment(oid)
    if not o or not f or o["status"] == "delivered":
        return False
    note = (f["note"] or "").strip()
    await db.set_order(oid, status="delivered",
                       delivered_text=note or "Activated by the operator.")
    if o["product_id"]:
        await db.ex("UPDATE products SET sold_count = sold_count + ? WHERE id = ?",
                    (o["qty"], o["product_id"]))
    await db.set_fulfil(oid, stage="done")
    await db.fulfil_say(oid, "system", "Marked complete.")
    # Codes are useless now and a liability stored. The number and the wording
    # of the conversation stay; only the one-time codes go.
    try:
        await db.fulfil_scrub(oid)
    except Exception as e:                              # pragma: no cover
        log.warning("could not scrub codes on #%s: %s", oid, e)
    await _safe(bot, o["user_id"],
                await texts.t("fulfil_done", oid=o["code"] or oid,
                              product=_esc(o["product_name"] or ""),
                              note=("\n\n" + _esc(note)) if note else ""),
                reply_markup=k.home_kb())
    return True


async def fulfil_cancel(bot: Bot, oid: int, reason: str = "") -> bool:
    """Couldn't be activated. Refunds to wallet through the ordinary path."""
    o = await db.order(oid)
    f = await db.fulfilment(oid)
    if not o or not f or o["status"] == "delivered":
        return False
    await db.set_fulfil(oid, stage="cancelled")
    await db.fulfil_say(oid, "system", f"Cancelled and refunded. {reason}".strip())
    await _refund(bot, o, reason or "it could not be activated")
    return True


async def nudge_fulfilments(bot: Bot) -> None:
    """Chase buyers who have gone quiet, once, then hand it to the admins.

    Only stages where we are waiting on them. The reminder is deliberately the
    same prompt again rather than a scolding: most people simply missed the
    message, and the useful thing to resend is the question.
    """
    try:
        stale = await db.fulfil_stale(NUDGE_AFTER_MIN)
    except Exception as e:                              # pragma: no cover
        log.warning("could not scan for stale fulfilments: %s", e)
        return
    for f in stale:
        oid = f["order_id"]
        o = await db.order(oid)
        if not o or o["status"] != "fulfilling":
            continue
        code = o["code"] or oid
        kind = "fulfil_nudge_number" if f["stage"] == "awaiting_number" \
            else "fulfil_nudge_otp"
        try:
            await _safe(bot, f["user_id"], await texts.t(kind, oid=code))
            await db.ex("UPDATE fulfilment SET nudged = nudged + 1 WHERE order_id = ?",
                        (oid,))
            await db.fulfil_say(oid, "system", "Reminder sent to the buyer.")
            await notify_admins(
                bot, f"⏳ <b>#{code}</b> — no reply for {NUDGE_AFTER_MIN} min. "
                     f"Buyer reminded; it may need a manual follow-up.",
                skip=f["user_id"])
        except Exception as e:
            log.warning("nudge failed for #%s: %s", oid, e)


async def _refund(bot: Bot, o, reason: str) -> None:
    # Both halves come back: what the buyer paid on the rail, and the wallet
    # balance the order was holding. Refunding only the rail share would
    # quietly keep the part they'd already had deducted.
    back = round(float(o["amount"] or 0) + await db.release_balance(o["id"]), 2)
    await db.add_balance(o["user_id"], o["amount"])
    await db.set_order(o["id"], status="cancelled")
    await _safe(bot, o["user_id"],
                await texts.t("refund_notice", oid=o["id"], reason=reason,
                              amount=cfg.money(back)),
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


async def _credit_overpayment(bot: Bot, o, notify: bool = True) -> float:
    """Anything sent above the order total becomes wallet balance.

    A buyer who rounds up, or pays from an exchange that sends a little extra,
    should not simply lose the difference. Rounded to the shop's own precision
    so a fraction of a cent from rate conversion doesn't produce a message
    about nothing.
    """
    try:
        received = float(o["received"] or 0)
    except (KeyError, IndexError, TypeError):
        return 0.0                  # column not present on an older row
    extra = round(received - float(o["amount"] or 0), 2)
    if extra < 0.01:
        return 0.0
    await db.add_balance(o["user_id"], extra)
    log.info("order %s overpaid by %s — credited to wallet", o["id"], extra)
    if notify:
        await _notify_overpay(bot, o, extra)
    return extra


_MIN_STOCK_ADD = 1
"""Smallest top-up worth announcing.

A single unit trickling back in is noise, and the group mutes a shop that
posts noise. Raise this if you add stock in small batches — at 25, only a
real restock reaches the group.
"""


async def announce_restocks(bot: Bot) -> None:
    """Post to the group when a product is new, or comes back from sold out.

    Availability is compared against what was last seen, not against a hook in
    whichever code path added the stock — admin panel, web panel and bulk
    upload all add stock differently, and a hook in one of them would silently
    miss the others.

    The very first pass records the whole catalogue and announces nothing,
    otherwise every existing product would be announced at once the moment this
    ships. A `bootstrapped` flag marks that pass, which is what lets a product
    first seen *later* be recognised as genuinely new rather than pre-existing.
    """
    chat = (await flair.restock_chat()).strip()
    first_run = await db.setting("restock:bootstrapped", "") != "1"

    for p in await db.products(None, only_active=True):
        pid = p["id"]
        # Manual products hold no stock, and available() reports them as always
        # buyable so they can be sold at all. Left in, that reads as a jump from
        # 0 to a million and the group gets "1000000 new stock added".
        if "manual" in p.keys() and p["manual"]:
            continue
        avail = await db.available(pid)
        in_stock = avail > 0 or p["infinite"]
        now = "1" if in_stock else "0"
        was = await db.setting(f"restock:instock:{pid}", "")
        announced_new = await db.setting(f"restock:new:{pid}", "") == "1"

        if was != now:
            await db.set_setting(f"restock:instock:{pid}", now)

        # price is tracked the same way as stock: compare against what was last
        # seen, so a change made through any panel is caught
        price = round(float(p["price"] or 0), 2)
        try:
            old_price = float(await db.setting(f"restock:price:{pid}", "") or 0)
        except ValueError:
            old_price = 0.0
        if price != old_price:
            await db.set_setting(f"restock:price:{pid}", f"{price:.2f}")

        # Numeric availability, tracked the same way as price. The boolean
        # above only catches 0 -> N; a top-up on a product that never sold out
        # is invisible to it, which is exactly the case we want to announce.
        try:
            last_avail = int(await db.setting(f"restock:avail:{pid}", "") or 0)
        except ValueError:
            last_avail = 0
        added = max(avail - last_avail, 0)
        if avail != last_avail:
            await db.set_setting(f"restock:avail:{pid}", str(avail))

        if first_run:
            # everything that already exists counts as known, new or not
            await db.set_setting(f"restock:new:{pid}", "1")
            continue
        if not chat or not in_stock:
            continue

        if not announced_new:
            # a product the bot has never seen in stock before
            await db.set_setting(f"restock:new:{pid}", "1")
            kind = "newproduct_group"
        elif was == "0":
            kind = "restock_group"
        elif not p["infinite"] and added >= _MIN_STOCK_ADD:
            kind = "stockadded_group"
        elif old_price and price < old_price - 0.009:
            # cheaper than last time. A rise is recorded silently — nobody
            # wants an announcement that their shop got more expensive.
            kind = "pricedrop_group"
        else:
            continue

        try:
            # The description the product page shows, as a quote block so a
            # long one stays collapsible instead of burying the Buy button.
            # Trimmed: a group post is a teaser, the full text is one tap away.
            desc = (p["description"] or "").strip()
            if len(desc) > 400:
                desc = desc[:400].rsplit(" ", 1)[0] + "…"
            off = int(round((old_price - price) / old_price * 100)) if old_price else 0
            body = await texts.t(
                kind,
                product=flair.product_tag(p),
                price=cfg.money(p["price"]),
                was=cfg.money(old_price),
                percent=f"{off}%",
                stock="∞" if p["infinite"] else avail,
                added=added,
                desc=f"\n<blockquote expandable>{_esc(desc)}</blockquote>\n"
                     if desc else "")
            sent = await bot.send_message(
                chat, await flair.render(body),
                reply_markup=k.kb([k.url_btn(
                    flair.label("buy", f"Buy {p['name']}"),
                    f"https://t.me/{flair.BOT_USERNAME}?start=p_{pid}",
                    style="primary", icon_slot="buy")]))
            log.info("%s announced for product %s", kind, pid)

            # separate slots per kind, so a price drop replaces the last price
            # drop rather than knocking the newest product off the pin bar
            await _pin_announcement(bot, chat, sent.message_id, kind)
        except Exception as e:
            log.warning("announcement failed for %s: %s", pid, e)

    if first_run:
        await db.set_setting("restock:bootstrapped", "1")


async def _pin_announcement(bot: Bot, chat: str, msg_id: int,
                            slot: str = "newproduct_group") -> None:
    """Pin an announcement and unpin the one it replaces.

    One slot per kind: the newest product and the latest price drop can both
    be pinned, but a second price drop replaces the first. Telegram allows many
    pins at once, so without this the bar fills up and the things worth seeing
    end up the least visible in it.

    Pinned silently — the announcement itself already notified the group, and a
    second ping for the same event is what makes people mute a shop.

    Failure is logged, not raised: the announcement has already landed, and a
    missing pin permission shouldn't look like a broken announcement.
    """
    key = f"restock:pinned:{slot}"
    prev = await db.setting(key, "")
    try:
        await bot.pin_chat_message(chat, msg_id, disable_notification=True)
        await db.set_setting(key, str(msg_id))
    except Exception as e:
        log.warning("could not pin in %s — the bot needs pin rights there: %s",
                    chat, e)
        return
    if prev.isdigit() and prev != str(msg_id):
        try:
            await bot.unpin_chat_message(chat, int(prev))
        except Exception:
            pass          # already gone, or somebody unpinned it by hand


async def notify_restock(bot: Bot) -> None:
    """Tell everyone waiting that a sold-out product is available again.

    Driven from the waitlist rather than hooked into each place stock is added
    — admin panel, web panel, bulk upload — so no route can add stock without
    the alert going out.
    """
    for pid in await db.watched_products():
        p = await db.product(pid)
        if not p or not p["is_active"]:
            continue
        avail = await db.available(pid)
        if avail <= 0 and not p["infinite"]:
            continue
        for uid in await db.take_watchers(pid):
            u = await db.get_user(uid)
            if u and not u["notify_stock"]:
                continue
            await _safe(bot, uid, await texts.t(
                "restock_alert", product=_esc(p["name"]),
                emoji=p["emoji"] or "", stock=avail,
                price=cfg.money(p["price"])),
                reply_markup=k.kb([k.btn("🛒 Buy now", f"p:{pid}", style="primary")]))
        log.info("restock alert sent for product %s", pid)


async def notify_underpaid(bot: Bot) -> None:
    """Tell buyers whose payment fell short, once per new amount received.

    Silence here is expensive: the buyer thinks they've paid, the shop thinks
    nothing happened, and the money is sitting on an address neither of them is
    looking at. `short_notified` records the figure they were last told, so a
    second partial payment produces one new message rather than a repeat every
    poll.
    """
    rows = await db.q(
        "SELECT id, code, user_id, amount, received, pay_address FROM orders "
        "WHERE status = 'pending' AND COALESCE(received, 0) > 0 "
        "AND COALESCE(amount, 0) > 0 AND COALESCE(received, 0) < amount "
        "AND COALESCE(short_notified, 0) < COALESCE(received, 0)")
    for o in rows:
        short = round(float(o["amount"]) - float(o["received"]), 2)
        await db.set_order(o["id"], short_notified=float(o["received"]))
        await _safe(bot, o["user_id"], await texts.t(
            "underpaid_notice",
            sent=cfg.money(o["received"]), total=cfg.money(o["amount"]),
            short=cfg.money(short), address=o["pay_address"] or "",
            oid=o["code"] or o["id"]))
        await notify_admins(
            bot, f"⚠️ <b>Part payment</b> — order #{o['code'] or o['id']}\n"
                 f"{cfg.money(o['received'])} of {cfg.money(o['amount'])}, "
                 f"{cfg.money(short)} outstanding")
        log.info("order %s part-paid, buyer told they owe %s", o["id"], short)


async def _notify_overpay(bot: Bot, o, extra: float) -> None:
    """Sent after the goods, so the buyer reads confirmation then change —
    crediting has to happen earlier than this to keep the balance line honest,
    but the message belongs at the end."""
    if extra < 0.01:
        return
    user = await db.get_user(o["user_id"])
    await _safe(bot, o["user_id"], await texts.t(
        "overpay_credited",
        sent=cfg.money(float(o["received"] or 0)), extra=cfg.money(extra),
        balance=cfg.money(user["balance"] if user else extra),
        oid=o["code"] or o["id"]))


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
        await bot.send_document(chat_id, file, caption=caption)
    except Exception as e:
        log.warning("doc to %s failed: %s", chat_id, e)
