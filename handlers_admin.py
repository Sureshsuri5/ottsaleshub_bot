from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import db
import delivery
import flair
import pricing
import texts
import keyboards as k
from config import cfg

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return cfg.is_admin(event.from_user.id)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class A(StatesGroup):
    cat_name = State()
    prod_name = State()
    prod_desc = State()
    prod_price = State()
    stock_lines = State()
    edit_value = State()
    find_user = State()
    bal_amount = State()
    approve_amount = State()
    rail_value = State()
    slot_value = State()
    intro_value = State()
    text_value = State()
    tier_name = State()
    tier_price = State()
    prod_keywords = State()
    broadcast = State()
    setting_value = State()


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _show(c: CallbackQuery, text: str, markup=None) -> None:
    """Edit the current screen in place; fall back to a new message."""
    try:
        if c.message.photo or c.message.document:
            await c.message.edit_caption(caption=text, reply_markup=markup)
        else:
            await c.message.edit_text(text, reply_markup=markup)
    except Exception:
        try:
            await c.message.answer(text, reply_markup=markup)
        except Exception:
            pass


async def _panel_text() -> str:
    s = await db.stats()
    al = await db.alert_counts()
    body = (f"🛠 <b>Admin panel</b>\n\n"
            f"👥 {s['users']} users · 📦 {s['products']} products\n"
            f"⏳ {s['pending']} open order(s) · 💰 {cfg.money(s['rev_today'])} today")
    alerts = []
    if al["withdrawals"]:
        alerts.append(f"💸 <b>{al['withdrawals']} withdrawal request(s)</b> waiting — "
                      f"{cfg.money(al['withdraw_total'])}")
    if al["reviews"]:
        alerts.append(f"🔎 <b>{al['reviews']} payment(s)</b> awaiting review")
    import payments
    if cfg.rate_warning():
        alerts.append(f"❗ <b>{esc(cfg.rate_warning())}</b>")
    if cfg.webapp_enabled and not cfg.miniapps_live:
        alerts.append("📱 <b>Mini App hidden</b> — WEBAPP_URL isn't a public https "
                      "address. Send /status")
    hidden = payments.misconfigured()
    if hidden:
        alerts.append(f"🏦 <b>{len(hidden)} payment rail(s)</b> hidden from buyers — "
                      "Settings → Payment rails")
    if alerts:
        body += "\n\n⚠️ <b>Needs you</b>\n" + "\n".join(alerts)
    return body


# ------------------------------------------------------------------ entry
@router.message(Command("admin"))
async def panel(m: Message, state: FSMContext):
    await state.clear()
    al = await db.alert_counts()
    await m.answer(await _panel_text(),
                   reply_markup=k.admin_menu(al["reviews"], al["withdrawals"]))


@router.callback_query(F.data == "a:home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    al = await db.alert_counts()
    await _show(c, await _panel_text(), k.admin_menu(al["reviews"], al["withdrawals"]))
    await c.answer()


@router.callback_query(F.data == "a:cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    al = await db.alert_counts()
    await _show(c, await _panel_text(), k.admin_menu(al["reviews"], al["withdrawals"]))
    await c.answer("Cancelled.")


def _cancel_kb():
    return k.kb([k.btn("✖ Cancel", "a:cancel")])


# -------------------------------------------------------------- flair ids
def has_custom_emoji(m: Message) -> bool:
    return any(e.type == "custom_emoji" for e in (m.entities or []))


@router.message(Command("flair"))
async def flair_menu(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await _flair_home()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "a:flair")
async def flair_home_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await _flair_home()
    await _show(c, text, markup)
    await c.answer()


async def _flair_home():
    await flair.reload()
    rows = []
    for section, slots in flair.sections().items():
        done = sum(1 for s in slots if flair.ICONS.get(s))
        rows.append([k.btn(f"{section} — {done}/{len(slots)} set", f"a:fsec:{section}")])
    emoji = await db.setting("flair:welcome_emoji", flair.SLOTS["welcome"])
    delay = await db.setting("flair:welcome_delay", "0")
    rows.append([k.btn(f"⏳ Intro: {emoji or 'off'} · {delay}s", "a:intro")])
    rows.append([k.btn("🧪 Test buttons", "a:btntest")])
    rows.append([k.btn("« Panel", "a:home")])
    total = len(flair.SLOTS_META)
    done = sum(1 for s in flair.SLOTS_META if flair.ICONS.get(s))
    return (
        "🎨 <b>Button icons</b>\n\n"
        f"Status: {'✅ enabled' if flair.ICONS_OK else '⚠️ disabled after a refusal'}\n"
        f"Set: <b>{done}</b> of {total} buttons\n\n"
        "Pick a section, then a button, then <b>send the premium emoji itself</b> — "
        "I'll read its id from the message.",
        k.kb(*rows),
    )


@router.callback_query(F.data.startswith("a:fsec:"))
async def flair_section(c: CallbackQuery, state: FSMContext):
    await state.clear()
    section = c.data.split(":", 2)[2]
    await flair.reload()
    rows = []
    for slot in flair.sections().get(section, []):
        fallback = flair.SLOTS[slot]
        rows.append([k.btn(f"{fallback} {flair.slot_label(slot)} "
                           f"{'✨' if flair.ICONS.get(slot) else '·'}", f"a:slot:{slot}")])
    rows.append([k.btn("« Sections", "a:flair")])
    await _show(c, f"🎨 <b>{esc(section)}</b>\n\n✨ marks a button that already has an icon.",
                k.kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("a:slot:"))
async def slot_edit(c: CallbackQuery, state: FSMContext):
    slot = c.data.split(":")[2]
    await state.set_state(A.slot_value)
    await state.update_data(slot=slot)
    await _show(c, f"Send the premium emoji for <b>{flair.slot_label(slot)}</b>.\n"
                   f"Current fallback: {flair.SLOTS[slot]}\n\n"
                   "Paste its numeric id instead if you prefer, or send <code>-</code> "
                   "to clear it.", _cancel_kb())
    await c.answer()


@router.message(A.slot_value)
async def slot_save(m: Message, state: FSMContext):
    slot = (await state.get_data())["slot"]
    await state.clear()
    ids = [e.custom_emoji_id for e in (m.entities or []) if e.type == "custom_emoji"]
    value = ids[0] if ids else (m.text or "").strip()
    if value == "-":
        value = ""
    elif not value.isdigit():
        return await m.answer("That wasn't a premium emoji. Send the emoji itself, or "
                              "paste its numeric id.", reply_markup=_cancel_kb())
    await db.set_setting(f"flair:emoji:{slot}", value)
    await flair.reload()
    await m.answer(
        f"✅ Saved for <b>{flair.slot_label(slot)}</b>.",
        reply_markup=k.kb(
            [k.btn(f"« {flair.slot_section(slot)}", f"a:fsec:{flair.slot_section(slot)}")],
            [k.btn("🧪 Test buttons", "a:btntest")]))


@router.callback_query(F.data == "a:intro")
async def intro_edit(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.intro_value)
    await _show(c,
        "⏳ <b>Intro beat</b>\n\n"
        "Sent right after /start, just before the welcome message.\n\n"
        "Send an emoji (it renders large on its own), optionally followed by "
        "how long it should stay on screen — e.g. <code>⏳ 1.5</code>\n"
        "It is cleared in the background, so it never delays the welcome.\n"
        "Send <code>-</code> to turn it off.\n\n"
        "<i>For a premium emoji, set the <b>Intro beat</b> slot under "
        "Other — it's used here automatically. A sticker on that slot wins "
        "over both.</i>",
        _cancel_kb())
    await c.answer()


@router.message(A.intro_value)
async def intro_save(m: Message, state: FSMContext):
    await state.clear()
    parts = (m.text or "").strip().split()
    emoji = parts[0] if parts else "-"
    delay = "0"
    if len(parts) > 1:
        try:
            delay = str(max(0.0, min(5.0, float(parts[1]))))
        except ValueError:
            pass
    await db.set_setting("flair:welcome_emoji", "" if emoji == "-" else emoji)
    await db.set_setting("flair:welcome_delay", delay)
    await m.answer(f"✅ Intro {'off' if emoji == '-' else emoji + ' · ' + delay + 's'}. "
                   "Send /start to see it.", reply_markup=k.back())


@router.callback_query(F.data == "a:btntest")
async def button_test(c: CallbackQuery):
    note = await flair.style_demo(c.bot, c.from_user.id)
    await c.answer("Sent — check the message below." if note == "ok" else note,
                   show_alert=True)


@router.message(Command("ids"))
async def ids_help(m: Message):
    await m.answer(
        "🎨 <b>Flair ids</b>\n\n"
        "• Send me a <b>sticker</b> and I'll return its file id.\n"
        "• Send a message containing a <b>premium emoji</b> and I'll return its "
        "custom emoji id.\n\n"
        "Paste either into the admin panel under <b>More → Sales feed &amp; flair</b>.\n\n"
        "<i>Premium emoji need the bot owner to have Telegram Premium, or the bot to own a "
        "Fragment username. Inline button labels can't show them at all — Telegram allows "
        "plain text only there.</i>")


@router.message(StateFilter(None), F.sticker)
async def sticker_id(m: Message):
    s = m.sticker
    await m.answer(f"🎟 <b>Sticker</b>\n\nfile id:\n<code>{s.file_id}</code>\n\n"
                   f"set: {esc(s.set_name or '—')} · emoji: {esc(s.emoji or '—')}\n"
                   f"{'custom emoji id: <code>%s</code>' % s.custom_emoji_id if s.custom_emoji_id else ''}")


@router.message(StateFilter(None), has_custom_emoji)
async def emoji_ids(m: Message):
    found = [e.custom_emoji_id for e in m.entities if e.type == "custom_emoji"]
    lines = "\n".join(f"<code>{i}</code>" for i in dict.fromkeys(found))
    await m.answer(f"✨ <b>Custom emoji id{'s' if len(found) > 1 else ''}</b>\n\n{lines}")


@router.message(Command("check"))
async def check_ref(m: Message, state: FSMContext):
    """Test a transaction hash against every rail without making a deposit."""
    await state.clear()
    import payments
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer(
            "🔍 <b>Check a transaction</b>\n\n"
            "Send <code>/check &lt;hash&gt;</code> and I'll look it up on every rail "
            "and report exactly what each one sees.\n\n"
            "Use it to prove a rail works before advertising it.")

    ref = parts[1].strip()
    out = [f"🔍 <b>Checking</b>\n<code>{esc(ref[:80])}</code>\n"]
    found = False
    for prov in payments.enabled():
        if not hasattr(prov, "verify_ref"):
            continue
        try:
            amount = await prov.verify_ref(ref)
        except Exception as e:
            out.append(f"⚠️ {prov.title} — lookup failed: {type(e).__name__}")
            continue
        if amount:
            found = True
            out.append(f"✅ <b>{prov.title}</b> — found, worth {cfg.money(amount)}")
        else:
            why = await prov.diagnose() if hasattr(prov, "diagnose") else "not found"
            out.append(f"❌ {prov.title} — {why}")

    if not found:
        out.append("\n<i>No rail could confirm this. If you did send it, check the "
                   "receiving address and network match the rail you're testing.</i>")
    await m.answer("\n".join(out), reply_markup=k.back())


BACKUP_TABLES = ("users", "categories", "products", "stock", "orders",
                 "tiers", "tier_prices", "withdrawals", "settings", "seen_tx")


@router.message(Command("backup"))
async def backup_cmd(m: Message, state: FSMContext):
    """Dump every table to a gzipped JSON file and send it here.

    Supabase's free plan keeps no point-in-time history, so the only copy of
    your balances and undelivered stock is the one you take yourself. JSON
    rather than SQL so it restores onto either engine.
    """
    await state.clear()
    import gzip
    import json
    from datetime import datetime, timezone

    note = await m.answer("📦 Building backup…")
    data: dict[str, list] = {}
    counts = []
    try:
        for table in BACKUP_TABLES:
            rows = await db.q(f"SELECT * FROM {table}")
            data[table] = [dict(r) for r in rows]
            counts.append(f"{table}: {len(rows):,}")
    except Exception as e:
        return await note.edit_text(f"❌ Backup failed: <code>{esc(str(e)[:300])}</code>")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    blob = gzip.compress(json.dumps(
        {"taken_at": db.now(), "backend": db.backend(), "tables": data},
        default=str).encode())

    # Bots can upload 50 MB. A shop that outgrows that should be dumping from
    # the database directly rather than through Telegram.
    if len(blob) > 49 * 1024 * 1024:
        return await note.edit_text(
            f"❌ Backup is {len(blob) / 1024 / 1024:.0f} MB — too large to send "
            "over Telegram. Use <code>pg_dump</code> against your Supabase URL.")

    await note.delete()
    await m.answer_document(
        BufferedInputFile(blob, filename=f"shop-backup-{stamp}.json.gz"),
        caption="🗄 <b>Backup</b>\n" + "\n".join(counts) +
                f"\n\n<i>{len(blob) / 1024:.0f} KB · {db.backend()}</i>",
        reply_markup=k.back())


@router.message(Command("wallet"))
async def wallet_cmd(m: Message, state: FSMContext):
    """Which derived accounts actually hold money, and where to find them.

    Addresses are matched back to their derivation index by re-deriving from
    the xpub rather than storing the index — the address is already on the
    order, and a stored index could drift out of step with it.
    """
    await state.clear()
    import hdwallet
    if not hdwallet.ready():
        return await m.answer(f"🔑 Per-order addresses are off — "
                              f"{esc(hdwallet.problem())}")

    try:
        nxt = int(await db.setting("hd:next_index", "0") or 0)
    except ValueError:
        nxt = 0
    if not nxt:
        return await m.answer("No deposit addresses issued yet.")

    # address -> index, for every address ever handed out
    where = {}
    for i in range(min(nxt, 2000)):
        try:
            where[hdwallet.address(i).lower()] = i
        except Exception:
            break

    rows = await db.q(
        "SELECT pay_address, status, amount, received, code FROM orders "
        "WHERE pay_address != '' AND pay_address IS NOT NULL "
        "ORDER BY id DESC LIMIT 200")

    funded: dict[str, list] = {}
    pending = 0
    for r in rows:
        if r["status"] in {"paid", "delivered"}:
            funded.setdefault(r["pay_address"].lower(), []).append(r)
        elif r["status"] == "pending":
            pending += 1

    if not funded:
        return await m.answer(
            f"🔑 <b>Deposit wallet</b>\n\n{nxt} address(es) issued, "
            f"{pending} awaiting payment.\nNone have received funds yet.")

    lines = ["🔑 <b>Accounts holding funds</b>", ""]
    total = 0.0
    shared = (cfg.evm_address or "").lower()
    for addr, orders in list(funded.items())[:25]:
        idx = where.get(addr)
        # what landed on the address, not what the order asked for — an
        # overpaid order holds more than its total, and this list exists to
        # tell you what is actually there to sweep
        got = sum(float(o["received"] or 0) or float(o["amount"] or 0)
                  for o in orders)
        total += got
        # MetaMask numbers accounts from 1, the derivation path from 0 — say
        # both, because the whole point is finding it in the wallet
        if idx is not None:
            seat = f"Account {idx + 1} (index {idx})"
        elif addr == shared:
            seat = "Shared address (orders before per-order addresses)"
        else:
            seat = "Unknown address"
        lines.append(f"<b>{seat}</b> — {cfg.money(got)}")
        lines.append(f"<code>{esc(orders[0]['pay_address'])}</code>")
        lines.append("")

    lines.append(f"<b>Total received: {cfg.money(total)}</b>")
    lines.append(f"<i>{pending} order(s) still awaiting payment. "
                 f"Each account needs a little BNB for gas before you can "
                 f"move its USDT out.</i>")
    await m.answer("\n".join(lines), reply_markup=k.back())


@router.message(Command("status"))
async def status_cmd(m: Message, state: FSMContext):
    """One screen that answers 'why isn't X showing up?'"""
    await state.clear()
    import payments
    await payments.reload_rails()

    lines = ["🩺 <b>Bot status</b>", ""]
    warn = cfg.rate_warning()
    if warn:
        lines += [f"❗ <b>{esc(warn)}</b>", ""]

    # --- Mini App ---
    if not cfg.webapp_enabled:
        lines.append("📱 Mini App: <b>disabled</b> — set WEBAPP_ENABLED=true")
    elif cfg.miniapps_live:
        lines.append(f"📱 Mini App: ✅ <b>live</b>\n<code>{esc(cfg.webapp_url)}</code>")
    else:
        why = ("WEBAPP_URL is empty" if not cfg.webapp_url
               else f"WEBAPP_URL is <code>{esc(cfg.webapp_url)}</code> — must start "
                    "with https://")
        lines += [f"📱 Mini App: ⚠️ <b>hidden</b> — {why}", "",
                  "<i>Telegram won't open a Mini App from localhost or http. Run a "
                  "tunnel, paste its https address into WEBAPP_URL, and restart:</i>",
                  "<code>cloudflared tunnel --url http://localhost:8080</code>"]

    # --- payment rails ---
    st = payments.status()
    live = [r for r in st if r["ready"]]
    lines += ["", f"💳 Rails: <b>{len(live)}</b> of {len(st)} visible to buyers"]
    for r in st:
        if not r["ready"]:
            lines.append(f"⚠️ {r['title']} — needs {r['need']}")
        elif r.get("manual_only"):
            lines.append(f"🖐 {r['title']} — live, but manual approval "
                         "(API plan doesn't cover this chain)")
        elif r.get("via") == "node":
            lines.append(f"✅ {r['title']} <i>(verified via a public node)</i>")
        elif not r["auto"]:
            lines.append(f"🖐 {r['title']} — live, manual approval")
        else:
            lines.append(f"✅ {r['title']}")

    # --- everything else worth knowing ---
    s = await db.stats()
    lines += ["",
              f"🔌 Update mode: <b>{'webhook' if cfg.use_webhook else 'polling'}</b>",
              f"🎨 Premium emoji: {'✅ working' if flair.ICONS_OK else '⚠️ refused'}",
              f"🗄 Database: <b>{esc(db.backend())}</b>",]

    # Per-order deposit addresses: show the path and the next index so the
    # addresses the bot will hand out can be checked against your own wallet
    # before a buyer ever sees one.
    import hdwallet
    if hdwallet.ready():
        nxt = await db.setting("hd:next_index", "0")
        lines.append(f"🔑 Deposit addresses: <b>one per order</b> · "
                     f"{esc(hdwallet.PATH.format(i=nxt))} next")
        for i, a in enumerate(hdwallet.preview(2)):
            lines.append(f"    <code>{esc(a)}</code>  (index {i})")
    elif cfg.evm_xpub:
        lines.append(f"🔑 Deposit addresses: ⚠️ {esc(hdwallet.problem())}")
    else:
        lines.append("🔑 Deposit addresses: shared address "
                     "(set EVM_XPUB for one per order)")

    lines += [
              f"📦 {s['products']} product(s) · {s['in_stock']} unit(s) in stock",
              f"👥 {s['users']} user(s) · 🕒 {cfg.timezone}"]

    await m.answer("\n".join(lines), reply_markup=k.back())


# ---------------------------------------------------------------- tiers
@router.callback_query(F.data == "a:tiers")
async def tiers_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    for t in await db.tiers():
        n = await db.tier_members(t["id"])
        rows.append([k.btn(f"{t['name']} · −{t['discount']:g}% · {n} user(s)",
                           f"a:tier:{t['id']}")])
    rows.append([k.btn("➕ Add tier", "a:tieradd")])
    rows.append([k.btn("« Panel", "a:home")])
    await _show(c,
                "🏷 <b>Price tiers</b>\n\n"
                "A tier gives its members a percentage off every product. You can "
                "also set an exact price per product on a tier, which wins over the "
                "percentage.\n\nUsers with no tier pay the list price.",
                k.kb(*rows))
    await c.answer()


@router.callback_query(F.data == "a:tieradd")
async def tier_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.tier_name)
    await _show(c, "Send the tier as <code>name percent</code>\n"
                   "e.g. <code>Reseller 20</code> or <code>VIP 35</code>", _cancel_kb())
    await c.answer()


@router.message(A.tier_name)
async def tier_add_save(m: Message, state: FSMContext):
    await state.clear()
    parts = (m.text or "").rsplit(maxsplit=1)
    try:
        name, pct = parts[0].strip(), float(parts[1])
        assert name and 0 <= pct < 100
    except Exception:
        return await m.answer("Send it as <code>Reseller 20</code>.", reply_markup=_cancel_kb())
    try:
        await db.add_tier(name, pct)
    except Exception:
        return await m.answer("A tier with that name already exists.",
                              reply_markup=k.back("a:tiers"))
    await m.answer(f"✅ <b>{esc(name)}</b> created at −{pct:g}%.",
                   reply_markup=k.back("a:tiers"))


@router.callback_query(F.data.startswith("a:tier:"))
async def tier_open(c: CallbackQuery):
    tid = int(c.data.split(":")[2])
    t = await db.tier(tid)
    n = await db.tier_members(tid)
    over = await db.q("SELECT p.name, tp.price FROM tier_prices tp "
                      "JOIN products p ON p.id = tp.product_id WHERE tp.tier_id = ?", (tid,))
    body = (f"🏷 <b>{esc(t['name'])}</b>\n\n"
            f"Discount: <b>−{t['discount']:g}%</b>\n"
            f"Members: <b>{n}</b>\n")
    if over:
        body += "\nExact prices:\n" + "\n".join(
            f"• {esc(r['name'])} — {cfg.money(r['price'])}" for r in over)
    await _show(c, body, k.kb(
        [k.btn("💲 Set product prices", f"a:tprices:{tid}")],
        [k.btn("✏️ Rename / change %", f"a:tieredit:{tid}")],
        [k.btn("🗑 Delete tier", f"a:tierdel:{tid}", style="danger")],
        [k.btn("« Tiers", "a:tiers")]))
    await c.answer()


@router.callback_query(F.data.startswith("a:tprices:"))
async def tier_price_list(c: CallbackQuery, state: FSMContext):
    """Every product's price for one tier, on one screen."""
    await state.clear()
    tid = int(c.data.split(":")[2])
    t = await db.tier(tid)
    rows = []
    for p in await db.products(None, only_active=False):
        exact = (await db.tier_prices(p["id"])).get(tid)
        if exact is not None:
            shown = f"{cfg.money(exact)} · set"
        elif t["discount"]:
            shown = f"{cfg.money(pricing.apply(p['price'], t['discount']))} · −{t['discount']:g}%"
        else:
            shown = f"{cfg.money(p['price'])} · list"
        rows.append([k.btn(f"{p['name'][:22]}: {shown}", f"a:tprice:{p['id']}:{tid}")])
    if not rows:
        rows.append([k.btn("No products yet", "noop")])
    rows.append([k.btn("« Tier", f"a:tier:{tid}")])
    await _show(c,
                f"💲 <b>{esc(t['name'])} — product prices</b>\n\n"
                f"Default for this tier: <b>−{t['discount']:g}%</b>\n\n"
                "Tap a product to set an exact price for this tier. Products you "
                "don't set follow the percentage above.",
                k.kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("a:tieredit:"))
async def tier_edit(c: CallbackQuery, state: FSMContext):
    tid = int(c.data.split(":")[2])
    await state.set_state(A.tier_name)
    await state.update_data(edit_tier=tid)
    await _show(c, "Send the new <code>name percent</code>, e.g. "
                   "<code>Reseller 25</code>", _cancel_kb())
    await c.answer()


@router.callback_query(F.data.startswith("a:tierdel:"))
async def tier_del(c: CallbackQuery):
    tid = int(c.data.split(":")[2])
    await db.del_tier(tid)
    await c.answer("Tier deleted — its members go back to list price.", show_alert=True)
    await _show(c, "🏷 Tier deleted — its members are back on list pricing.",
                k.back("a:tiers"))


# ------------------------------------------------------------- messages
@router.message(Command("texts"))
async def texts_cmd(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await _texts_home()
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "a:texts")
async def texts_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await _texts_home()
    await _show(c, text, markup)
    await c.answer()


async def _texts_home():
    changed = await texts.overrides()
    rows = []
    for section, keys in texts.sections().items():
        n = sum(1 for key in keys if key in changed)
        rows.append([k.btn(f"{section} — {n}/{len(keys)} edited", f"a:tsec:{section}")])
    if changed:
        rows.append([k.btn(f"↩ Restore all {len(changed)} to defaults", "a:tresetall")])
    rows.append([k.btn("« Panel", "a:home")])
    note = ("Every message the bot sends is here. Anything you don't change keeps "
            "its default.")
    if changed:
        note += ("\n\n<i>An edited message keeps the exact text you saved — including "
                 "its emoji. Restore it to pick up icon slots like "
                 "<code>{{w_point}}</code>.</i>")
    return (
        f"✏️ <b>Bot messages</b>\n\n"
        f"Edited: <b>{len(changed)}</b> of {len(texts.MESSAGES)}\n\n{note}",
        k.kb(*rows),
    )


@router.callback_query(F.data == "a:tresetall")
async def texts_reset_all(c: CallbackQuery, state: FSMContext):
    await state.clear()
    changed = await texts.overrides()
    await _show(c,
                f"↩️ Restore <b>all {len(changed)}</b> edited messages to their "
                "defaults?\n\nYour wording is discarded, and every message goes "
                "back to using the icon slots from /flair.",
                k.kb([k.btn("✅ Yes, restore all", "a:tresetall2", style="danger")],
                     [k.btn("« Cancel", "a:texts")]))
    await c.answer()


@router.callback_query(F.data == "a:tresetall2")
async def texts_reset_all_confirm(c: CallbackQuery, state: FSMContext):
    await state.clear()
    n = len(await texts.overrides())
    await db.ex("DELETE FROM settings WHERE key LIKE 'text:%'")
    text, markup = await _texts_home()
    await _show(c, f"↩️ Restored {n} message(s).\n\n{text}", markup)
    await c.answer(f"{n} restored")


@router.callback_query(F.data.startswith("a:tsec:"))
async def texts_section(c: CallbackQuery, state: FSMContext):
    await state.clear()
    section = c.data.split(":", 2)[2]
    changed = await texts.overrides()
    rows = [[k.btn(f"{'✏️' if key in changed else '·'} {texts.MESSAGES[key].label}",
                   f"a:text:{key}")]
            for key in texts.sections().get(section, [])]
    rows.append([k.btn("« Sections", "a:texts")])
    await _show(c, f"✏️ <b>{esc(section)}</b>\n\n✏️ marks a message you've changed.",
                k.kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("a:text:"))
async def text_edit(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 2)[2]
    msg = texts.MESSAGES[key]
    current = texts.to_source(await texts.raw(key))
    preview = await texts.t(key, **{f: "…" for f in msg.fields})
    await state.set_state(A.text_value)
    await state.update_data(text_key=key)
    fields = ("\n\nPlaceholders: " + " ".join(f"<code>{{{f}}}</code>" for f in msg.fields)
              if msg.fields else "")
    button = k.copy_btn("📋 Copy current text", current)

    overridden = key in await texts.overrides()
    state_line = ("✏️ <i>You've edited this message, so its emoji are fixed here "
                  "and /flair no longer changes them. Tap Restore default to put "
                  "it back under /flair's control.</i>"
                  if overridden else
                  "🎨 <i>Using the default — its emoji come from /flair, so "
                  "changing an icon there updates this message too.</i>")
    body = [f"✏️ <b>{esc(msg.label)}</b>", "", state_line, "",
            f"Preview:\n<blockquote>{preview}</blockquote>{fields}", ""]
    if button:
        body.append("Tap <b>Copy current text</b>, paste it into the box, edit it "
                    "and send it back.")
    else:
        # too long for a copy button, so give a code block — Telegram lets you
        # tap-and-hold a <pre> block to copy the whole thing
        body += ["Current text — tap and hold to copy, then paste, edit and send back:",
                 f"<pre>{esc(current)}</pre>",
                 "",
                 "<i>Premium emoji appear as</i> <code>{{e:id:🛒}}</code><i> — leave "
                 "those as they are and they'll render. To add a new one, just type "
                 "the emoji into your message.</i>"]
    body += ["", "<b>To change an emoji:</b> copy the text, delete the one you "
             "don't want, and type a premium emoji in its place. Then send it back.",
             "", "<i>Anything in</i> <code>{curly braces}</code> <i>is filled in "
             "automatically — leave those as they are.</i>"]

    await _show(c, "\n".join(body),
                k.kb([button] if button else [],
                     [k.btn("↩ Restore default", f"a:treset:{key}")],
                     [k.btn("✖ Cancel", "a:cancel")]))
    await c.answer()


@router.callback_query(F.data.startswith("a:treset:"))
async def text_reset(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 2)[2]
    await state.clear()
    await db.ex("DELETE FROM settings WHERE key = ?", (f"text:{key}",))
    section = texts.MESSAGES[key].section
    preview = await texts.t(key, **{f: "…" for f in texts.MESSAGES[key].fields})
    await _show(c,
                f"↩️ <b>{esc(texts.MESSAGES[key].label)}</b> restored to the default."
                f"\n\n<blockquote>{preview}</blockquote>",
                k.kb([k.btn(f"« {section}", f"a:tsec:{section}")],
                     [k.btn("« Messages", "a:texts")]))
    await c.answer("Restored")


@router.message(A.text_value)
async def text_save(m: Message, state: FSMContext):
    key = (await state.get_data())["text_key"]
    await state.clear()
    # tags an admin typed become real tags; premium emoji they typed become
    # tokens, so the next copy-paste round trip keeps them
    raw = (texts.to_source(texts.normalise_pasted(texts.restore_tags(m.html_text)))
           if m.text else "")
    section = texts.MESSAGES[key].section
    # en dash, em dash and the plain hyphen all mean "restore the default" —
    # phone keyboards rewrite hyphens and the distinction is invisible
    if raw.strip() in {"-", "–", "—", "reset", "default"}:
        await db.set_setting(f"text:{key}", texts.MESSAGES[key].default)
        await db.ex("DELETE FROM settings WHERE key = ?", (f"text:{key}",))
        note = "↩️ Restored the default."
    else:
        await db.set_setting(f"text:{key}", raw)
        note = "✅ Saved."
    preview = await texts.t(key, **{f: "…" for f in texts.MESSAGES[key].fields})
    await m.answer(f"{note}\n\n<blockquote>{preview}</blockquote>",
                   reply_markup=k.kb([k.btn(f"« {section}", f"a:tsec:{section}")],
                                     [k.btn("« Messages", "a:texts")]))


# ------------------------------------------------------------------ stats
@router.callback_query(F.data == "a:stats")
async def stats(c: CallbackQuery):
    s = await db.stats()
    low = await db.low_stock(cfg.low_stock)
    txt = (f"📊 <b>Statistics</b>\n\n"
           f"👥 Users: <b>{s['users']}</b> ({s['banned']} banned)\n"
           f"📦 Active products: <b>{s['products']}</b>\n"
           f"🗃 Stock units available: <b>{s['in_stock']}</b>\n\n"
           f"✅ Delivered orders: <b>{s['orders']}</b>\n"
           f"⏳ Open / awaiting review: <b>{s['pending']}</b>\n\n"
           f"💰 Revenue today: <b>{cfg.money(s['rev_today'])}</b>\n"
           f"💰 Revenue all time: <b>{cfg.money(s['rev_all'])}</b>")
    if low:
        txt += "\n\n⚠️ <b>Low stock</b>\n" + "\n".join(
            f"• {esc(r['name'])} — {r['c']}" for r in low)
    await _show(c, txt, k.back())
    await c.answer()


# ---------------------------------------------------------------- reviews
@router.callback_query(F.data == "a:reviews")
async def reviews(c: CallbackQuery):
    rows = list(await db.pending_reviews()) + list(await db.open_orders())
    if not rows:
        await _show(c, "🧾 Nothing awaiting review 🎉", k.back())
        return await c.answer()
    await _show(c, f"🔎 <b>{len(rows)} order(s) awaiting review</b>", k.back())
    for o in rows:
        await c.message.answer(
            f"#{o['id']} · {esc(o['product_name'])} ×{o['qty']}\n"
            f"{cfg.money(o['amount'])} via {o['provider']}\n"
            f"User <code>{o['user_id']}</code>\n"
            + (f"Ref: <code>{esc(o['external_ref'])}</code>" if o["external_ref"]
               else "<i>No reference submitted yet — waiting on the buyer.</i>"),
            reply_markup=k.review_kb(o["id"]))
    await c.answer()


@router.callback_query(F.data.startswith("a:ok:"))
async def approve(c: CallbackQuery, state: FSMContext):
    oid = int(c.data.split(":")[2])
    o = await db.order(oid)
    if o and not o["amount"]:
        # variable deposit — you decide what actually landed
        await state.set_state(A.approve_amount)
        await state.update_data(oid=oid)
        return await _show(c, f"How much did you receive for order #{oid}?\n"
                              f"Send the amount in {cfg.fiat}.", _cancel_kb())
    await _finish_approval(c.bot, oid)
    await _show(c, c.message.html_text + "\n\n✅ Approved", k.back())
    await c.answer()


async def _finish_approval(bot, oid: int) -> bool:
    await db.set_order(oid, status="pending")     # re-open so settle() accepts it
    o = await db.order(oid)
    return await delivery.settle(bot, oid, ref=o["external_ref"])


@router.message(A.approve_amount)
async def approve_amount(m: Message, state: FSMContext):
    try:
        amount = round(float((m.text or "").strip().replace(",", "")), 2)
        assert amount > 0
    except Exception:
        return await m.answer("Send a positive number.", reply_markup=_cancel_kb())
    oid = (await state.get_data())["oid"]
    await state.clear()
    await db.set_order(oid, amount=amount)
    ok = await _finish_approval(m.bot, oid)
    await m.answer(f"{'✅ Credited' if ok else '⚠️ Failed'} {cfg.money(amount)} "
                   f"for order #{oid}.", reply_markup=k.back())


@router.callback_query(F.data.startswith("a:no:"))
async def reject(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = await db.order(oid)
    await db.set_order(oid, status="rejected")
    await _show(c, c.message.html_text + "\n\n❌ Rejected", k.back())
    try:
        await c.bot.send_message(
            o["user_id"], f"❌ Order #{oid} was rejected — we couldn't match your payment. "
                          "Contact support if you believe this is a mistake.",
            reply_markup=k.home_kb())
    except Exception:
        pass
    await c.answer()


# -------------------------------------------------------- withdrawals
@router.callback_query(F.data == "a:wd")
async def withdrawals(c: CallbackQuery):
    rows = await db.pending_withdrawals()
    if not rows:
        await _show(c, "💸 No withdrawal requests waiting.", k.back())
        return await c.answer()
    await _show(c, f"💸 <b>{len(rows)} withdrawal(s) waiting</b>", k.back())
    for w in rows:
        u = await db.get_user(w["user_id"])
        await c.message.answer(
            f"#{w['id']} · <b>{cfg.money(w['amount'])}</b> via {esc(w['method'])}\n"
            f"User <code>{w['user_id']}</code> · balance {cfg.money(u['balance'])}\n"
            f"<code>{esc(w['address'])}</code>",
            reply_markup=k.kb([k.btn("✅ Mark paid", f"a:wdok:{w['id']}", style="success"),
                               k.btn("❌ Reject", f"a:wdno:{w['id']}", style="danger")]))
    await c.answer()


@router.callback_query(F.data.startswith("a:wdok:"))
async def wd_paid(c: CallbackQuery):
    wid = int(c.data.split(":")[2])
    w = await db.withdrawal(wid)
    if not w or w["status"] != "pending":
        return await c.answer("Already handled.", show_alert=True)
    u = await db.get_user(w["user_id"])
    if u["balance"] + 1e-9 < w["amount"]:
        return await c.answer("Their balance no longer covers this.", show_alert=True)
    # deduct only now, so a rejected request never touches the balance
    await db.add_balance(w["user_id"], -w["amount"])
    await db.set_withdrawal(wid, status="paid", processed_at=db.now())
    await _show(c, c.message.html_text + "\n\n✅ Paid", k.back())
    try:
        await c.bot.send_message(
            w["user_id"], f"✅ <b>Withdrawal #{wid} sent</b>\n\n"
                          f"{cfg.money(w['amount'])} via {esc(w['method'])}")
    except Exception:
        pass
    await c.answer()


@router.callback_query(F.data.startswith("a:wdno:"))
async def wd_reject(c: CallbackQuery):
    wid = int(c.data.split(":")[2])
    w = await db.withdrawal(wid)
    await db.set_withdrawal(wid, status="rejected", processed_at=db.now())
    await _show(c, c.message.html_text + "\n\n❌ Rejected", k.back())
    try:
        await c.bot.send_message(
            w["user_id"], f"❌ Withdrawal #{wid} was rejected. Your balance is unchanged — "
                          "contact support if you need details.")
    except Exception:
        pass
    await c.answer()


# ------------------------------------------------------------- categories
@router.callback_query(F.data == "a:cats")
async def cats(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show(c, "🗂 <b>Categories</b>\n\nTap one to manage its products.",
                k.admin_cats_kb(await db.categories()))
    await c.answer()


@router.callback_query(F.data == "a:catadd")
async def cat_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.cat_name)
    await _show(c, "Send the new category name:", _cancel_kb())
    await c.answer()


@router.message(A.cat_name)
async def cat_add_save(m: Message, state: FSMContext):
    await state.clear()
    try:
        await db.add_category(m.text.strip())
        await m.answer("✅ Category added.",
                       reply_markup=k.admin_cats_kb(await db.categories()))
    except Exception:
        await m.answer("That category already exists.", reply_markup=k.back("a:cats"))


@router.callback_query(F.data.startswith("a:catdel:"))
async def cat_del_confirm(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    cat = await db.category(cid)
    n = len(await db.products(cid, only_active=False))
    await _show(c, f"🗑 Delete <b>{esc(cat['name'])}</b>?\n\n"
                   f"This also removes {n} product(s) and all their stock.",
                k.confirm_kb(f"a:catdel2:{cid}", "a:cats"))
    await c.answer()


@router.callback_query(F.data.startswith("a:catdel2:"))
async def cat_del(c: CallbackQuery):
    await db.del_category(int(c.data.split(":")[2]))
    await _show(c, "🗑 Category deleted.", k.admin_cats_kb(await db.categories()))
    await c.answer()


@router.callback_query(F.data.startswith("a:cat:"))
async def cat_open(c: CallbackQuery):
    cid = int(c.data.split(":")[2])
    prods = await db.products(cid, only_active=False)
    cat = await db.category(cid)
    await _show(c, f"📦 Products in <b>{esc(cat['name'])}</b>",
                k.admin_prod_list_kb(prods, cid))
    await c.answer()


# --------------------------------------------------------------- products
@router.callback_query(F.data.startswith("a:prodadd:"))
async def prod_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.prod_name)
    await state.update_data(cid=int(c.data.split(":")[2]))
    await _show(c, "Step 1/3 — send the <b>product name</b>:", _cancel_kb())
    await c.answer()


@router.message(A.prod_name)
async def prod_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(A.prod_desc)
    await m.answer("Step 2/3 — send the <b>description</b> (or <code>-</code> to skip):",
                   reply_markup=_cancel_kb())


@router.message(A.prod_desc)
async def prod_desc(m: Message, state: FSMContext):
    await state.update_data(desc="" if m.text.strip() == "-" else m.text.strip())
    await state.set_state(A.prod_price)
    await m.answer(f"Step 3/3 — send the <b>price</b> in {cfg.fiat}:",
                   reply_markup=_cancel_kb())


@router.message(A.prod_price)
async def prod_price(m: Message, state: FSMContext):
    try:
        price = round(float(m.text.strip().replace(",", "")), 2)
    except ValueError:
        return await m.answer("Send a number, e.g. 249", reply_markup=_cancel_kb())
    d = await state.get_data()
    await state.clear()
    pid = await db.add_product(d["cid"], d["name"], d["desc"], price)
    p = await db.product(pid)
    await m.answer(f"✅ Created <b>{esc(p['name'])}</b>. Add stock next.\n\n"
                   + await _prod_text(p), reply_markup=k.admin_prod_kb(p))


@router.callback_query(F.data.startswith("a:prod:"))
async def prod_open(c: CallbackQuery, state: FSMContext):
    await state.clear()
    p = await db.product(int(c.data.split(":")[2]))
    await _show(c, await _prod_text(p), k.admin_prod_kb(p))
    await c.answer()


async def _prod_text(p) -> str:
    left = "♾ unlimited" if p["infinite"] else f"{await db.stock_count(p['id'])} available"
    mark = (p["emoji"] or "") + (" ✨" if p["icon_emoji_id"] else "")
    return (f"<b>{mark} {esc(p['name'])}</b>  (id {p['id']})\n\n"
            f"{esc(p['description']) or '<i>no description</i>'}\n\n"
            f"💲 {cfg.money(p['price'])}\n"
            f"📦 {left}\n"
            f"📈 sold: {p['sold_count']}\n"
            f"status: {'🟢 visible' if p['is_active'] else '🔴 hidden'}")


@router.callback_query(F.data.startswith("a:toggle:"))
async def prod_toggle(c: CallbackQuery):
    p = await db.product(int(c.data.split(":")[2]))
    await db.update_product(p["id"], is_active=0 if p["is_active"] else 1)
    p = await db.product(p["id"])
    await _show(c, await _prod_text(p), k.admin_prod_kb(p))
    await c.answer("Visibility updated.")


@router.callback_query(F.data.startswith("a:proddel:"))
async def prod_del_confirm(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    p = await db.product(pid)
    await _show(c, f"🗑 Delete <b>{esc(p['name'])}</b> and all of its stock?",
                k.confirm_kb(f"a:proddel2:{pid}", f"a:prod:{pid}"))
    await c.answer()


@router.callback_query(F.data.startswith("a:proddel2:"))
async def prod_del(c: CallbackQuery):
    p = await db.product(int(c.data.split(":")[2]))
    cid = p["category_id"]
    await db.del_product(p["id"])
    await _show(c, "🗑 Product deleted.",
                k.admin_prod_list_kb(await db.products(cid, only_active=False), cid))
    await c.answer()


@router.callback_query(F.data.startswith("a:edit:"))
async def prod_edit(c: CallbackQuery, state: FSMContext):
    _, _, field, pid = c.data.split(":")
    await state.set_state(A.edit_value)
    await state.update_data(field=field, pid=int(pid))
    hint = {
        "emoji": "Send one plain emoji to show before the product name (e.g. 🎬).",
        "icon_emoji_id": "Send the premium emoji itself, or paste its numeric id. "
                         "Use /ids if you need to look one up.",
        "unit": "What one unit is called — shows as <i>Price: ₹99 / code</i>. "
                "Send <code>-</code> to clear it.",
    }.get(field, f"Send the new <b>{field}</b>:")
    await _show(c, hint, _cancel_kb())
    await c.answer()


EDITABLE = {"name", "description", "price", "emoji", "icon_emoji_id", "unit"}


@router.message(A.edit_value)
async def prod_edit_save(m: Message, state: FSMContext):
    d = await state.get_data()
    if d["field"] not in EDITABLE:
        await state.clear()
        return await m.answer("That field can't be edited here.")
    value: object = m.text.strip()
    if d["field"] == "unit" and value == "-":
        value = ""
    if d["field"] == "icon_emoji_id":
        # accept either a pasted id or a forwarded premium emoji
        ids = [e.custom_emoji_id for e in (m.entities or []) if e.type == "custom_emoji"]
        value = ids[0] if ids else str(value)
        if value and not str(value).isdigit():
            return await m.answer("Send the numeric id, or just send the premium emoji "
                                  "itself.", reply_markup=_cancel_kb())
    if d["field"] == "price":
        try:
            value = round(float(str(value).replace(",", "")), 2)
        except ValueError:
            return await m.answer("Send a number.", reply_markup=_cancel_kb())
    await state.clear()
    await db.update_product(d["pid"], **{d["field"]: value})
    p = await db.product(d["pid"])
    await m.answer("✅ Updated.\n\n" + await _prod_text(p), reply_markup=k.admin_prod_kb(p))


@router.callback_query(F.data.startswith("a:infinite:"))
async def prod_infinite(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    p = await db.product(pid)
    if p["infinite"]:
        await db.update_product(pid, infinite=0)
        p = await db.product(pid)
        await _show(c, await _prod_text(p), k.admin_prod_kb(p))
        return await c.answer("Switched back to per-unit stock.")
    await state.set_state(A.stock_lines)
    await state.update_data(pid=pid, infinite=True)
    await _show(c, "♾ <b>Unlimited mode</b>\n\nSend the content every buyer should receive "
                   "(a download link, licence text, invite link…).", _cancel_kb())
    await c.answer()


# ------------------------------------------------------------------ stock
@router.callback_query(F.data.startswith("a:kw:"))
async def product_keywords(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[2])
    p = await db.product(pid)
    await state.set_state(A.prod_keywords)
    await state.update_data(pid=pid)
    current = (p["keywords"] or "").strip() or "—"
    await _show(c,
                f"🔎 <b>Group keywords — {esc(p['name'])}</b>\n\n"
                f"Now: <code>{esc(current)}</code>\n\n"
                "Extra words that should surface this product when someone "
                "mentions them in your group. Comma separated.\n"
                "e.g. <code>gemini, bard, google ai</code>\n\n"
                "The product name already matches on its own — this is for "
                "nicknames and misspellings. Send <code>-</code> to clear.",
                _cancel_kb())
    await c.answer()


@router.message(A.prod_keywords)
async def product_keywords_save(m: Message, state: FSMContext):
    pid = (await state.get_data())["pid"]
    await state.clear()
    raw = (m.text or "").strip()
    await db.update_product(pid, keywords="" if raw == "-" else raw)
    await m.answer("✅ Saved." if raw != "-" else "↩️ Cleared.",
                   reply_markup=k.back(f"a:prod:{pid}"))


@router.callback_query(F.data.startswith("a:tprice:"))
async def product_tier_prices(c: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = c.data.split(":")
    pid = int(parts[2])
    p = await db.product(pid)
    if len(parts) > 3:                      # a tier was picked
        await state.set_state(A.tier_price)
        await state.update_data(pid=pid, tid=int(parts[3]))
        t = await db.tier(int(parts[3]))
        return await _show(c, f"Price of <b>{esc(p['name'])}</b> for "
                              f"<b>{esc(t['name'])}</b>?\n"
                              f"List price is {cfg.money(p['price'])}.\n\n"
                              "Send a number, or <code>-</code> to fall back to the "
                              f"tier's −{t['discount']:g}%.", _cancel_kb())
    tiers = await db.tiers()
    if not tiers:
        return await c.answer("Create a price tier first (Panel → Price tiers).",
                              show_alert=True)
    over = await db.tier_prices(pid)
    rows = []
    for t in tiers:
        shown = (cfg.money(over[t["id"]]) if t["id"] in over
                 else f"{cfg.money(pricing.apply(p['price'], t['discount']))} (−{t['discount']:g}%)")
        rows.append([k.btn(f"{t['name']}: {shown}", f"a:tprice:{pid}:{t['id']}")])
    rows.append([k.btn("« Back", f"a:prod:{pid}")])
    await _show(c, f"💲 <b>Tier prices — {esc(p['name'])}</b>\n\n"
                   f"List price: <b>{cfg.money(p['price'])}</b>\n\n"
                   "An exact price here overrides that tier's percentage.", k.kb(*rows))
    await c.answer()


@router.message(A.tier_price)
async def tier_price_save(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    raw = (m.text or "").strip()
    if raw == "-":
        await db.set_tier_price(d["pid"], d["tid"], None)
        note = "↩️ Back to the tier's percentage."
    else:
        try:
            await db.set_tier_price(d["pid"], d["tid"], round(float(raw), 2))
            note = "✅ Saved."
        except ValueError:
            return await m.answer("Send a number, or <code>-</code> to clear.",
                                  reply_markup=_cancel_kb())
    await m.answer(note, reply_markup=k.back(f"a:tprice:{d['pid']}"))


@router.callback_query(F.data.startswith("a:stockadd:"))
async def stock_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.stock_lines)
    await state.update_data(pid=int(c.data.split(":")[2]), infinite=False)
    await _show(c, "➕ <b>Add stock</b>\n\nSend the items — <b>one per line</b> — "
                   "or upload a .txt file.\nEach line goes to exactly one buyer.",
                _cancel_kb())
    await c.answer()


@router.message(A.stock_lines, F.document)
async def stock_file(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    f = await bot.get_file(m.document.file_id)
    buf = await bot.download_file(f.file_path)
    n = await db.add_stock(d["pid"], buf.read().decode("utf-8", errors="ignore").splitlines())
    p = await db.product(d["pid"])
    await m.answer(f"✅ Added {n} item(s).\n\n" + await _prod_text(p),
                   reply_markup=k.admin_prod_kb(p))


@router.message(A.stock_lines)
async def stock_text(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    if d.get("infinite"):
        await db.update_product(d["pid"], infinite=1, static_payload=m.text)
        p = await db.product(d["pid"])
        return await m.answer("♾ Unlimited mode on.\n\n" + await _prod_text(p),
                              reply_markup=k.admin_prod_kb(p))
    n = await db.add_stock(d["pid"], (m.text or "").splitlines())
    p = await db.product(d["pid"])
    await m.answer(f"✅ Added {n} item(s).\n\n" + await _prod_text(p),
                   reply_markup=k.admin_prod_kb(p))


@router.callback_query(F.data.startswith("a:stockview:"))
async def stock_view(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    rows = await db.q(
        "SELECT payload FROM stock WHERE product_id = ? AND is_sold = 0 ORDER BY id", (pid,))
    if not rows:
        return await c.answer("No unsold stock.", show_alert=True)
    body = "\n".join(r["payload"] for r in rows)
    await c.message.answer_document(
        BufferedInputFile(body.encode(), filename=f"stock_{pid}.txt"),
        caption=f"{len(rows)} unsold item(s).",
        reply_markup=k.back(f"a:prod:{pid}"))
    await c.answer()


@router.callback_query(F.data.startswith("a:purge:"))
async def purge(c: CallbackQuery):
    n = await db.purge_sold(int(c.data.split(":")[2]))
    await c.answer(f"Removed {n} sold row(s).", show_alert=True)


# ------------------------------------------------------------------ users
@router.callback_query(F.data == "a:users")
async def users(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.find_user)
    await _show(c, "👤 <b>Find a user</b>\n\nSend a numeric ID or @username:", _cancel_kb())
    await c.answer()


@router.message(A.find_user)
async def user_found(m: Message, state: FSMContext):
    u = await db.find_user(m.text)
    if not u:
        return await m.answer("No such user. Try again:", reply_markup=_cancel_kb())
    await state.clear()
    await m.answer(await _user_text(u), reply_markup=k.user_kb(u))


async def _user_text(u) -> str:
    n = await db.q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders "
                    "WHERE user_id = ? AND status = 'delivered'", (u["tg_id"],))
    t = await db.tier(u["tier_id"])
    tier_line = (f"🏷 Pricing: <b>{esc(t['name'])}</b> (−{t['discount']:g}%)"
                 if t else "🏷 Pricing: <b>Standard</b>")
    return (f"👤 <b>{esc(u['first_name'] or '')}</b> @{esc(u['username'] or '—')}\n"
            f"ID: <code>{u['tg_id']}</code>\n"
            f"👛 Balance: <b>{cfg.money(u['balance'])}</b>\n"
            f"🧾 Orders: {n['c']} · spent {cfg.money(n['s'])}\n"
            f"{tier_line}\n"
            f"Status: {'🚫 banned' if u['is_banned'] else '✅ active'}\n"
            f"Joined: {u['created_at']}")


@router.callback_query(F.data.startswith("a:ban:"))
async def ban(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    u = await db.get_user(uid)
    await db.set_ban(uid, not u["is_banned"])
    u = await db.get_user(uid)
    await _show(c, await _user_text(u), k.user_kb(u))
    await c.answer("Banned." if u["is_banned"] else "Unbanned.")


@router.callback_query(F.data.startswith("a:utier:"))
async def user_tier(c: CallbackQuery):
    parts = c.data.split(":")
    uid = int(parts[2])
    if len(parts) > 3:
        tid = int(parts[3]) or None
        await db.set_user_tier(uid, tid)
        u = await db.get_user(uid)
        await _show(c, await _user_text(u), k.user_kb(u))
        return await c.answer("Pricing updated.")
    rows = [[k.btn("Standard (list price)", f"a:utier:{uid}:0")]]
    for t in await db.tiers():
        rows.append([k.btn(f"{t['name']} · −{t['discount']:g}%", f"a:utier:{uid}:{t['id']}")])
    rows.append([k.btn("👤 Prices just for this user", f"a:upersonal:{uid}")])
    rows.append([k.btn("« Back", f"a:uback:{uid}")])
    await _show(c, "🏷 <b>Pricing for this user</b>\n\n"
                   "Put them in a shared tier, or give them their own price list.",
                k.kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("a:upersonal:"))
async def user_personal_prices(c: CallbackQuery, state: FSMContext):
    """Give one buyer their own price list, without affecting anyone else."""
    await state.clear()
    uid = int(c.data.split(":")[2])
    tid = await db.personal_tier(uid)
    await c.answer("Personal price list ready.")
    c2 = c.model_copy(update={"data": f"a:tprices:{tid}"}).as_(c.bot)
    await tier_price_list(c2, state)


@router.callback_query(F.data.startswith("a:uback:"))
async def user_back(c: CallbackQuery):
    u = await db.get_user(int(c.data.split(":")[2]))
    await _show(c, await _user_text(u), k.user_kb(u))
    await c.answer()


@router.callback_query(F.data.startswith("a:bal:"))
async def bal(c: CallbackQuery, state: FSMContext):
    _, _, uid, op = c.data.split(":")
    await state.set_state(A.bal_amount)
    await state.update_data(uid=int(uid), op=op)
    await _show(c, f"Amount to {'add' if op == 'add' else 'deduct'} ({cfg.fiat}):",
                _cancel_kb())
    await c.answer()


@router.message(A.bal_amount)
async def bal_save(m: Message, state: FSMContext):
    d = await state.get_data()
    try:
        amt = round(float(m.text.strip()), 2)
    except ValueError:
        return await m.answer("Send a number.", reply_markup=_cancel_kb())
    await state.clear()
    delta = amt if d["op"] == "add" else -amt
    await db.add_balance(d["uid"], delta)
    u = await db.get_user(d["uid"])
    await m.answer(await _user_text(u), reply_markup=k.user_kb(u))
    try:
        await m.bot.send_message(
            d["uid"], f"👛 Your balance was {'credited' if delta > 0 else 'debited'} "
                      f"{cfg.money(abs(delta))}.\nNew balance: <b>{cfg.money(u['balance'])}</b>",
            reply_markup=k.home_kb())
    except Exception:
        pass


@router.callback_query(F.data.startswith("a:uorders:"))
async def user_orders(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    rows = await db.user_orders(uid, 25)
    if not rows:
        return await c.answer("No orders.", show_alert=True)
    txt = "\n".join(f"#{o['id']} {esc(o['product_name'])} ×{o['qty']} · "
                    f"{cfg.money(o['amount'])} · {o['status']}" for o in rows)
    await _show(c, f"🧾 <b>Orders for</b> <code>{uid}</code>\n\n{txt}",
                k.back(f"a:home"))
    await c.answer()


# -------------------------------------------------------------- broadcast
@router.callback_query(F.data == "a:bc")
async def bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.broadcast)
    n = len(await db.all_user_ids(promos_only=True))
    await _show(c, f"📣 <b>Broadcast</b>\n\nGoes to {n} users who allow announcements. "
                   "HTML formatting is preserved.", _cancel_kb())
    await c.answer()


@router.message(A.broadcast)
async def bc_send(m: Message, state: FSMContext):
    await state.clear()
    ids = await db.all_user_ids(promos_only=True)
    sent = failed = 0
    status = await m.answer(f"📣 Sending to {len(ids)} users…")
    for i, uid in enumerate(ids, 1):
        try:
            await m.bot.send_message(uid, m.html_text, reply_markup=k.home_kb())
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)          # ~20 msg/s, within Telegram limits
        if i % 100 == 0:
            await status.edit_text(f"📣 {i}/{len(ids)}…")
    await status.edit_text(f"📣 Done — delivered {sent}, failed {failed}.",
                           reply_markup=k.back())


# --------------------------------------------------------------- settings
@router.callback_query(F.data == "a:settings")
async def settings(c: CallbackQuery):
    on = await db.setting("admin_sale_alerts", "0") == "1"
    await _show(c,
                "⚙️ <b>Settings</b>\n\n"
                f"🔔 Sale alerts to admins: <b>{'on' if on else 'off'}</b>\n"
                "<i>Reviews, withdrawals and low stock always alert — those need "
                "you. This is only the message per completed sale.</i>",
                k.settings_kb(on))
    await c.answer()


@router.callback_query(F.data == "a:salealerts")
async def toggle_sale_alerts(c: CallbackQuery):
    on = await db.setting("admin_sale_alerts", "0") == "1"
    await db.set_setting("admin_sale_alerts", "0" if on else "1")
    await settings(c)
    await c.answer("Sale alerts " + ("off" if on else "on"))


@router.callback_query(F.data == "a:rails")
async def rails(c: CallbackQuery, state: FSMContext):
    """Shows every enabled rail and, when one is hidden, exactly why."""
    await state.clear()
    import payments
    await payments.reload_rails()

    live = [r for r in payments.status() if r["ready"]]
    broken = [r for r in payments.status() if not r["ready"]]

    body = [f"🏦 <b>Payment rails</b>\n",
            f"Buyers can see <b>{len(live)}</b> of {len(payments.status())} enabled rails."]
    if live:
        body.append("\n✅ <b>Live</b>")
        body += [f"• {r['title']}" + ("" if r["auto"] else " <i>(manual review)</i>")
                 for r in live]
    if broken:
        body.append("\n⚠️ <b>Hidden — not configured</b>")
        body += [f"• {r['title']} — needs {r['need']}" for r in broken]
        body.append("\n<i>A rail with nowhere to send money is hidden rather than "
                    "shown broken. Fill in what's listed and restart.</i>")

    rows = []
    for code, spec in payments.MANUAL_RAILS.items():
        if code not in cfg.providers:
            continue
        val = payments.RAIL_ACCOUNTS.get(code) or "— not set"
        rows.append([k.btn(f"{spec['title']}: {val}", f"a:rail:{code}")])
    rows.append([k.btn("« Back", "a:settings")])
    await _show(c, "\n".join(body), k.kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("a:rail:"))
async def rail_edit(c: CallbackQuery, state: FSMContext):
    import payments
    code = c.data.split(":")[2]
    if code not in cfg.providers:
        return await c.answer("That rail isn't enabled — add it to "
                              "ENABLED_PROVIDERS first.", show_alert=True)
    spec = payments.MANUAL_RAILS[code]
    await state.set_state(A.rail_value)
    await state.update_data(rail=code)
    await _show(c, f"Send the <b>{spec['label']}</b> for {spec['title']}.\n"
                   "Send <code>-</code> to clear it.", _cancel_kb())
    await c.answer()


@router.message(A.rail_value)
async def rail_save(m: Message, state: FSMContext):
    import payments
    code = (await state.get_data())["rail"]
    await state.clear()
    value = "" if m.text.strip() == "-" else m.text.strip()
    await db.set_setting(f"rail:{code}", value)
    await payments.reload_rails()
    await m.answer("✅ Saved.", reply_markup=k.back("a:rails"))


@router.callback_query(F.data.startswith("a:set:"))
async def setting_edit(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.set_state(A.setting_value)
    await state.update_data(key=key)
    current = await db.setting(key, "<i>not set</i>")
    await _show(c, f"Current <b>{key}</b>:\n\n{current}\n\nSend the new text:", _cancel_kb())
    await c.answer()


@router.message(A.setting_value)
async def setting_save(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    await db.set_setting(d["key"], m.html_text)
    await m.answer("✅ Saved.", reply_markup=k.settings_kb())
