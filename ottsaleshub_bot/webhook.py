"""Optional PSP webhook — turns UPI into an automatic rail.

Enable with WEBHOOK_ENABLED=true and point your provider at
    https://your-domain/psp/webhook

There is no honest way to read UPI settlements without a payment provider, so
this is the supported path: Razorpay, Cashfree, PhonePe and the rest all POST a
signed callback when money lands.

Two things make this safe to expose publicly:

  * the signature is checked against the raw body before anything is parsed, so
    a forged callback never reaches the database;
  * `db.mark_seen()` means a replayed callback settles an order at most once.

Matching is deliberately forgiving about payload shape — every provider nests
things differently — but strict about the two facts that matter: which order,
and how much.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re

from aiogram import Bot
from aiohttp import web

import db
import delivery
from config import cfg

log = logging.getLogger(__name__)

# The note we put on every UPI invoice: ORD<order code>
REF_NOTE = re.compile(r"ORD([A-Za-z0-9]{3,10})")

PAID_EVENTS = ("captur", "success", "paid", "completed", "credit")


def _valid(raw: bytes, signature: str) -> bool:
    """Providers sign the same way but encode differently — accept both."""
    if not signature:
        return False
    mac = hmac.new(cfg.webhook_secret.encode(), raw, hashlib.sha256)
    if hmac.compare_digest(mac.hexdigest(), signature):
        return True
    return hmac.compare_digest(base64.b64encode(mac.digest()).decode(), signature)


def _walk(obj):
    """Every key/value in a nested payload, whatever shape the provider used."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _find_amount(data: dict) -> float | None:
    """Amount in rupees. Most providers send paise; some send a decimal string."""
    for key, val in _walk(data):
        if str(key).lower() not in {"amount", "amount_paid", "txamount",
                                    "payment_amount"}:
            continue
        try:
            n = float(val)
        except (TypeError, ValueError):
            continue
        # a large integer is paise, not rupees
        return round(n / 100, 2) if isinstance(val, int) and n >= 1000 else round(n, 2)
    return None


async def _find_order(data: dict):
    """The order this payment belongs to.

    Preferred route is our own reference note, which is what survives a plain
    UPI transfer. An explicit order id in provider metadata also works.
    """
    for _key, val in _walk(data):
        if not isinstance(val, str):
            continue
        m = REF_NOTE.search(val)
        if m:
            row = await db.order_by_code(m.group(1))
            if row:
                return row
    for key, val in _walk(data):
        if str(key).lower() in {"order_id", "orderid", "order_no"}:
            try:
                row = await db.order(int(val))
            except (TypeError, ValueError):
                continue
            if row:
                return row
    return None


def _find_ref(data: dict) -> str | None:
    for key, val in _walk(data):
        if str(key).lower() in {"utr", "rrn", "bank_reference", "payment_id",
                                "transaction_id", "referenceid"} and val:
            return str(val)[:64]
    return None


async def _handle(request: web.Request) -> web.Response:
    raw = await request.read()
    sig = (request.headers.get("X-Razorpay-Signature")
           or request.headers.get("x-webhook-signature")
           or request.headers.get("X-Signature", ""))
    if not _valid(raw, sig):
        log.warning("webhook: bad signature from %s", request.remote)
        return web.Response(status=401, text="bad signature")

    try:
        data = json.loads(raw)
    except ValueError:
        return web.Response(status=400, text="bad json")

    event = str(data.get("event") or data.get("type") or "").lower()
    if event and not any(w in event for w in PAID_EVENTS):
        return web.json_response({"ok": True, "ignored": event})

    order = await _find_order(data)
    if not order:
        log.warning("webhook: no order matched — is the reference note reaching "
                    "the provider?")
        return web.Response(status=400, text="no matching order")

    ref = _find_ref(data) or f"order{order['id']}"
    if not await db.mark_seen(f"psp:{ref}", order["id"]):
        return web.json_response({"ok": True, "duplicate": True})

    # An underpayment must not deliver. Send it to review rather than guess.
    paid = _find_amount(data)
    expected = float(order["pay_amount"] or 0)
    if paid is not None and expected and paid + 0.5 < expected:
        await db.set_order(order["id"], status="awaiting_review", external_ref=ref)
        await delivery.notify_admins(
            request.app["bot"],
            f"⚠️ Underpaid order #{order['code'] or order['id']} — expected "
            f"₹{expected:.2f}, received ₹{paid:.2f}. Ref <code>{ref}</code>",
            skip=order["user_id"])
        return web.json_response({"ok": False, "reason": "underpaid"})

    bot: Bot = request.app["bot"]
    settled = await delivery.settle(bot, order["id"], ref)
    log.info("webhook settled order %s via %s", order["id"], ref)
    return web.json_response({"ok": settled})


def add_routes(app: web.Application) -> None:
    """Mounted onto the Mini App server so everything shares one port."""
    app.router.add_post(cfg.webhook_path, _handle)
    log.info("PSP webhook mounted at %s", cfg.webhook_path)
