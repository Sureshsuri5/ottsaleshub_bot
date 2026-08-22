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
import secrets
import json
import asyncio
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
import timefmt
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
# A session lasts until it is signed out. Ten years rather than a literal
# never: the token carries its own expiry, and a field that can't expire has no
# way to be shortened later if that turns out to be a mistake. The renewal
# below still slides it forward, so an active session never approaches even
# this. Sign out is the way off a device.
SESSION_TTL = 10 * 365 * 24 * 3600


def _sign(payload: str) -> str:
    return hmac.new(cfg.bot_token.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_session(uid: int) -> str:
    exp = int(time.time()) + SESSION_TTL
    body = f"{uid}.{exp}"
    return f"{body}.{_sign(body)}"


# A login link is good for two minutes and one use. Short because it travels
# through Telegram and sits in a chat; single-use because a link that still
# works after you've clicked it is a password with extra steps.
LOGIN_TTL = 120

# Password hashing. scrypt is in the standard library, so no dependency, and
# it is memory-hard — a leaked table can't be brute-forced with a GPU the way
# a plain SHA-256 table can.
_SCRYPT = dict(n=2 ** 14, r=8, p=1)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            dklen=32, **_SCRYPT)
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), want)


# Login attempts per email and per IP. Password guessing is only practical at
# volume, so the cheapest real defence is refusing to answer quickly.
_TRIES: dict[str, list[float]] = {}
LOGIN_TRIES, LOGIN_WINDOW = 5, 300


def _login_ok(key: str) -> bool:
    now = time.time()
    hits = [t for t in _TRIES.get(key, []) if now - t < LOGIN_WINDOW]
    _TRIES[key] = hits
    if len(hits) >= LOGIN_TRIES:
        return False
    hits.append(now)
    return True


def issue_login(uid: int) -> str:
    exp = int(time.time()) + LOGIN_TTL
    nonce = secrets.token_urlsafe(9)
    body = f"{uid}.{exp}.{nonce}"
    return f"{body}.{_sign('login:' + body)}"


async def redeem_login(token: str) -> int | None:
    """Turn a one-time link into a user id, or None if it can't be used.

    Burning the nonce before returning means a link forwarded to someone else,
    or replayed from history, is already spent.
    """
    try:
        uid, exp, nonce, sig = token.split(".")
    except (AttributeError, ValueError):
        return None
    if not hmac.compare_digest(_sign(f"login:{uid}.{exp}.{nonce}"), sig):
        return None
    if int(exp) < time.time():
        return None
    if not cfg.is_admin(int(uid)):
        return None
    key = f"panel:used:{nonce}"
    if await db.setting(key, ""):
        return None
    await db.set_setting(key, str(int(time.time())))
    return int(uid)


def read_session(token: str) -> int | None:
    """The admin/buyer session. Maker tokens are deliberately not accepted.

    A maker token carries an `m` prefix on its subject, so int() below rejects
    it — but only because the ValueError is caught. Without that catch this
    raises a 500 on a token that should simply be refused, and a crash is a
    worse answer than a denial.
    """
    try:
        uid, exp, sig = token.split(".")
    except (AttributeError, ValueError):
        return None
    if not hmac.compare_digest(_sign(f"{uid}.{exp}"), sig):
        return None
    try:
        if int(exp) < time.time():
            return None
        return int(uid)
    except ValueError:
        return None


def issue_maker_session(mid: int) -> str:
    """A maker's session. Same signing key, different subject namespace.

    The `m` prefix is the security boundary. A maker token is not merely
    flagged as lower-privilege — it cannot be parsed as an admin subject at
    all, so read_session refuses it without needing to know makers exist.
    Getting this wrong the other way, with one shared format and a role field
    checked at each endpoint, means one missed check hands a supplier the shop.
    """
    exp = int(time.time()) + SESSION_TTL
    body = f"m{mid}.{exp}"
    return f"{body}.{_sign(body)}"


def read_maker_session(token: str) -> int | None:
    try:
        sub, exp, sig = token.split(".")
    except (AttributeError, ValueError):
        return None
    if not sub.startswith("m"):
        return None
    if not hmac.compare_digest(_sign(f"{sub}.{exp}"), sig):
        return None
    try:
        if int(exp) < time.time():
            return None
        return int(sub[1:])
    except ValueError:
        return None


def session_needs_renew(token: str) -> bool:
    """True once a valid session is past halfway through its life.

    Renewing on every request would mint a token per API call for no gain;
    renewing at the halfway mark means anyone using the panel stays signed in
    indefinitely, while a session that is simply abandoned still lapses on
    schedule. That is the difference between "don't log me out while I'm
    working" and "this token is good forever".
    """
    try:
        _, exp, _ = token.split(".")
        return int(exp) - time.time() < SESSION_TTL / 2
    except (AttributeError, ValueError):
        return False


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
# Reachable without a session — they are how you *get* one. The sign-in and
# one-time-link endpoints have to be here or the middleware rejects them before
# the password is ever checked, which looks exactly like a wrong password.
PUBLIC_PATHS = {"/api/me", "/api/catalog", "/api/auth/telegram",
                "/api/panel/auth", "/api/panel/login"}


@web.middleware
async def auth_middleware(request: web.Request, handler: Callable):
    if not request.path.startswith("/api/") or request.path.startswith("/api/v1/"):
        return await handler(request)

    # Makers are handled first and return early, so a maker request never
    # reaches the admin logic below. The two paths share no state: a maker
    # never gets request["uid"] or request["admin"] set at all, so an admin
    # endpoint reached by mistake finds no user and refuses, rather than
    # finding a user whose privileges depend on a check being remembered.
    if request.path.startswith("/api/maker/"):
        if request.path == "/api/maker/auth":
            return await handler(request)
        mid = read_maker_session(request.headers.get("X-Session", "")
                                 or request.query.get("_session", ""))
        if mid is None:
            return web.json_response({"error": "Sign in required."}, status=401)
        row = await db.maker(mid)
        if not row or not row["is_active"]:
            return web.json_response({"error": "Account disabled."}, status=403)
        request["maker_id"] = mid
        # Observed presence, throttled to once a minute inside the query. This
        # is what makes a stale "online" tick detectable later.
        try:
            await db.touch_maker_seen(mid)
        except Exception:                               # pragma: no cover
            pass
        return await handler(request)

    user = verify_init_data(request.headers.get("X-Init-Data", "")
                            or request.query.get("_auth", ""))
    renew_for: int | None = None
    has_session = bool(request.headers.get("X-Session", "")
                       or request.query.get("_session", ""))
    if user is not None and not has_session:
        # Telegram stops refreshing initData after a day, and inside the Mini
        # App there is no session behind it — so the panel would authenticate
        # fine all week and then fail with "No access" on the same device that
        # was working an hour earlier. Minting a session on the first
        # authenticated call gives that path something durable to fall back on.
        try:
            renew_for = int(user["id"])
        except (KeyError, TypeError, ValueError):       # pragma: no cover
            renew_for = None

    if user is None:                                  # browser session cookie
        raw = (request.headers.get("X-Session", "")
               or request.query.get("_session", ""))
        uid = read_session(raw)
        if uid:
            row = await db.get_user(uid)
            if row:
                user = {"id": uid, "username": row["username"],
                        "first_name": row["first_name"]}
                if session_needs_renew(raw):
                    renew_for = uid

    if user is None and cfg.panel_token:
        # query string as well as header: an <img src> can't send headers, so
        # the QR image had no way to authenticate in browser-token mode and
        # came back 401 as a broken image
        token = (request.headers.get("X-Admin-Token", "")
                 or request.query.get("token", ""))
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
    resp = await handler(request)
    if renew_for is not None:
        # A fresh token rides back on the response and the client swaps it in.
        # Guarded: a streamed response may already be on the wire, and a failed
        # renewal must never turn a working request into an error — the old
        # token is still valid, so the worst case is renewing on the next call.
        try:
            if not getattr(resp, "prepared", False):
                resp.headers["X-Session-Renew"] = issue_session(renew_for)
        except Exception:                               # pragma: no cover
            pass
    return resp


# Timestamps are stored UTC. The bot renders them in the shop's timezone via
# timefmt; the web app was printing the raw column, so the same order showed
# two different times depending on where you looked at it.
_TIME_COLS = ("created_at", "paid_at", "expires_at", "processed_at")


def rows(seq) -> list[dict]:
    return [with_local_times(dict(r)) for r in seq]


def with_local_times(d: dict) -> dict:
    """Add a `_local` twin for each timestamp, in the shop's timezone."""
    for col in _TIME_COLS:
        if col in d and d[col]:
            d[f"{col}_local"] = timefmt.local_dt(d[col])
    return d


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
                          # web-safe: Telegram markup and premium emoji tokens
                          # mean nothing in a browser and were being printed raw
                          "description": flair.plain(p["description"]),
                          "note": flair.plain(p["note"] if "note" in p.keys() else ""),
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
    return web.json_response(with_local_times(dict(o)))


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
        "counts": await db.order_counts(),
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
                  "category_id", "emoji", "icon_emoji_id", "unit", "note", "cost", "manual",
                  "maker_id", "ask_for", "needs_otp"}


async def adm_product_update(request):
    pid = int(request.match_info["pid"])
    if request.method == "DELETE":
        await db.del_product(pid)
        return web.json_response({"ok": True})
    d = {k: v for k, v in (await body(request)).items() if k in ALLOWED_FIELDS}
    if "price" in d:
        d["price"] = round(float(d["price"]), 2)
    for flag in ("is_active", "infinite", "manual", "needs_otp"):
        if flag in d:
            d[flag] = int(bool(d[flag]))
    await db.update_product(pid, **d)
    return web.json_response(dict(await db.product(pid)))


async def adm_stock(request):
    pid = int(request.match_info["pid"])
    if request.method == "GET":
        rows_ = await db.stock_rows(pid)
        return web.json_response({
            "items": [x["payload"] for x in rows_],          # legacy shape
            "rows": [{"id": x["id"], "payload": x["payload"]} for x in rows_],
            "sold": (await db.q1("SELECT COUNT(*) c FROM stock "
                                 "WHERE product_id = ? AND is_sold = 1", (pid,)))["c"],
        })
    d = await body(request)
    if d.get("delete_id"):
        ok = await db.delete_stock(pid, int(d["delete_id"]))
        if not ok:
            return web.json_response(
                {"error": "That item is already sold or gone."}, status=400)
        return web.json_response({"ok": True, "stock": await db.stock_count(pid)})
    if d.get("clear_unsold"):
        removed = await db.clear_unsold(pid)
        return web.json_response({"removed": removed,
                                  "stock": await db.stock_count(pid)})
    if d.get("purge"):
        return web.json_response({"removed": await db.purge_sold(pid)})
    added, skipped = await db.add_stock(pid, str(d.get("lines", "")).splitlines())
    return web.json_response({"added": added, "skipped": skipped,
                              "stock": await db.stock_count(pid)})


async def adm_orders(request):
    status = request.query.get("status", "open")
    return web.json_response({
        "orders": rows(await db.list_orders(
            status, int(request.query.get("limit", 60)),
            term=request.query.get("q", ""))),
        "counts": await db.order_counts(),
    })


async def adm_order_action(request):
    oid = int(request.match_info["oid"])
    action = request.match_info["action"]
    o = await db.order(oid)
    if not o:
        return web.json_response({"error": "not found"}, status=404)
    bot = request.app["bot"]
    # A dialog in the panel stops a mis-tap, but not a double-tap that lands
    # twice, a stale tab whose buttons predate the delivery, or a retried
    # request. settle() refuses to deliver an order that is already paid or
    # delivered — but only by reading its status, and approve used to overwrite
    # that status with "pending" before calling it, which walked straight past
    # the guard and allocated a second set of stock. Check before touching it.
    SETTLED = {"paid", "delivered", "fulfilling", "cancelled", "rejected"}
    if action == "approve":
        if o["status"] in SETTLED:
            return web.json_response(
                {"error": f"order is already {o['status']}"}, status=409)
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
        # release_balance below hands money back. On an order that already
        # shipped that is a refund with the goods still gone, so treat a
        # settled order as out of reach here the same way cancel does.
        if o["status"] in {"delivered", "paid", "fulfilling"}:
            return web.json_response(
                {"error": f"order is already {o['status']} — delete it instead"}, status=409)
        if o["status"] == "rejected":
            return web.json_response({"ok": True, "status": "rejected"})
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


async def adm_order_delete(request):
    """Remove an order permanently, undoing its stock and sold-count effects."""
    try:
        oid = int(request.query.get("id", "0") or (await body(request)).get("id", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "Which order?"}, status=400)
    if not oid:
        return web.json_response({"error": "Which order?"}, status=400)
    res = await db.delete_order(oid)
    if not res.get("deleted"):
        return web.json_response({"error": "No such order."}, status=404)
    log.info("order %s deleted by admin %s (was %s)", oid, request["uid"], res["was"])
    return web.json_response(res)


async def adm_order(request):
    """Everything about one order, including what was delivered.

    The list can only show a truncated reference and no items at all — this is
    where you look when a buyer asks what they were sent, or when you need the
    full transaction hash to check on-chain.
    """
    try:
        oid = int(request.query.get("id", "0"))
    except ValueError:
        oid = 0
    o = await db.order(oid)
    if not o:
        return web.json_response({"error": "No such order."}, status=404)
    u = await db.get_user(o["user_id"])
    d = with_local_times(dict(o))
    d["items"] = [ln for ln in (o["delivered_text"] or "").split("\n") if ln.strip()]
    d["username"] = u["username"] if u else None
    d["first_name"] = u["first_name"] if u else None
    d["balance"] = float(u["balance"]) if u else 0.0
    d["currency"] = cfg.symbol
    import payments
    d["explorer"] = payments.explorer_url(o["provider"], o["external_ref"] or "")
    return web.json_response(d)


async def adm_profit(request):
    d = await db.profit()
    d["currency"] = cfg.symbol
    return web.json_response(d)


async def adm_dashboard(request):
    d = await db.dashboard()
    d["currency"] = cfg.symbol
    return web.json_response(d)


async def adm_wallet_hide(request):
    """Hide a swept address from the wallet list, or bring it back.

    Hidden, not deleted: the address is derived from the seed and the row is an
    order that belongs in the history. Hiding one that still holds a balance is
    refused — the whole point of the list is that nothing with money in it can
    fall out of view.
    """
    d = await request.json()
    addr = str(d.get("address", "")).strip().lower()
    if not addr.startswith("0x"):
        return web.json_response({"error": "Bad address."}, status=400)

    hidden = {a for a in (await db.setting("wallet:hidden", "")).split(",") if a}
    if d.get("show"):
        hidden.discard(addr)
    else:
        prov = payments.get("bep20")
        bal = None
        if prov is not None and hasattr(prov, "token_balance"):
            try:
                bal = await asyncio.wait_for(prov.token_balance(addr), timeout=10)
            except Exception:
                bal = None
        if bal is not None and bal > 0.009:
            return web.json_response(
                {"error": f"That address still holds {bal:.2f}. Sweep it first."},
                status=400)
        if bal is None:
            return web.json_response(
                {"error": "Couldn't check the balance — the chain didn't answer. "
                          "Try again in a moment."}, status=503)
        hidden.add(addr)
    await db.set_setting("wallet:hidden", ",".join(sorted(hidden)))
    return web.json_response({"ok": True, "hidden": len(hidden)})


async def adm_wallet(request):
    """Which derived accounts hold funds, for the panel.

    Same source as /wallet in chat. The index is recovered by re-deriving from
    the xpub rather than stored on the order — a stored index could drift out
    of step with the address a buyer was actually shown.
    """
    import hdwallet
    if not hdwallet.ready():
        return web.json_response({"ready": False, "problem": hdwallet.problem()})

    try:
        nxt = int(await db.setting("hd:next_index", "0") or 0)
    except ValueError:
        nxt = 0
    where = {}
    for i in range(min(nxt, 2000)):
        try:
            where[hdwallet.address(i).lower()] = i
        except Exception:
            break

    rows = await db.q(
        "SELECT pay_address, status, amount, received, code FROM orders "
        "WHERE pay_address != '' AND pay_address IS NOT NULL "
        "ORDER BY id DESC LIMIT 300")
    shared = (cfg.evm_address or "").lower()
    acc: dict[str, dict] = {}
    pending = 0
    for r in rows:
        if r["status"] == "pending":
            pending += 1
            continue
        if r["status"] not in {"paid", "delivered", "credited"}:
            continue
        a = r["pay_address"]
        e = acc.setdefault(a.lower(), {
            "address": a, "amount": 0.0, "orders": 0, "late": 0,
            "index": where.get(a.lower()),
            "shared": a.lower() == shared})
        e["amount"] += float(r["received"] or 0) or float(r["amount"] or 0)
        e["orders"] += 1
        e["late"] += 1 if r["status"] == "credited" else 0

    hidden = {a for a in (await db.setting("wallet:hidden", "")).split(",") if a}
    show_all = request.query.get("all") == "1"
    # Only seed-derived accounts. EVM_ADDRESS is the shop's main wallet — the
    # destination swept funds are collected to — so its balance is a mix of
    # already-collected money and old pre-derivation orders. Reporting it here
    # would invite counting the same income twice.
    out = [e for e in acc.values()
           if e["index"] is not None
           and (show_all or e["address"].lower() not in hidden)]
    out = sorted(out, key=lambda e: (e["index"] is None, e["index"] or 0))
    for e in out:
        e["hidden"] = e["address"].lower() in hidden

    # Live on-chain balances. What the bot recorded arriving and what is still
    # sitting there are different numbers the moment you sweep, and the second
    # one is what decides whether a sweep is worth the gas. Queried in parallel
    # with a hard timeout: a slow node must not hang the whole panel.
    prov = payments.get("bep20")
    if prov is not None and hasattr(prov, "token_balance"):
        async def _bal(entry):
            try:
                entry["available"] = await prov.token_balance(entry["address"])
            except Exception:
                entry["available"] = None
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_bal(e) for e in out[:40])), timeout=20)
        except Exception:
            log.warning("on-chain balances timed out")

    known = [e["available"] for e in out if isinstance(e.get("available"), (int, float))]
    return web.json_response({
        "hidden_count": len(hidden),
        "showing_all": show_all,
        "unclaimed": round(sum(known), 2) if known else None,
        "unknown_balances": sum(1 for e in out if e.get("available") is None),
        "ready": True, "next_index": nxt, "path": hdwallet.PATH,
        "pending": pending,
        "total": round(sum(e["amount"] for e in out), 2),
        "accounts": out,
        "currency": cfg.symbol,
    })


async def adm_users(request):
    users = await db.list_users(request.query.get("q", ""), 60)
    summaries = await db.user_summaries([u["tg_id"] for u in users])
    return web.json_response([
        {**dict(u), **summaries.get(u["tg_id"], {"orders": 0, "spent": 0})}
        for u in users])


async def maker_auth(request):
    """Maker sign-in. Same rate limits and same vague error as the admin form."""
    d = await body(request)
    email = str(d.get("email", "")).strip().lower()[:120]
    password = str(d.get("password", ""))[:200]
    ip = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0]

    if not _login_ok(f"ip:{ip}") or not _login_ok(f"mk:{email}"):
        return web.json_response(
            {"error": "Too many attempts. Wait five minutes."}, status=429)

    row = await db.maker_by_email(email) if email else None
    if not row or not check_password(password, row["pw_hash"]):
        log.warning("failed maker login for %r from %s", email, ip)
        return web.json_response({"error": "Wrong email or password."}, status=401)
    if not row["is_active"]:
        return web.json_response({"error": "This account is disabled."}, status=403)

    await db.touch_maker_login(int(row["id"]))
    _TRIES.pop(f"mk:{email}", None)
    log.info("maker login by %s", email)
    return web.json_response({"session": issue_maker_session(int(row["id"])),
                              "name": row["name"] or row["email"]})


async def maker_status(request):
    d = await body(request)
    online = bool(d.get("online"))
    await db.set_maker_online(request["maker_id"], online)
    return web.json_response({"online": online})


async def maker_me(request):
    m = await db.maker(request["maker_id"])
    return web.json_response({"name": (m["name"] or m["email"]) if m else "",
                              "online": bool(m["is_online"]) if m else False,
                              "shop": cfg.shop_name})


async def maker_orders(request):
    closed = request.query.get("closed") == "1"
    rows = await db.maker_queue(request["maker_id"], closed=closed)
    return web.json_response({"items": [dict(r) for r in rows]})


async def maker_thread(request):
    oid = int(request.match_info["oid"])
    if not await db.maker_owns(request["maker_id"], oid):
        return web.json_response({"error": "Not your order."}, status=403)
    f = await db.fulfilment(oid)
    o = await db.order(oid)
    await db.fulfil_seen(oid)
    # Deliberately no buyer identity. A supplier needs the number to activate
    # against and the conversation to run — not who the customer is, what else
    # they have bought, or their Telegram handle.
    return web.json_response({
        "order": {"code": (o["code"] if o else None) or oid,
                  "product_name": o["product_name"] if o else "",
                  "qty": o["qty"] if o else 1},
        "fulfilment": {"stage": f["stage"], "number": f["number"],
                       "note": f["note"], "ask_for": f["ask_for"],
                       "extra": f["extra"],
                       "needs_otp": bool(f["needs_otp"])},
        "messages": [dict(m) for m in await db.fulfil_thread(oid)]})


async def maker_action(request):
    oid = int(request.match_info["oid"])
    if not await db.maker_owns(request["maker_id"], oid):
        return web.json_response({"error": "Not your order."}, status=403)
    d = await body(request)
    bot = request.app["bot"]
    act = str(d.get("action", "")).strip()

    # No cancel and no refund: those move money, and money stays with the shop
    # owner. A maker who cannot activate an order says so in the chat.
    if act == "send":
        text = str(d.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "Write a message first."}, status=400)
        if not await delivery.fulfil_to_user(bot, oid, text, sender="maker"):
            return web.json_response({"error": "Could not deliver."}, status=502)
    elif act == "ask_otp":
        await delivery.fulfil_request_otp(bot, oid)
    elif act == "note":
        await db.set_fulfil(oid, note=str(d.get("note", ""))[:500])
    elif act == "complete":
        if not await delivery.fulfil_complete(bot, oid):
            return web.json_response({"error": "Already closed."}, status=409)
        m = await db.maker(request["maker_id"])
        await delivery.notify_admins(
            bot, f"✅ Order <b>#{oid}</b> completed by "
                 f"<b>{(m['name'] or m['email']) if m else 'a maker'}</b>.")
    else:
        return web.json_response({"error": "Unknown action."}, status=400)

    f = await db.fulfilment(oid)
    o = await db.order(oid)
    # The same shape maker_thread returns. Omitting `order` here left the panel
    # holding a response without it, and the next render died on d.order.code —
    # so the first message or OTP request threw instead of repainting.
    return web.json_response({
        "order": {"code": (o["code"] if o else None) or oid,
                  "product_name": o["product_name"] if o else "",
                  "qty": o["qty"] if o else 1},
        "fulfilment": {"stage": f["stage"], "number": f["number"],
                       "note": f["note"], "ask_for": f["ask_for"],
                       "extra": f["extra"],
                       "needs_otp": bool(f["needs_otp"])} if f else None,
        "messages": [dict(m) for m in await db.fulfil_thread(oid)]})


# ------------------------------------------------------- maker admin (owner)

async def adm_makers(request):
    if request.method == "GET":
        return web.json_response({"makers": [dict(r) for r in await db.makers_list()]})
    d = await body(request)
    act = str(d.get("action", "")).strip()
    if act == "add":
        email = str(d.get("email", "")).strip().lower()[:120]
        password = str(d.get("password", ""))
        if "@" not in email or len(password) < 8:
            return web.json_response(
                {"error": "Need an email and a password of 8+ characters."},
                status=400)
        if await db.maker_by_email(email):
            return web.json_response({"error": "That email already exists."},
                                     status=409)
        await db.add_maker(email, hash_password(password),
                           str(d.get("name", "")))
    elif act == "password":
        password = str(d.get("password", ""))
        if len(password) < 8:
            return web.json_response({"error": "8 characters minimum."}, status=400)
        await db.set_maker_password(int(d["id"]), hash_password(password))
    elif act == "active":
        await db.set_maker_active(int(d["id"]), bool(d.get("active")))
    elif act == "delete":
        await db.drop_maker(int(d["id"]))
    else:
        return web.json_response({"error": "Unknown action."}, status=400)
    return web.json_response({"makers": [dict(r) for r in await db.makers_list()]})


async def adm_fulfil(request):
    """The work list. `closed=1` for the finished ones."""
    closed = request.query.get("closed") == "1"
    rows = await db.fulfil_queue(closed=closed)
    return web.json_response({"items": [dict(r) for r in rows],
                              **await db.fulfil_counts()})


async def adm_fulfil_thread(request):
    oid = int(request.match_info["oid"])
    f = await db.fulfilment(oid)
    if not f:
        return web.json_response({"error": "No such order."}, status=404)
    o = await db.order(oid)
    u = await db.get_user(f["user_id"])
    # opening the thread is what marks it read — the badge exists to say
    # "somebody has not looked at this yet", and now somebody has
    await db.fulfil_seen(oid)
    return web.json_response({
        "order": dict(o) if o else None,
        "fulfilment": dict(f),
        "user": dict(u) if u else None,
        "messages": [dict(m) for m in await db.fulfil_thread(oid)]})


async def adm_fulfil_action(request):
    oid = int(request.match_info["oid"])
    d = await body(request)
    bot = request.app["bot"]
    f = await db.fulfilment(oid)
    if not f:
        return web.json_response({"error": "No such order."}, status=404)
    act = str(d.get("action", "")).strip()

    if act == "send":
        text = str(d.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "Write a message first."}, status=400)
        if not await delivery.fulfil_to_user(bot, oid, text):
            return web.json_response({"error": "Could not deliver."}, status=502)
    elif act == "ask_otp":
        await delivery.fulfil_request_otp(bot, oid)
    elif act == "note":
        await db.set_fulfil(oid, note=str(d.get("note", ""))[:500])
    elif act == "complete":
        if not await delivery.fulfil_complete(bot, oid):
            return web.json_response({"error": "Already closed."}, status=409)
    elif act == "cancel":
        if not await delivery.fulfil_cancel(bot, oid, str(d.get("reason", "")).strip()):
            return web.json_response({"error": "Already closed."}, status=409)
    elif act == "close":
        # The reason is mandatory here and optional on `cancel`: this is the
        # path that keeps the buyer's money, so it is the one where an
        # unexplained close is worth refusing outright.
        reason = str(d.get("reason", "")).strip()
        if not reason:
            return web.json_response(
                {"error": "Give a reason — the buyer is told it."}, status=400)
        if not await delivery.fulfil_close(bot, oid, reason[:300]):
            return web.json_response({"error": "Already closed."}, status=409)
    else:
        return web.json_response({"error": "Unknown action."}, status=400)

    f = await db.fulfilment(oid)
    o = await db.order(oid)
    return web.json_response({
        "fulfilment": dict(f) if f else None,
        "order": dict(o) if o else None,
        "messages": [dict(m) for m in await db.fulfil_thread(oid)]})


async def adm_user_prices(request):
    """Custom prices for one buyer. GET lists them, POST sets or clears one."""
    uid = int(request.match_info["uid"])
    if request.method == "POST":
        d = await body(request)
        pid = int(d.get("product_id") or 0)
        if not pid:
            return web.json_response({"error": "Pick a product."}, status=400)
        if d.get("clear"):
            await db.clear_user_price(uid, pid)
        else:
            try:
                price = round(float(d.get("price")), 2)
            except (TypeError, ValueError):
                return web.json_response({"error": "Price must be a number."},
                                         status=400)
            if price < 0:
                return web.json_response({"error": "Price can't be negative."},
                                         status=400)
            await db.set_user_price(uid, pid, price)
    return web.json_response({"prices": [dict(r) for r in
                                         await db.user_price_rows(uid)]})


async def adm_referrers(request):
    rows = await db.top_referrers(200)
    return web.json_response([dict(r) for r in rows])


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
    if str(d.get("message", "")).strip():
        # A direct note to one person. Not routed through the broadcast path:
        # that resolves an audience and sends in the background, whereas this
        # is a single send whose failure the admin needs to see immediately —
        # if the buyer has blocked the bot, saying "sent" would be a lie.
        try:
            await request.app["bot"].send_message(
                uid, str(d["message"]).strip())
        except Exception as e:
            return web.json_response(
                {"error": f"Could not deliver: {e}"}, status=502)
    u = await db.get_user(uid)
    return web.json_response({**dict(u), **await db.user_summary(uid)})


def personalise(text: str, row: dict) -> str:
    """Fill the name placeholders for one recipient.

    Plain replacement rather than str.format: a broadcast is written by hand and
    will contain stray braces sooner or later — a price in {} or an emoji — and
    format() would raise on those and drop the whole send.

    The fallback matters as much as the substitution. Telegram makes first_name
    mandatory, but a blank one slips through, and "Hi ," reads worse than no
    name at all.
    """
    name = (row.get("first_name") or "").strip() or "there"
    user = (row.get("username") or "").strip()
    out = text.replace("{name}", name)
    return out.replace("{username}", f"@{user}" if user else name)


async def _run_broadcast(bot, rows: list[dict], text: str, by: int) -> None:
    """Send to everyone, then report back. Runs detached from the request.

    A few hundred recipients at Telegram's rate limit takes longer than any
    sensible HTTP timeout, so the request returns as soon as the audience is
    resolved and this carries on in the background. The admin gets a DM with
    the outcome, which is also the only place a partial failure is visible.
    """
    import asyncio
    sent = failed = blocked = 0
    for row in rows:
        try:
            await bot.send_message(row["tg_id"], personalise(text, row))
            sent += 1
        except Exception as e:
            # someone who blocked the bot is an expected outcome at this scale,
            # not an error worth flagging separately to the admin
            if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                blocked += 1
            else:
                failed += 1
        await asyncio.sleep(0.05)          # ~20/s, inside Telegram's limit
    try:
        await bot.send_message(
            by, f"📣 <b>Broadcast finished</b>\n\nDelivered: <b>{sent}</b>"
                f"\nBlocked the bot: {blocked}\nFailed: {failed}"
                f"\nAudience: {len(rows)}")
    except Exception:                                   # pragma: no cover
        log.warning("broadcast finished but the summary DM failed")


async def adm_broadcast(request):
    d = await body(request)
    text = str(d.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "Write the message first."}, status=400)

    rows = await db.broadcast_targets()
    if not rows:
        return web.json_response({"error": "Nobody to send to yet."}, status=400)

    # a dry run so the panel can show who it would reach, and what one of them
    # will actually see, before anything is sent
    if d.get("preview"):
        return web.json_response({"total": len(rows),
                                  "sample": personalise(text, rows[0])})

    import asyncio
    asyncio.create_task(_run_broadcast(
        request.app["bot"], rows, text, request["uid"]))
    return web.json_response({"queued": len(rows)})


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
        if "menu_button" in d:
            await db.set_setting("menu_button",
                                 "app" if d["menu_button"] else "commands")
            # applied straight away: a setting that needs a redeploy to take
            # effect is one the admin will assume is broken
            try:
                from aiogram.types import (MenuButtonCommands, MenuButtonWebApp,
                                           WebAppInfo)
                bot = request.app["bot"]
                if d["menu_button"] and cfg.miniapps_live:
                    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                        text=cfg.shop_name[:16] or "Shop",
                        web_app=WebAppInfo(url=cfg.webapp_url)))
                else:
                    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            except Exception as e:
                log.warning("menu button not applied: %s", e)
        if "miniapp_enabled" in d:
            await db.set_setting("miniapp_enabled",
                                 "1" if d["miniapp_enabled"] else "0")
        if "maintenance" in d:
            await db.set_setting("maintenance", "1" if d["maintenance"] else "0")
        for field, key in (("rails_disabled", "rails:disabled"),
                           ("rails_off_topup", "rails:disabled_topup"),
                           ("rails_off_purchase", "rails:disabled_purchase")):
            if field in d:
                off = [str(c).strip() for c in (d[field] or [])
                       if str(c).strip() and str(c).strip() != "balance"]
                await db.set_setting(key, ",".join(off))
        if any(f in d for f in ("rails_disabled", "rails_off_topup",
                                "rails_off_purchase")):
            await payments.reload_rails()
        for key in ("sales_chat", "restock_chat", "hide_amount",
                    "sale_template", "sale_button"):
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
        "restock_chat": await db.setting("flair:restock_chat", ""),
        "maintenance": await db.setting("maintenance", "0") == "1",
        "miniapp_enabled": await db.setting("miniapp_enabled", "1") != "0",
        "menu_button": await db.setting("menu_button", "commands") == "app",
        # every rail the shop could offer, with whether it's currently on and
        # whether it's actually configured — a rail can be "on" and still
        # hidden because it has no address set
        # split=True marks rails that also get per-context switches. Only the
        # manual, region-specific ones need them; a crypto rail is either
        # accepted or it isn't.
        "rails": [{"code": p.code, "title": p.title,
                   "on": p.code not in payments.DISABLED,
                   "topup": p.code not in payments.DISABLED_TOPUP,
                   "purchase": p.code not in payments.DISABLED_PURCHASE,
                   "split": p.code in payments.MANUAL_RAILS or p.code == "upi",
                   "ready": payments._ready(p.code)}
                  for p in (payments.REGISTRY[c] for c in cfg.providers
                            if c in payments.REGISTRY)],
        "hide_amount": await db.setting("flair:hide_amount", "0"),
        "sale_template": await db.setting("flair:sale_template", flair.DEFAULT_SALE_TEMPLATE),
        "sale_button": await db.setting("flair:sale_button", "0"),
        "custom_emoji_working": flair.custom_ok(),
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


async def panel_login(request):
    """Exchange a one-time link for a session."""
    d = await body(request)
    uid = await redeem_login(str(d.get("t", "")))
    if not uid:
        return web.json_response(
            {"error": "That login link has expired or been used. "
                      "Send /panel to the bot for a new one."}, status=401)
    log.info("panel login for admin %s", uid)
    return web.json_response({"session": issue_session(uid)})


async def panel_auth(request):
    """Email and password login.

    Wrong email and wrong password give the same answer, so this can't be used
    to discover which addresses exist.
    """
    d = await body(request)
    email = str(d.get("email", "")).strip().lower()[:120]
    password = str(d.get("password", ""))[:200]
    ip = request.headers.get("X-Forwarded-For", request.remote or "?").split(",")[0]

    if not _login_ok(f"ip:{ip}") or not _login_ok(f"em:{email}"):
        log.warning("panel login rate limited for %s / %s", ip, email)
        return web.json_response(
            {"error": "Too many attempts. Wait five minutes."}, status=429)

    row = await db.admin_login(email) if email else None
    if not row or not check_password(password, row["pw_hash"]):
        log.warning("failed panel login for %r from %s", email, ip)
        return web.json_response({"error": "Wrong email or password."}, status=401)
    if not cfg.is_admin(int(row["tg_id"])):
        return web.json_response({"error": "That account is no longer an admin."},
                                 status=403)

    await db.touch_admin_login(email)
    _TRIES.pop(f"em:{email}", None)
    log.info("panel login by %s", email)
    return web.json_response({"session": issue_session(int(row["tg_id"]))})


async def sms_inbound(request):
    """Bank credit SMS forwarded from a phone.

    The phone app posts the raw message; this reads the amount and UTR, records
    it, and settles the matching order. Two orders can ask for the same amount,
    so a credit is only auto-settled when exactly one open order matches —
    anything ambiguous waits for the buyer's UTR to disambiguate it.
    """
    if not cfg.sms_token:
        return web.json_response({"error": "sms forwarding is off"}, status=503)
    token = (request.headers.get("X-SMS-Token", "")
             or request.query.get("token", ""))
    if token != cfg.sms_token:
        return web.json_response({"error": "bad token"}, status=401)

    try:
        d = await request.json()
        text = str(d.get("text") or d.get("message") or "")
    except Exception:
        text = (await request.text())

    import smsparse
    parsed = smsparse.parse(text)
    if not parsed:
        # Not a credit — the phone forwards everything, and most of it is noise
        return web.json_response({"ok": True, "ignored": True})
    amount, utr = parsed

    fresh = await db.record_sms(utr, amount, text)
    if not fresh:
        return web.json_response({"ok": True, "duplicate": True, "utr": utr})

    # a buyer may have submitted this UTR before the SMS arrived
    waiting = await db.q(
        "SELECT * FROM orders WHERE status = 'awaiting_review' "
        "AND provider IN ('upi', 'razorpay') AND external_ref = ?", (utr,))
    if not waiting:
        candidates = await db.q(
            "SELECT * FROM orders WHERE status = 'pending' "
            "AND provider IN ('upi', 'razorpay') "
            "AND ABS(COALESCE(pay_amount, amount) - ?) <= 0.01", (amount,))
        if len(candidates) != 1:
            log.info("sms credit %s for %s: %d matching order(s), holding",
                     utr, amount, len(candidates))
            return web.json_response({"ok": True, "held": True,
                                      "matches": len(candidates)})
        waiting = candidates

    o = waiting[0]
    if abs(float(o["pay_amount"] or o["amount"]) - amount) > 0.01:
        log.warning("sms credit %s is %s but order %s wants %s",
                    utr, amount, o["id"], o["pay_amount"])
        return web.json_response({"ok": True, "mismatch": True})

    await db.claim_sms(utr, o["id"])
    await db.set_order(o["id"], external_ref=utr)
    import delivery
    await delivery.settle(request.app["bot"], o["id"], utr)
    log.info("sms credit %s settled order %s", utr, o["id"])
    return web.json_response({"ok": True, "settled": o["id"]})


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
                    "manual": bool(p["manual"]) if "manual" in p.keys() else False,
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

    # Idempotency. A reseller whose request times out has no way to know whether
    # the purchase happened; retrying could buy twice and not retrying loses the
    # keys. With a client_ref, a repeat call returns the original order instead
    # of creating another.
    ref = str(d.get("client_ref", ""))[:64].strip()
    if ref:
        prev = await db.order_by_client_ref(u["tg_id"], ref)
        if prev:
            return web.json_response(_v1_order(prev, repeated=True))

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
        qty=qty, amount=amount, provider="balance", pay_amount=amount,
        pay_unit=cfg.fiat, client_ref=ref)
    await db.add_balance(u["tg_id"], -amount)
    ok = await delivery.settle(request.app["bot"], oid)
    o = await db.order(oid)
    if not ok:
        return web.json_response({"error": "could not fulfil, balance refunded",
                                  "order_id": oid}, status=409)
    return web.json_response({
        **_v1_order(o),
        "balance": (await db.get_user(u["tg_id"]))["balance"],
    })


def _v1_order(o, repeated: bool = False) -> dict:
    """One order in the shape the developer API returns everywhere."""
    out = {
        "order_id": o["id"],
        "code": o["code"],
        "product": o["product_name"],
        "qty": o["qty"],
        "charged": o["amount"],
        "status": o["status"],
        "created_at": o["created_at"],
        "items": [ln for ln in (o["delivered_text"] or "").split("\n") if ln.strip()],
    }
    if o["client_ref"]:
        out["client_ref"] = o["client_ref"]
    if repeated:
        # so a caller can tell a replay from a fresh purchase
        out["repeated"] = True
    return out


async def v1_orders(request):
    """Recent orders for this key. Lets a reseller reconcile after a crash."""
    u, err = await api_auth(request)
    if err:
        return err
    try:
        limit = max(1, min(int(request.query.get("limit", 20)), 100))
    except ValueError:
        limit = 20
    rows_ = await db.q(
        "SELECT * FROM orders WHERE user_id = ? AND kind = 'purchase' "
        "ORDER BY id DESC LIMIT ?", (u["tg_id"], limit))
    return web.json_response({"orders": [_v1_order(r) for r in rows_]})


async def v1_order(request):
    """One order by its code, with the delivered items.

    The items are returned again on every call rather than once: if the
    original response was lost, this is the only way the buyer gets them back.
    """
    u, err = await api_auth(request)
    if err:
        return err
    code = request.match_info["code"]
    o = await db.order_by_code(u["tg_id"], code)
    if not o:
        o = await db.order_by_client_ref(u["tg_id"], code)
    if not o:
        return web.json_response({"error": "order not found"}, status=404)
    return web.json_response(_v1_order(o))


# ------------------------------------------------------------------ wiring
def _page(name: str):
    async def handler(_request):
        return web.FileResponse(STATIC / name)
    return handler


@web.middleware
async def no_stale_ui(request, handler):
    """Stop browsers serving a stale panel.

    The admin screens are a single HTML file with the JavaScript inline, so a
    cached copy means a deployed change is invisible until the person happens
    to hard-refresh — which has cost more debugging time this project than any
    actual bug. Revalidate every time; these files are small.
    """
    resp = await handler(request)
    try:
        if request.path.startswith("/static/") or request.path in ("/", "/admin"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    except AttributeError:
        pass
    return resp


@web.middleware
async def maintenance_gate(request, handler):
    """Close the Mini App too while maintenance is on.

    Blocking only the chat would leave the web app fully functional — buyers
    would keep checking out through a storefront the shop believes is shut.
    Admin routes stay open so the switch can be turned back off.
    """
    # /sms/ is a payment confirmation arriving from the outside, like the
    # Telegram webhook. Closing the shop must not stop money being recorded —
    # a buyer who already paid still needs their order settled, and a bank has
    # no way to retry later.
    if request.path.startswith(("/api/admin", "/health", "/build", "/tg/", "/sms/")) \
            or request.method == "GET" and not request.path.startswith("/api/"):
        return await handler(request)
    admin = cfg.is_admin(request.get("uid") or 0)
    if await db.setting("maintenance", "0") == "1" and not admin:
        import texts
        return web.json_response(
            {"error": await texts.t("maintenance"), "maintenance": True},
            status=503)
    # Hiding the menu button alone isn't enough — anyone with the URL, or an
    # old chat message containing it, could still open the storefront.
    if await db.setting("miniapp_enabled", "1") == "0" and not admin:
        return web.json_response(
            {"error": "The web shop is closed right now. Use the bot in "
                      "Telegram instead.", "miniapp_off": True}, status=503)
    return await handler(request)


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
    # auth first: the maintenance gate needs request["uid"] to spot an admin
    app = web.Application(middlewares=[no_stale_ui, json_errors, api_rate_limit,
                                       auth_middleware, maintenance_gate])
    app["bot"] = bot
    r = app.router

    r.add_get("/", _page("shop.html"))
    r.add_get(cfg.admin_path, _page("admin.html"))
    r.add_get("/maker", _page("maker.html"))
    r.add_post("/api/maker/auth", maker_auth)
    r.add_get("/api/maker/me", maker_me)
    r.add_post("/api/maker/status", maker_status)
    r.add_get("/api/maker/orders", maker_orders)
    r.add_get("/api/maker/fulfil/{oid}", maker_thread)
    r.add_post("/api/maker/fulfil/{oid}", maker_action)
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
    r.add_get("/api/admin/order", adm_order)
    r.add_delete("/api/admin/order", adm_order_delete)
    r.add_get("/api/admin/profit", adm_profit)
    r.add_get("/api/admin/dashboard", adm_dashboard)
    r.add_get("/api/admin/wallet", adm_wallet)
    r.add_post("/api/admin/wallet/hide", adm_wallet_hide)
    r.add_get("/api/admin/users", adm_users)
    r.add_get("/api/admin/referrers", adm_referrers)
    r.add_get("/api/admin/user/{uid}/prices", adm_user_prices)
    r.add_post("/api/admin/user/{uid}/prices", adm_user_prices)
    r.add_get("/api/admin/fulfil", adm_fulfil)
    r.add_get("/api/admin/fulfil/{oid}", adm_fulfil_thread)
    r.add_post("/api/admin/fulfil/{oid}", adm_fulfil_action)
    r.add_post("/api/admin/user/{uid}", adm_user_action)
    r.add_post("/api/admin/broadcast", adm_broadcast)
    r.add_get("/api/admin/makers", adm_makers)
    r.add_post("/api/admin/makers", adm_makers)
    r.add_get("/api/admin/settings", adm_settings)
    r.add_post("/api/admin/settings", adm_settings)
    r.add_get("/api/admin/texts", adm_texts)
    r.add_post("/api/admin/texts", adm_texts)
    r.add_get("/api/admin/flair", adm_flair)
    r.add_post("/api/admin/flair", adm_flair)

    if cfg.webhook_enabled:
        import webhook
        webhook.add_routes(app)

    r.add_post("/api/panel/login", panel_login)
    r.add_post("/api/panel/auth", panel_auth)
    r.add_post("/sms/inbound", sms_inbound)
    r.add_get("/api/v1/products", v1_products)
    r.add_get("/api/v1/balance", v1_balance)
    r.add_post("/api/v1/purchase", v1_purchase)
    r.add_get("/api/v1/orders", v1_orders)
    r.add_get("/api/v1/order/{code}", v1_order)

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
