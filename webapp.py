"""HTTP server for the two Telegram Mini Apps.

  /            storefront  — catalogue, checkout, live payment status, orders
  /admin       admin panel — stats, catalogue, stock, orders, users, broadcast
  /api/...     JSON API used by both
  /psp/webhook optional PSP callback (mounted from webhook.py)

Authentication is Telegram's own `initData`: the Mini App sends it on every
request and the server re-computes the HMAC with the bot token. The user id in
a validated payload cannot be forged, so it is trusted as the identity for
checkout, order access and admin rights. Nothing else is trusted from the client
— prices, stock and balances are always read server-side.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from aiogram import Bot
from aiohttp import web

import db
import delivery
import flair
import pricing
import texts
import payments
from config import cfg

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


# ------------------------------------------------------------------- auth
def verify_init_data(init_data: str, max_age: int = 86400) -> dict | None:
    """Validate Telegram WebApp initData. Returns the user dict or None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", cfg.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if abs(time.time() - int(pairs.get("auth_date", 0))) > max_age:
        return None
    try:
        return json.loads(pairs.get("user", "{}")) or None
    except ValueError:
        return None


# ---- browser sessions (Telegram Login Widget) --------------------------
SESSION_TTL = 30 * 24 * 3600


def _sign(payload: str) -> str:
    return hmac.new(cfg.bot_token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_session(uid: int) -> str:
    exp = int(time.time()) + SESSION_TTL
    body = f"{uid}.{exp}"
    return f"{body}.{_sign(body)}"


def read_session(token: str) -> int | None:
    try:
        uid, exp, sig = token.split(".")
    except (AttributeError, ValueError):
        return None
    if not hmac.compare_digest(_sign(f"{uid}.{exp}"), sig):
        return None
    if int(exp) < time.time():
        return None
    return int(uid)


def verify_login_widget(data: dict) -> dict | None:
    """Telegram Login Widget payload.

    Same idea as initData but a different secret: SHA256 of the bot token rather
    than an HMAC keyed with 'WebAppData'.
    """
    data = {k: v for k, v in data.items() if v is not None}
    received = str(data.pop("hash", ""))
    if not received:
        return None
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(cfg.bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if abs(time.time() - int(data.get("auth_date", 0))) > 86400:
        return None
    return data


# Screens a visitor can see before signing in — the shopfront, nothing personal.
PUBLIC_PATHS = {"/api/me", "/api/catalog", "/api/auth/telegram"}


@web.middleware
async def auth_middleware(request: web.Request, handler: Callable):
    if not request.path.startswith("/api/") or request.path.startswith("/api/v1/"):
        return await handler(request)

    user = verify_init_data(request.headers.get("X-Init-Data", "")
                            or request.query.get("_auth", ""))

    if user is None:                                  # browser session cookie
        uid = read_session(request.headers.get("X-Session", "")
                           or request.query.get("_session", ""))
        if uid:
            row = await db.get_user(uid)
            if row:
                user = {"id": uid, "username": row["username"],
                        "first_name": row["first_name"]}

    if user is None and cfg.panel_token:
        token = request.headers.get("X-Admin-Token", "")
        if hmac.compare_digest(token, cfg.panel_token) and cfg.admin_ids:
            user = {"id": cfg.admin_ids[0], "first_name": "Browser", "username": "dev"}

    if user is None:
        if request.path in PUBLIC_PATHS:
            request["uid"] = None
            request["admin"] = False
            return await handler(request)
        return web.json_response({"error": "unauthorised"}, status=401)

    row = await db.upsert_user(user["id"], user.get("username"), user.get("first_name"))
    if row["is_banned"] and not cfg.is_admin(user["id"]):
        return web.json_response({"error": "banned"}, status=403)

    request["uid"] = user["id"]
    request["admin"] = cfg.is_admin(user["id"])
    if request.path.startswith("/api/admin/") and not request["admin"]:
        return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)


def rows(seq) -> list[dict]:
    return [dict(r) for r in seq]


async def body(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# -------------------------------------------------------------- shop API
HERO_DEFAULTS = {
    "hero_badge": "Trusted digital marketplace",
    "hero_title": "Premium digital products,",
    "hero_accent": "delivered instantly.",
    "hero_sub": ("Top up your balance once and unlock fast, automated delivery on every "
                 "purchase. Secure payments, live stock, and 24/7 support — "
                 "all in one place."),
}


async def api_login(request):
    """Exchange a Telegram Login Widget payload for a session token."""
    data = verify_login_widget(await body(request))
    if not data:
        return web.json_response({"error": "invalid login"}, status=401)
    uid = int(data["id"])
    row = await db.upsert_user(uid, data.get("username"), data.get("first_name"))
    await db.activate(uid)
    if row["is_banned"]:
        return web.json_response({"error": "account suspended"}, status=403)
    return web.json_response({"session": issue_session(uid), "id": uid,
                              "name": data.get("first_name", "")})


async def api_me(request):
    if request["uid"] is None:                       # anonymous shopfront
        s = await db.stats()
        hero = {k: await db.setting(k, v) for k, v in HERO_DEFAULTS.items()}
        currencies = [{"code": cfg.fiat, "symbol": cfg.symbol, "rate": 1}]
        if cfg.second_code:
            currencies.append({"code": cfg.second_code,
                               "symbol": cfg.second_symbol or cfg.second_code,
                               "rate": cfg.second_rate})
        return web.json_response({
            "signed_in": False, "shop": cfg.shop_name, "currency": cfg.fiat,
            "symbol": cfg.symbol, "decimals": cfg.decimals, "support": cfg.support_url,
            "bot": flair.BOT_USERNAME, "hero": hero, "currencies": currencies,
            "logo": await db.setting("shop_logo", ""),
            "providers": [], "tier": "",
            "stats": {"products": s["products"], "delivered": s["orders"],
                      "stock": s["in_stock"], "users": s["users"]},
        })
    u = await db.get_user(request["uid"])
    s = await db.stats()
    hero = {k: await db.setting(k, v) for k, v in HERO_DEFAULTS.items()}
    currencies = [{"code": cfg.fiat, "symbol": cfg.symbol, "rate": 1}]
    if cfg.second_code:
        currencies.append({"code": cfg.second_code,
                           "symbol": cfg.second_symbol or cfg.second_code,
                           "rate": cfg.second_rate})
    s = await db.stats()
    hero = {k: await db.setting(k, v) for k, v in HERO_DEFAULTS.items()}
    return web.json_response({
        # inside Telegram the identity is already proven; "signed in" here means
        # the buyer has opened an account, which is an explicit act
        "signed_in": bool(u["activated"]), "identified": True,
        "bot": flair.BOT_USERNAME,
        "logo": await db.setting("shop_logo", ""),
        "hero": hero,
        "stats": {"products": s["products"], "delivered": s["orders"],
                  "stock": s["in_stock"], "users": s["users"]},
        "id": u["tg_id"], "name": u["first_name"], "balance": u["balance"],
        "admin": request["admin"], "shop": cfg.shop_name, "currency": cfg.fiat,
        "symbol": cfg.symbol, "decimals": cfg.decimals, "support": cfg.support_url,
        "notify_orders": bool(u["notify_orders"]),
        "terms": await terms_state(request["uid"]),
        "tier": await pricing.label(request["uid"]),
        "providers": [{"code": p.code, "title": p.title} for p in payments.enabled()],
        "hero": hero,
        "currencies": currencies,
        "stats": {"products": s["products"], "delivered": s["orders"],
                  "stock": s["in_stock"], "users": s["users"]},
    })


async def terms_state(uid: int | None) -> dict:
    """Terms text and whether this buyer still has to accept it.

    The version is a hash of the text itself rather than a number an admin has
    to remember to bump — edit the terms in /texts and everyone is asked again,
    which is the only behaviour that makes the acceptance mean anything.
    """
    body = await texts.t("terms_body")
    version = hashlib.sha1(body.encode()).hexdigest()[:12]
    accepted = ""
    if uid:
        u = await db.get_user(uid)
        accepted = (u["terms_version"] if u else "") or ""
    return {"text": body, "version": version, "accepted": accepted == version}


async def api_terms_accept(request):
    """Record acceptance of the exact text the buyer was shown."""
    if request["uid"] is None:
        return web.json_response({"error": "not signed in"}, status=401)
    d = await request.json()
    state = await terms_state(request["uid"])
    # Compare against the current text: a stale tab could otherwise accept a
    # version that has since been replaced.
    if d.get("version") != state["version"]:
        return web.json_response({"error": "terms changed", **state}, status=409)
    await db.set_terms_version(request["uid"], state["version"])
    return web.json_response({"ok": True})


async def api_activate(request):
    """Open an account for a buyer Telegram has already identified."""
    if request["uid"] is None:
        return web.json_response({"error": "unauthorised"}, status=401)
    await db.activate(request["uid"])
    u = await db.get_user(request["uid"])
    return web.json_response({"signed_in": True, "name": u["first_name"],
                              "balance": u["balance"]})


async def api_notify(request):
    d = await body(request)
    if "orders" in d:
        await db.set_notify(request["uid"], "notify_orders", bool(d["orders"]))
    if "promos" in d:
        await db.set_notify(request["uid"], "notify_promos", bool(d["promos"]))
    u = await db.get_user(request["uid"])
    return web.json_response({"orders": bool(u["notify_orders"]),
                              "promos": bool(u["notify_promos"])})


async def api_catalog(request):
    out = []
    for c in await db.categories():
        prods = []
        for p in await db.products(c["id"], only_active=True):
            prods.append({**dict(p),
                          "price": (await pricing.price_for(p, request["uid"])
                                    if request["uid"] else round(p["price"], 2)),
                          "list_price": p["price"],
                          "stock": await db.stock_count(p["id"]),
                          "available": await db.available(p["id"])})
        if prods:
            out.append({**dict(c), "products": prods})
    return web.json_response(out)


async def api_orders(request):
    return web.json_response(rows(await db.user_orders(request["uid"], 50)))


async def api_order(request):
    o = await db.order(int(request.match_info["oid"]))
    if not o or (o["user_id"] != request["uid"] and not request["admin"]):
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(dict(o))


async def api_order_qr(request):
    o = await db.order(int(request.match_info["oid"]))
    if not o or o["user_id"] != request["uid"]:
        raise web.HTTPNotFound()
    prov = payments.get(o["provider"])
    inv = await prov.create(o)
    if not inv.qr_payload:
        raise web.HTTPNotFound()
    return web.Response(body=payments.qr_png(inv.qr_payload), content_type="image/png")


async def api_checkout(request):
    d = await body(request)
    uid = request["uid"]
    code = d.get("provider", "")
    prov = payments.get(code)
    # ask payments what's live rather than reading the raw setting: wallet
    # balance is always offered and never appears in ENABLED_PROVIDERS
    if not prov or code not in {p.code for p in payments.enabled()}:
        return web.json_response({"error": "That payment method is unavailable."}, status=400)

    kind = d.get("kind", "purchase")
    if kind == "topup":
        try:
            amount = round(float(d.get("amount", 0)), 2)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            return web.json_response({"error": "Enter an amount above zero."}, status=400)
        pid, qty, name = None, 1, "Wallet top-up"
    else:
        pid, qty = int(d.get("product_id", 0)), max(1, int(d.get("qty", 1)))
        p = await db.product(pid)
        if not p or not p["is_active"]:
            return web.json_response({"error": "That product is no longer listed."}, status=400)
        if not p["infinite"] and await db.stock_count(pid) < qty:
            return web.json_response({"error": "Not enough stock left."}, status=409)
        amount = round(await pricing.price_for(p, uid) * qty, 2)
        name = p["name"]

    # wallet balance settles immediately
    if code == "balance":
        u = await db.get_user(uid)
        if u["balance"] + 1e-9 < amount:
            return web.json_response({"error": "Balance too low."}, status=402)
        oid = await db.create_order(
            user_id=uid, kind="purchase", product_id=pid, product_name=name, qty=qty,
            amount=amount, provider="balance", pay_amount=amount, pay_unit=cfg.fiat)
        await db.add_balance(uid, -amount)
        await delivery.settle(request.app["bot"], oid)
        # hand the keys straight back so the app can show them without a second
        # round trip — a buyer shouldn't have to go looking for what they just paid for
        done = await db.order(oid)
        u = await db.get_user(uid)
        return web.json_response({
            "order_id": oid, "instant": True,
            "code": done["code"], "product": done["product_name"], "qty": done["qty"],
            "charged": amount, "balance": u["balance"],
            "items": [ln for ln in (done["delivered_text"] or "").split("\n") if ln],
        })

    if hasattr(prov, "unique_amount"):
        pay_amount = await prov.unique_amount(amount)
        pay_unit = prov.quote(amount)[1]
    else:
        pay_amount, pay_unit = prov.quote(amount)

    oid = await db.create_order(
        user_id=uid, kind=kind, product_id=pid, product_name=name, qty=qty, amount=amount,
        provider=code, pay_amount=pay_amount, pay_unit=pay_unit,
        expires_at=db.in_minutes(cfg.order_ttl))
    inv = await prov.create(await db.order(oid))
    if inv.pay_address:
        await db.set_order(oid, pay_address=inv.pay_address)

    res: dict[str, Any] = {
        "order_id": oid, "instant": False, "text": inv.text,
        "pay_amount": inv.pay_amount, "pay_unit": inv.pay_unit,
        "pay_address": inv.pay_address, "manual_ref": inv.manual_ref,
        "qr": bool(inv.qr_payload), "expires_in": cfg.order_ttl * 60,
    }
    # Telegram Stars: hand the buyer a native invoice to open
    if inv.native_stars:
        bot: Bot = request.app["bot"]
        res["invoice_link"] = await bot.create_invoice_link(
            title=name[:32], description=f"Order #{oid}", payload=f"order:{oid}",
            provider_token="", currency="XTR",
            prices=[{"label": name[:32], "amount": inv.native_stars}])
    return web.json_response(res)


async def api_check(request):
    o = await db.order(int(request.match_info["oid"]))
    if not o or o["user_id"] != request["uid"]:
        return web.json_response({"error": "not found"}, status=404)
    if o["status"] == "pending":
        found = await payments.get(o["provider"]).poll([o])
        for oid, ref in found:
            await delivery.settle(request.app["bot"], oid, ref)
        o = await db.order(o["id"])
    return web.json_response({"status": o["status"], "delivered": o["delivered_text"]})


async def api_submit_ref(request):
    oid = int(request.match_info["oid"])
    o = await db.order(oid)
    if not o or o["user_id"] != request["uid"] or o["status"] != "pending":
        return web.json_response({"error": "This order is not awaiting a reference."}, status=400)
    ref = str((await body(request)).get("ref", "")).strip()
    if not texts.valid_ref(ref):
        return web.json_response(
            {"error": "Paste the transaction hash or ID from your wallet — "
                      "one value, no spaces."}, status=400)
    if not await db.mark_seen(f"utr:{ref}", oid):
        return web.json_response({"error": "That reference was already submitted."}, status=409)
    await db.set_order(oid, status="awaiting_review", external_ref=ref)
    await delivery.notify_admins(
        request.app["bot"],
        f"🔎 <b>Review needed</b> — order #{oid}\nUser <code>{o['user_id']}</code> · "
        f"{cfg.money(o['amount'])}\nUTR: <code>{ref}</code>")
    return web.json_response({"status": "awaiting_review"})


async def api_cancel(request):
    oid = int(request.match_info["oid"])
    o = await db.order(oid)
    if o and o["user_id"] == request["uid"] and o["status"] == "pending":
        await db.set_order(oid, status="cancelled")
    return web.json_response({"ok": True})


# ------------------------------------------------------------- admin API
async def adm_stats(request):
    return web.json_response({
        **await db.stats(),
        "alerts": await db.alert_counts(),
        "series": await db.revenue_series(14),
        "low": rows(await db.low_stock(cfg.low_stock)),
        "threshold": cfg.low_stock,
    })


async def adm_catalog(request):
    return web.json_response(await db.catalog())


async def adm_category(request):
    d = await body(request)
    if request.method == "DELETE":
        await db.del_category(int(request.match_info["cid"]))
        return web.json_response({"ok": True})
    name = str(d.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "Name the category first."}, status=400)
    try:
        cid = await db.add_category(name)
    except Exception:
        return web.json_response({"error": "A category with that name exists."}, status=409)
    return web.json_response({"id": cid})


async def adm_product_create(request):
    d = await body(request)
    try:
        price = round(float(d.get("price", 0)), 2)
    except (TypeError, ValueError):
        return web.json_response({"error": "Price must be a number."}, status=400)
    name = str(d.get("name", "")).strip()
    if not name or price <= 0:
        return web.json_response({"error": "Give the product a name and a price."}, status=400)
    pid = await db.add_product(int(d["category_id"]), name, str(d.get("description", "")), price)
    return web.json_response({"id": pid})


ALLOWED_FIELDS = {"name", "description", "price", "is_active", "infinite", "static_payload",
                  "category_id", "emoji", "icon_emoji_id", "unit"}


async def adm_product_update(request):
    pid = int(request.match_info["pid"])
    if request.method == "DELETE":
        await db.del_product(pid)
        return web.json_response({"ok": True})
    d = {k: v for k, v in (await body(request)).items() if k in ALLOWED_FIELDS}
    if "price" in d:
        d["price"] = round(float(d["price"]), 2)
    for flag in ("is_active", "infinite"):
        if flag in d:
            d[flag] = int(bool(d[flag]))
    await db.update_product(pid, **d)
    return web.json_response(dict(await db.product(pid)))


async def adm_stock(request):
    pid = int(request.match_info["pid"])
    if request.method == "GET":
        r = await db.q("SELECT payload FROM stock WHERE product_id = ? AND is_sold = 0 "
                       "ORDER BY id", (pid,))
        return web.json_response({"items": [x["payload"] for x in r]})
    d = await body(request)
    if d.get("purge"):
        return web.json_response({"removed": await db.purge_sold(pid)})
    n = await db.add_stock(pid, str(d.get("lines", "")).splitlines())
    return web.json_response({"added": n, "stock": await db.stock_count(pid)})


async def adm_orders(request):
    status = request.query.get("status", "open")
    return web.json_response(rows(await db.list_orders(status, int(request.query.get("limit", 60)))))


async def adm_order_action(request):
    oid = int(request.match_info["oid"])
    action = request.match_info["action"]
    o = await db.order(oid)
    if not o:
        return web.json_response({"error": "not found"}, status=404)
    bot = request.app["bot"]
    if action == "approve":
        await db.set_order(oid, status="pending")
        ok = await delivery.settle(bot, oid, ref=o["external_ref"])
        return web.json_response({"ok": ok, "status": (await db.order(oid))["status"]})
    if action == "cancel":
        if o["status"] in {"delivered", "paid"}:
            return web.json_response({"error": "already settled"}, status=409)
        await db.set_order(oid, status="cancelled")
        await db.release_balance(oid)
        return web.json_response({"ok": True, "status": "cancelled"})
    if action == "reject":
        await db.set_order(oid, status="rejected")
        # same as the Telegram panel: hand back any reserved wallet balance
        await db.release_balance(oid)
        try:
            await bot.send_message(o["user_id"], f"❌ Order #{oid} was rejected — we couldn't "
                                                 "match your payment. Contact support if this "
                                                 "looks wrong.")
        except Exception:
            pass
        return web.json_response({"ok": True, "status": "rejected"})
    return web.json_response({"error": "unknown action"}, status=400)


async def adm_withdrawals(request):
    if request.method == "POST":
        d = await body(request)
        wid, action = int(d.get("id", 0)), d.get("action")
        wd = await db.withdrawal(wid)
        if not wd or wd["status"] != "pending":
            return web.json_response({"error": "already handled"}, status=409)
        bot = request.app["bot"]
        if action == "paid":
            u = await db.get_user(wd["user_id"])
            if u["balance"] + 1e-9 < wd["amount"]:
                return web.json_response(
                    {"error": "their balance no longer covers this"}, status=409)
            # deduct only now, so a rejected request never touches the balance
            await db.add_balance(wd["user_id"], -wd["amount"])
            await db.set_withdrawal(wid, status="paid", processed_at=db.now())
            note = (f"✅ <b>Withdrawal #{wid} sent</b>\n\n"
                    f"{cfg.money(wd['amount'])} via {wd['method']}")
        else:
            await db.set_withdrawal(wid, status="rejected", processed_at=db.now())
            note = (f"❌ Withdrawal #{wid} was rejected. Your balance is unchanged — "
                    "contact support if you need details.")
        try:
            await bot.send_message(wd["user_id"], note)
        except Exception:
            pass

    out = []
    for wd in await db.pending_withdrawals():
        u = await db.get_user(wd["user_id"])
        out.append({**dict(wd), "balance": u["balance"] if u else 0,
                    "username": (u["username"] if u else "") or "",
                    "name": (u["first_name"] if u else "") or ""})
    return web.json_response({"pending": out})


async def adm_users(request):
    out = []
    for u in await db.list_users(request.query.get("q", ""), 60):
        out.append({**dict(u), **await db.user_summary(u["tg_id"])})
    return web.json_response(out)


async def adm_user_action(request):
    uid = int(request.match_info["uid"])
    d = await body(request)
    if "delta" in d:
        await db.add_balance(uid, round(float(d["delta"]), 2))
        u = await db.get_user(uid)
        try:
            await request.app["bot"].send_message(
                uid, f"👛 Balance updated. New balance: <b>{cfg.money(u['balance'])}</b>")
        except Exception:
            pass
    if "banned" in d:
        await db.set_ban(uid, bool(d["banned"]))
    u = await db.get_user(uid)
    return web.json_response({**dict(u), **await db.user_summary(uid)})


async def adm_broadcast(request):
    text = str((await body(request)).get("text", "")).strip()
    if not text:
        return web.json_response({"error": "Write the message first."}, status=400)
    import asyncio
    bot = request.app["bot"]
    ids = await db.all_user_ids()
    sent = failed = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    return web.json_response({"sent": sent, "failed": failed, "total": len(ids)})


async def adm_texts(request):
    if request.method == "POST":
        for key, value in (await body(request)).items():
            if key not in texts.MESSAGES:
                continue
            if str(value).strip() == "":
                await db.ex("DELETE FROM settings WHERE key = ?", (f"text:{key}",))
            else:
                await db.set_setting(f"text:{key}", texts.to_source(
                    texts.normalise_pasted(texts.restore_tags(str(value)))))
    changed = await texts.overrides()
    return web.json_response({"messages": [
        {"key": key, "label": m.label, "section": m.section, "fields": list(m.fields),
         "default": texts.to_source(m.default),
         "value": texts.to_source(changed.get(key, m.default)),
         "edited": key in changed}
        for key, m in texts.MESSAGES.items()]})


async def adm_flair(request):
    """Sales feed target, sticker file ids and custom emoji ids."""
    if request.method == "POST":
        d = await body(request)
        if "test_buttons" in d:
            note = await flair.style_demo(request.app["bot"], request["uid"])
            return web.json_response({"ok": True, "note": note})
        if "test" in d:
            chat = str(d.get("chat", "")).strip()
            if not chat:
                return web.json_response({"error": "Set the group id first."}, status=400)
            fake = {"qty": 1, "amount": 499.0, "provider": "demo",
                    "user_id": request["uid"], "product_name": "Test product"}
            await db.set_setting("flair:sales_chat", chat)
            await flair.announce_sale(request.app["bot"], fake, None)
            return web.json_response({"ok": True})
        for key in ("sales_chat", "hide_amount", "sale_template", "sale_button"):
            if key in d:
                await db.set_setting(f"flair:{key}", str(d[key]))
        for slot in flair.SLOTS:
            if f"emoji_{slot}" in d:
                await db.set_setting(f"flair:emoji:{slot}", str(d[f"emoji_{slot}"]).strip())
            if f"sticker_{slot}" in d:
                await db.set_setting(f"flair:sticker:{slot}", str(d[f"sticker_{slot}"]).strip())

    await flair.reload()
    out = {
        "sales_chat": await db.setting("flair:sales_chat", ""),
        "hide_amount": await db.setting("flair:hide_amount", "0"),
        "sale_template": await db.setting("flair:sale_template", flair.DEFAULT_SALE_TEMPLATE),
        "sale_button": await db.setting("flair:sale_button", "0"),
        "custom_emoji_working": flair._custom_ok,
        "slots": [{"name": n, "fallback": flair.SLOTS[n], "label": flair.slot_label(n),
                   "section": flair.slot_section(n),
                   "emoji": await db.setting(f"flair:emoji:{n}", ""),
                   "sticker": await db.setting(f"flair:sticker:{n}", "")}
                  for n in flair.SLOTS_META],
    }
    return web.json_response(out)


async def adm_settings(request):
    if request.method == "GET":
        return web.json_response(await db.all_settings())
    d = await body(request)
    for k, v in d.items():
        await db.set_setting(str(k), str(v))
    return web.json_response(await db.all_settings())


# ------------------------------------------------------- developer API
async def api_auth(request: web.Request):
    """Key auth for programmatic access. Separate from the Mini App's initData —
    a leaked key can only spend that one user's balance."""
    if not cfg.api_enabled:
        return None, web.json_response({"error": "api disabled"}, status=503)
    # accept both conventions — every client library reaches for one or other
    key = (request.headers.get("X-API-Key", "")
           or request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
    u = await db.user_by_api_key(key)
    if not u:
        return None, web.json_response({"error": "invalid api key"}, status=401)
    if u["is_banned"]:
        return None, web.json_response({"error": "account suspended"}, status=403)
    return u, None


# A reseller polling in a loop shouldn't be able to exhaust the shop for
# everyone else. Generous enough that no honest integration notices.
API_LIMIT = 120          # requests per key
API_WINDOW = 60          # seconds
_api_hits: dict[str, list[float]] = {}


def _rate_ok(key: str) -> bool:
    now = time.time()
    hits = [t for t in _api_hits.get(key, []) if now - t < API_WINDOW]
    hits.append(now)
    _api_hits[key] = hits
    return len(hits) <= API_LIMIT


@web.middleware
async def api_rate_limit(request: web.Request, handler):
    if not request.path.startswith("/api/v1/"):
        return await handler(request)
    key = (request.headers.get("X-API-Key")
           or request.headers.get("Authorization", "")
           or request.remote or "?")[:80]
    if not _rate_ok(key):
        return web.json_response(
            {"error": f"rate limit: {API_LIMIT} requests per {API_WINDOW}s"},
            status=429, headers={"Retry-After": str(API_WINDOW)})
    return await handler(request)


async def v1_products(request):
    u, err = await api_auth(request)
    if err:
        return err
    out = []
    for p in await db.products(None, only_active=True):
        out.append({"id": p["id"], "name": p["name"],
                    "price": await pricing.price_for(p, u["tg_id"]),
                    "list_price": p["price"],
                    "unit": p["unit"], "currency": cfg.fiat,
                    "stock": None if p["infinite"] else await db.stock_count(p["id"]),
                    "unlimited": bool(p["infinite"])})
    return web.json_response({"products": out})


async def v1_balance(request):
    u, err = await api_auth(request)
    if err:
        return err
    return web.json_response({"balance": u["balance"], "currency": cfg.fiat,
                              "on_hold": await db.locked_balance(u["tg_id"])})


async def v1_purchase(request):
    u, err = await api_auth(request)
    if err:
        return err
    d = await body(request)
    try:
        pid, qty = int(d.get("product_id", 0)), max(1, int(d.get("qty", 1)))
    except (TypeError, ValueError):
        return web.json_response({"error": "product_id and qty must be numbers"}, status=400)

    p = await db.product(pid)
    if not p or not p["is_active"]:
        return web.json_response({"error": "product not found"}, status=404)
    if not p["infinite"] and await db.stock_count(pid) < qty:
        return web.json_response({"error": "insufficient stock"}, status=409)

    amount = round(await pricing.price_for(p, u["tg_id"]) * qty, 2)
    free = round(u["balance"] - await db.locked_balance(u["tg_id"]), 2)
    if free + 1e-9 < amount:
        return web.json_response({"error": "insufficient balance",
                                  "required": amount, "available": free}, status=402)

    oid = await db.create_order(
        user_id=u["tg_id"], kind="purchase", product_id=pid, product_name=p["name"],
        qty=qty, amount=amount, provider="balance", pay_amount=amount, pay_unit=cfg.fiat)
    await db.add_balance(u["tg_id"], -amount)
    ok = await delivery.settle(request.app["bot"], oid)
    o = await db.order(oid)
    if not ok:
        return web.json_response({"error": "could not fulfil, balance refunded",
                                  "order_id": oid}, status=409)
    return web.json_response({
        "order_id": oid, "product": p["name"], "qty": qty, "charged": amount,
        "items": (o["delivered_text"] or "").split("\n"),
        "balance": (await db.get_user(u["tg_id"]))["balance"],
    })


# ------------------------------------------------------------------ wiring
def _page(name: str):
    async def handler(_request):
        return web.FileResponse(STATIC / name)
    return handler


@web.middleware
async def json_errors(request, handler):
    """Turn an unhandled exception into JSON the Mini App can read.

    Without this, aiohttp answers a crash with an HTML 500 page, the client
    can't parse it, and every fault in every screen renders as the same
    "Something went wrong" — which says nothing and makes a one-line SQL bug
    take a day to find. The full traceback goes to the log either way.

    Detail is only returned on admin routes. A buyer has no use for a Python
    exception, and error text is a small information leak.
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        log.exception("unhandled error on %s %s", request.method, request.path)
        detail = f"{type(e).__name__}: {e}"[:300]
        if not request.path.startswith("/api/admin"):
            detail = "Something went wrong. Try again."
        return web.json_response({"error": detail}, status=500)


def build_app(bot: Bot) -> web.Application:
    app = web.Application(middlewares=[json_errors, api_rate_limit, auth_middleware])
    app["bot"] = bot
    r = app.router

    r.add_get("/", _page("shop.html"))
    r.add_get(cfg.admin_path, _page("admin.html"))
    r.add_get("/health", lambda _: web.Response(text="ok"))

    async def _build(_req):
        """Which copy of the code is actually running.

        Derived from the newest source file rather than a stamp someone has to
        remember to bump — a version marker that can silently go stale is worse
        than none, because it makes a stale deploy look current.
        """
        import re as _re
        import pathlib
        import time as _time
        import flair as _flair
        import handlers_admin as _ha
        import handlers_user as _hu

        root = pathlib.Path(__file__).parent
        files = list(root.glob("*.py")) + list(STATIC.iterdir())
        newest = max((f.stat().st_mtime for f in files), default=0)
        cmds = sorted({
            c for mod in (_ha, _hu)
            for c in _re.findall(r'Command\("(\w+)"\)',
                                 open(mod.__file__, encoding="utf-8").read())})
        return web.json_response({
            "build": _time.strftime("%Y%m%d-%H%M", _time.gmtime(newest)),
            "newest_file": max(files, key=lambda f: f.stat().st_mtime).name,
            "commands": cmds,
            "icon_slots": len(_flair.SLOTS_META),
            "messages": __import__("texts").MESSAGES.__len__(),
            "rails": sorted(p.code for p in __import__("payments").enabled()),
            "files": sorted(p.name for p in STATIC.iterdir()),
        })
    r.add_get("/build", _build)

    r.add_post("/api/auth/telegram", api_login)
    r.add_get("/api/me", api_me)
    r.add_post("/api/activate", api_activate)
    r.add_post("/api/notify", api_notify)
    r.add_get("/api/catalog", api_catalog)
    r.add_get("/api/orders", api_orders)
    r.add_get("/api/order/{oid}", api_order)
    r.add_get("/api/order/{oid}/qr", api_order_qr)
    r.add_get("/api/order/{oid}/check", api_check)
    r.add_post("/api/checkout", api_checkout)
    r.add_post("/api/order/{oid}/ref", api_submit_ref)
    r.add_post("/api/order/{oid}/cancel", api_cancel)

    r.add_get("/api/admin/stats", adm_stats)
    r.add_get("/api/admin/catalog", adm_catalog)
    r.add_post("/api/admin/category", adm_category)
    r.add_delete("/api/admin/category/{cid}", adm_category)
    r.add_post("/api/admin/product", adm_product_create)
    r.add_patch("/api/admin/product/{pid}", adm_product_update)
    r.add_delete("/api/admin/product/{pid}", adm_product_update)
    r.add_get("/api/admin/product/{pid}/stock", adm_stock)
    r.add_post("/api/admin/product/{pid}/stock", adm_stock)
    r.add_get("/api/admin/orders", adm_orders)
    r.add_post("/api/admin/order/{oid}/{action}", adm_order_action)
    r.add_get("/api/admin/withdrawals", adm_withdrawals)
    r.add_post("/api/admin/withdrawals", adm_withdrawals)
    r.add_post("/api/terms", api_terms_accept)
    r.add_get("/api/admin/users", adm_users)
    r.add_post("/api/admin/user/{uid}", adm_user_action)
    r.add_post("/api/admin/broadcast", adm_broadcast)
    r.add_get("/api/admin/settings", adm_settings)
    r.add_post("/api/admin/settings", adm_settings)
    r.add_get("/api/admin/texts", adm_texts)
    r.add_post("/api/admin/texts", adm_texts)
    r.add_get("/api/admin/flair", adm_flair)
    r.add_post("/api/admin/flair", adm_flair)

    if cfg.webhook_enabled:
        import webhook
        webhook.add_routes(app)

    r.add_get("/api/v1/products", v1_products)
    r.add_get("/api/v1/balance", v1_balance)
    r.add_post("/api/v1/purchase", v1_purchase)

    r.add_static("/static/", STATIC)
    return app


async def serve(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, cfg.webapp_host, cfg.webapp_port).start()
    log.info("mini apps on http://%s:%s (public: %s)",
             cfg.webapp_host, cfg.webapp_port, cfg.webapp_url or "not set")
    return runner


async def start(bot: Bot) -> web.AppRunner:
    return await serve(build_app(bot))
