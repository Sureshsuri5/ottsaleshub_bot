from __future__ import annotations

from aiogram.types import (CopyTextButton, InlineKeyboardButton,
                           InlineKeyboardMarkup, WebAppInfo)

import flair
import payments
from config import cfg


def btn(text: str, data: str, style: str | None = None,
        icon_slot: str | None = None) -> InlineKeyboardButton:
    """style: 'primary' (blue) | 'success' (green) | 'danger' (red) | None (neutral).
    icon_slot: a flair slot whose custom emoji is drawn before the label."""
    return InlineKeyboardButton(text=text, callback_data=data, style=style,
                                icon_custom_emoji_id=flair.icon(icon_slot))


def url_btn(text: str, url: str, style: str | None = None,
            icon_slot: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url, style=style,
                                icon_custom_emoji_id=flair.icon(icon_slot))


def app_btn(text: str, path: str = "", style: str | None = "primary",
            icon_slot: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=cfg.webapp_url + path),
                                style=style, icon_custom_emoji_id=flair.icon(icon_slot))


COPY_LIMIT = 256      # Telegram's hard cap on CopyTextButton payloads


def copy_btn(text: str, payload: str) -> InlineKeyboardButton | None:
    """Telegram's native copy button. Returns None when the payload is over the
    limit — the caller shows a tap-to-copy code block instead."""
    if len(payload) > COPY_LIMIT:
        return None
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=payload))


def kb(*rows) -> InlineKeyboardMarkup:
    # drop empty rows, and any button a helper declined to build
    cleaned = [[b for b in row if b is not None] for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=[r for r in cleaned if r])


def back_btn(label: str, data: str) -> InlineKeyboardButton:
    """Any navigation button. Routing them all through the `back` flair slot
    means setting that icon once styles every Back in the bot."""
    return btn(flair.label("back", label), data, icon_slot="back")


def HOME() -> list[InlineKeyboardButton]:
    """A function, not a constant: a module-level list would be built once at
    import and freeze whatever icon state existed then."""
    return [back_btn("Menu", "home")]


def home_kb() -> InlineKeyboardMarkup:
    return kb(HOME())


# ------------------------------------------------------------- main menu
def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    """Home screen. Shop is the one highlighted action; everything else is quiet.
    Each button reads its icon from its own flair slot."""
    L = flair.label
    rows = []
    if cfg.miniapps_live:
        rows.append([app_btn(L("menu_app", "Open Mini App"), "/", style=None,
                             icon_slot="menu_app")])
    rows.append([btn(L("menu_shop", "Shop"), "shop", style="primary", icon_slot="menu_shop")])
    rows.append([btn(L("menu_deposit", "Deposit"), "menu:balance", icon_slot="menu_deposit"),
                 btn(L("menu_profile", "My Profile"), "menu:profile", icon_slot="menu_profile")])
    rows.append([btn(L("menu_support", "Support"), "menu:support", icon_slot="menu_support")])
    rows.append([btn(L("menu_refer", "Refer & Earn"), "menu:refer", icon_slot="menu_refer")])
    if is_admin:
        rows.append([app_btn(L("menu_admin", "Admin panel"), "/admin", style="primary",
                             icon_slot="menu_admin")]
                    if cfg.miniapps_live
                    else [btn(L("menu_admin", "Admin panel"), "a:home", style="primary",
                              icon_slot="menu_admin")])
    return kb(*rows)


def profile_kb() -> InlineKeyboardMarkup:
    rows = [
        [btn(flair.label("p_stats", "My Stats"), "pf:stats", icon_slot="p_stats")],
        [btn(flair.label("p_notify", "Notifications"), "pf:notify", icon_slot="p_notify")],
        [btn(flair.label("p_orders", "My Orders"), "menu:orders", icon_slot="p_orders"),
         btn(flair.label("p_withdraw", "Withdraw"), "pf:withdraw", icon_slot="p_withdraw")],
    ]
    if cfg.api_enabled:
        rows.append([btn(flair.label("p_api", "Developer API"), "pf:api",
                         icon_slot="p_api")])
    rows.append([back_btn("Back", "home")])
    return kb(*rows)


def notify_kb(u) -> InlineKeyboardMarkup:
    def row(field, label):
        on = bool(u[field])
        return [btn(f"{'🔔' if on else '🔕'} {label}: {'On' if on else 'Off'}",
                    f"pf:toggle:{field}", style="success" if on else None)]
    return kb(row("notify_orders", "Order updates"),
              row("notify_promos", "Announcements"),
              [back_btn("Back", "menu:profile")])


def withdraw_kb(methods, can: bool) -> InlineKeyboardMarkup:
    rows = [[btn(m, f"wd:{i}")] for i, m in enumerate(methods)] if can else []
    rows.append([btn(flair.label("p_requests", "My requests"), "pf:wdlist", icon_slot="p_requests"), back_btn("Back", "menu:profile")])
    return kb(*rows)


def api_kb(has_key: bool) -> InlineKeyboardMarkup:
    return kb(
        [btn("♻️ Rotate key" if has_key else "🔑 Generate key", "pf:apikey")],
        [back_btn("Back", "menu:profile")],
    )


def refer_kb(link: str, can_transfer: bool) -> InlineKeyboardMarkup:
    rows = [
        # Telegram's own copy button — one tap, no round trip to the bot
        [InlineKeyboardButton(text=flair.label("r_copy", "Copy Referral Link"),
                              copy_text=CopyTextButton(text=link), style="primary",
                              icon_custom_emoji_id=flair.icon("r_copy"))],
    ]
    # both states use the same slot — the disabled variant is still the same
    # button to the buyer, it just isn't tappable yet
    rows.append([btn(flair.label("r_transfer", "Transfer to Wallet"),
                     "refer:transfer" if can_transfer else "refer:none",
                     style="success" if can_transfer else None,
                     icon_slot="r_transfer")])
    rows.append([back_btn("Back", "home")])
    return kb(*rows)


def community_kb() -> InlineKeyboardMarkup:
    row = []
    if cfg.channel_url:
        row.append(url_btn("📢 Channel", cfg.channel_url))
    if cfg.group_url:
        row.append(url_btn("💬 Group", cfg.group_url))
    return kb(row) if row else None


def cancel_kb() -> InlineKeyboardMarkup:
    """Shown under any prompt that waits for typed input."""
    return kb([btn(flair.label("cancel", "Cancel"), "fsmcancel", icon_slot="cancel")])


# ---------------------------------------------------------------- browse
PAGE_SIZE = 30


def shop_kb(prods, counts: dict[int, int], page: int = 0,
            prices: dict[int, float] | None = None) -> InlineKeyboardMarkup:
    """One flat list: icon + name + how many are left.

    Colour carries availability (green buyable / red sold out); the count in
    brackets is the same signal in a form you can compare across rows, and
    matters for anyone who can't distinguish the two colours.
    """
    pages = max(1, (len(prods) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    window = prods[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    rows = []
    for p in window:
        left = counts.get(p["id"], 0)
        in_stock = bool(p["infinite"]) or left > 0
        count = "∞" if p["infinite"] else left
        emoji = (p["emoji"] or "").strip()
        # a custom emoji renders as its own icon slot, so don't also inline it —
        # but if icons are off, the plain emoji has to come back into the label
        icon_id = flair.icon_id(p["icon_emoji_id"])
        label = f"{emoji + ' ' if emoji and not icon_id else ''}{p['name']} ({count})"
        _ = prices  # prices are shown on the product screen, not in the list
        rows.append([InlineKeyboardButton(
            text=label, callback_data=f"prod:{p['id']}",
            style="success" if in_stock else "danger",
            icon_custom_emoji_id=icon_id)])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn(flair.label("page_prev", "Prev"), f"shop:{page - 1}", icon_slot="page_prev"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn(flair.label("page_next", "Next"), f"shop:{page + 1}", icon_slot="page_next"))
        rows.append(nav)

    rows.append([btn(flair.label("refresh", "Refresh Stock"), f"shop:{page}",
                     icon_slot="refresh")])
    rows.append([back_btn("Back", "home")])
    return kb(*rows)


def product_kb(pid: int, in_stock: bool) -> InlineKeyboardMarkup:
    """Two buttons only. Quantity moves to the payment step, where the running
    total is already on screen and the choice actually costs something."""
    rows = []
    if in_stock:
        rows.append([btn(flair.label("buy", "Buy Now"), f"buy:{pid}", style="primary",
                         icon_slot="buy")])
    rows.append([back_btn("Back to Store", "shop")])
    return kb(*rows)


QTY_PRESETS = (1, 2, 3, 5, 10, 15, 20, 25)


def qty_kb(pid: int, avail: int) -> InlineKeyboardMarkup:
    """Preset quantities, capped by what's actually in stock."""
    opts = [n for n in QTY_PRESETS if n <= avail] or [1]
    # when the last few units fall between presets, offer the exact remainder —
    # otherwise "4 left" can only be bought as 3 without typing a number
    if avail < QTY_PRESETS[-1] and avail not in opts:
        opts.append(avail)
        opts.sort()
    rows = [[btn(str(n), f"q:{pid}:{n}") for n in opts[i:i + 4]]
            for i in range(0, len(opts), 4)]
    if avail > 1:
        rows.append([btn(flair.label("qty_custom", "Custom Amount"), f"qcustom:{pid}",
                         icon_slot="qty_custom")])
    rows.append([btn(flair.label("back", "Back to Product"), f"prod:{pid}",
                     icon_slot="back")])
    return kb(*rows)


GROUP_LABELS = {"direct": "💳 Pay Directly"}


def providers_kb(pid: int, qty: int, kind: str = "purchase",
                 balance: float | None = None, total: float | None = None
                 ) -> InlineKeyboardMarkup:
    """Order summary payment list.

    A group with several rails collapses into one entry (Pay Directly →
    submenu); a group with one rail is shown inline, because a submenu holding
    a single option is just an extra tap.
    """
    rows = []
    for name, provs in payments.groups().items():
        provs = [p for p in provs if not (kind == "topup" and p.code == "balance")]
        if not provs:
            continue
        if len(provs) > 1 and name in GROUP_LABELS:
            rows.append([btn(flair.label("pay", "Pay Directly"),
                             f"pgroup:{kind}:{pid}:{qty}:{name}", icon_slot="pay")])
            continue
        for p in provs:
            # the wallet button carries the figure, so nobody taps it to be told no
            if p.code == "balance" and balance is not None:
                enough = total is None or balance + 1e-9 >= total
                # through flair.label so the title's own 👛 is dropped when an
                # icon is set — otherwise the button carries two marks
                label = (f"{flair.label('pay_balance', p.title)} "
                         f"({cfg.money(balance)})")
                rows.append([btn(
                    label if enough else f"{label} — not enough",
                    f"pay:{kind}:{pid}:{qty}:balance" if enough else f"needfunds:{pid}",
                    style="success" if enough else None,
                    icon_slot="pay_balance")])
                continue
            rows.append([btn(flair.label(f"pay_{p.code}", p.title),
                         f"pay:{kind}:{pid}:{qty}:{p.code}", icon_slot=f"pay_{p.code}")])
    if kind == "purchase":
        rows.append([back_btn("Back", f"buy:{pid}"),
                     btn(flair.label("cancel_order", "Cancel Order"), "cancelbuy",
                         style="danger", icon_slot="cancel_order")])
    else:
        rows.append([back_btn("Back", "menu:balance")])
    return kb(*rows)


def rails_kb(pid: int, qty: int, kind: str, group: str) -> InlineKeyboardMarkup:
    rows = [[btn(flair.label(f"pay_{p.code}", p.title), f"pay:{kind}:{pid}:{qty}:{p.code}",
                 icon_slot=f"pay_{p.code}")]
            for p in payments.groups().get(group, [])]
    rows.append([back_btn("Back", f"q:{pid}:{qty}" if kind == "purchase"
                          else "menu:balance")])
    return kb(*rows)


def invoice_kb(oid: int, manual: bool, awaiting_ref: bool = False,
               back: str = "home", pay_url: str | None = None) -> InlineKeyboardMarkup:
    """When the bot is already listening for a pasted reference, the only thing
    worth offering is a way out — another button competes with the text input."""
    if awaiting_ref:
        return kb([back_btn("Back", back)])
    rows = []
    if pay_url:
        rows.append([url_btn(flair.label("pay_now", "Pay now"), pay_url,
                             style="success", icon_slot="pay_now")])
    return kb(
        *rows,
        [btn(flair.label("paid", "I've paid") if manual
             else flair.label("check", "Check payment"), f"chk:{oid}",
             style="success" if not pay_url else None,
             icon_slot="paid" if manual else "check")],
        [btn(flair.label("cancel", "Cancel order"), f"cancel:{oid}", style="danger",
             icon_slot="cancel"), back_btn("Menu", "home")],
    )


# --------------------------------------------------------------- account
TOPUP_PRESETS = (100, 250, 500, 1000)


def deposit_kb() -> InlineKeyboardMarkup:
    """Straight to the rails. Anything that takes an arbitrary amount goes to
    its address immediately; the rest ask for a figure first."""
    rows = []
    for p in payments.deposit_rails():
        target = f"dep:{p.code}" if payments.is_variable(p.code) else f"depamt:{p.code}"
        # same per-rail slot the checkout screens use, so one icon covers both
        rows.append([btn(flair.label(f"pay_{p.code}", p.title), target,
                         icon_slot=f"pay_{p.code}")])
    rows.append(HOME())
    return kb(*rows)


def deposit_amount_kb(code: str, unit: str = "", rate: float = 1.0,
                      symbol: str = "") -> InlineKeyboardMarkup:
    """Presets in whatever currency this rail actually moves.

    Asking an Indian buyer to pick "$100" when they'll be paying rupees makes
    them do the conversion in their head — so offer round rupee amounts and
    convert on the way back.
    """
    if unit and unit.upper() != cfg.fiat.upper() and rate > 0:
        # round figures in the paying currency, not converted odd ones
        native = [500, 1000, 2000, 5000] if rate > 20 else [10, 25, 50, 100]
        presets = [btn(f"{symbol or ''}{a:,}".strip(),
                       f"top:{int(round(a / rate * 100))}:{code}",
                       style="primary", icon_slot="money") for a in native]
    else:
        presets = [btn(cfg.money(a), f"top:{int(a * 100)}:{code}", style="primary",
                       icon_slot="money") for a in TOPUP_PRESETS]
    return kb(presets[:2], presets[2:],
              [btn(flair.label("qty_custom", "Custom amount"), f"topup:{code}",
                   icon_slot="qty_custom")],
              [back_btn("Back", "menu:balance")])


ORDERS_PAGE = 5


def orders_kb(rows, page: int = 0, total: int = 0) -> InlineKeyboardMarkup:
    """One row per order: reference code, product, quantity."""
    out = [[btn(f"#{o['code'] or o['id']} • {o['product_name'][:26]} x{o['qty']}",
                f"ord:{o['id']}")] for o in rows]
    nav = []
    if page > 0:
        nav.append(btn(flair.label("page_prev", "Prev"), f"orders:{page - 1}", icon_slot="page_prev"))
    if (page + 1) * ORDERS_PAGE < total:
        nav.append(btn(flair.label("page_next", "Next"), f"orders:{page + 1}", icon_slot="page_next"))
    if nav:
        out.append(nav)
    out.append([btn(flair.label("back", "Back"), "home", icon_slot="back")])
    return kb(*out)


def order_kb(oid: int | None = None, has_items: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if oid and has_items:
        rows.append([btn(flair.label("dl_txt", "TXT Download"), f"dl:txt:{oid}", icon_slot="dl_txt"),
                     btn(flair.label("dl_csv", "CSV Download"), f"dl:csv:{oid}", icon_slot="dl_csv")])
    rows.append([back_btn("Back", "menu:orders")])
    return kb(*rows)


def support_kb() -> InlineKeyboardMarkup:
    rows = []
    if cfg.support_url:
        rows.append([url_btn(flair.label("sup_button", "Contact Support"),
                             cfg.support_url, icon_slot="sup_button")])
    rows.append([back_btn("Back", "home")])
    return kb(*rows)


# ------------------------------------------------------------------ admin
def admin_menu(reviews: int = 0, withdrawals: int = 0) -> InlineKeyboardMarkup:
    """Counts sit on the buttons so pending work is visible without opening
    anything, and turn red when something is actually waiting."""
    rv = f"Reviews ({reviews})" if reviews else "Reviews"
    wd = f"Withdrawals ({withdrawals})" if withdrawals else "Withdrawals"
    return kb(
        [btn(flair.label("a_stats", "Stats"), "a:stats", icon_slot="a_stats"),
         btn(flair.label("a_reviews", rv), "a:reviews", icon_slot="a_reviews",
             style="danger" if reviews else None)],
        [btn(flair.label("a_withdrawals", wd), "a:wd", icon_slot="a_withdrawals",
             style="danger" if withdrawals else None)],
        [btn(flair.label("a_cats", "Categories"), "a:cats", icon_slot="a_cats"),
         btn(flair.label("box", "Products"), "a:cats", icon_slot="box")],
        [btn(flair.label("a_users", "Users"), "a:users", icon_slot="a_users"),
         btn(flair.label("a_broadcast", "Broadcast"), "a:bc", icon_slot="a_broadcast")],
        [btn(flair.label("a_settings", "Settings"), "a:settings", icon_slot="a_settings"),
         btn("🏷 Price tiers", "a:tiers")],
        [back_btn("Exit to shop", "home")],
    )


def back_to(data: str) -> InlineKeyboardMarkup:
    return kb([back_btn("Back", data)])


def back(data: str = "a:home") -> InlineKeyboardMarkup:
    return kb([back_btn("Back", data)])


def admin_cats_kb(cats) -> InlineKeyboardMarkup:
    rows = [[btn(c["name"], f"a:cat:{c['id']}"),
             btn("🗑", f"a:catdel:{c['id']}", style="danger")] for c in cats]
    rows.append([btn("➕ Add category", "a:catadd")])
    rows.append([back_btn("Back", "a:home")])
    return kb(*rows)


def admin_prod_list_kb(prods, cid: int) -> InlineKeyboardMarkup:
    rows = [[btn(f"{'🟢' if p['is_active'] else '🔴'} {p['name']}", f"a:prod:{p['id']}")]
            for p in prods]
    rows.append([btn("➕ Add product", f"a:prodadd:{cid}")])
    rows.append([back_btn("Categories", "a:cats"), back_btn("Panel", "a:home")])
    return kb(*rows)


def admin_prod_kb(p) -> InlineKeyboardMarkup:
    return kb(
        [btn("✏️ Name", f"a:edit:name:{p['id']}"),
         btn("💬 Description", f"a:edit:description:{p['id']}")],
        [btn("💲 Price", f"a:edit:price:{p['id']}"),
         btn("🔴 Hide" if p["is_active"] else "🟢 Show", f"a:toggle:{p['id']}")],
        [btn("😀 Emoji", f"a:edit:emoji:{p['id']}"),
         btn("✨ Custom icon", f"a:edit:icon_emoji_id:{p['id']}")],
        [btn("🏷 Price unit", f"a:edit:unit:{p['id']}"),
         btn("💲 Tier prices", f"a:tprice:{p['id']}")],
        [btn("🔎 Group keywords", f"a:kw:{p['id']}")],
        [btn("➕ Add stock", f"a:stockadd:{p['id']}", style="success", icon_slot="box"),
         btn("📋 Export stock", f"a:stockview:{p['id']}")],
        [btn("♾ Unlimited mode", f"a:infinite:{p['id']}"),
         btn("🧹 Purge sold", f"a:purge:{p['id']}")],
        [btn("🗑 Delete product", f"a:proddel:{p['id']}", style="danger")],
        [back_btn("Back", f"a:cat:{p['category_id']}"), back_btn("Panel", "a:home")],
    )


def confirm_kb(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return kb([btn("✅ Yes, delete", yes_data, style="danger"), btn("« Cancel", no_data)])


def review_kb(oid: int) -> InlineKeyboardMarkup:
    return kb([btn("✅ Approve", f"a:ok:{oid}", style="success", icon_slot="ok"),
               btn("❌ Reject", f"a:no:{oid}", style="danger")])


def user_kb(u) -> InlineKeyboardMarkup:
    return kb(
        [btn("➕ Add balance", f"a:bal:{u['tg_id']}:add", style="success", icon_slot="money"),
         btn("➖ Deduct", f"a:bal:{u['tg_id']}:sub")],
        [btn("🔓 Unban" if u["is_banned"] else "🚫 Ban", f"a:ban:{u['tg_id']}",
             style=None if u["is_banned"] else "danger")],
        [btn("🧾 Orders", f"a:uorders:{u['tg_id']}"),
         btn("🏷 Pricing", f"a:utier:{u['tg_id']}")],
        [btn("🔍 Find another", "a:users"), back_btn("Panel", "a:home")],
    )


def settings_kb(sale_alerts: bool = False) -> InlineKeyboardMarkup:
    return kb(
        [btn(f"🔔 Sale alerts: {'On' if sale_alerts else 'Off'}", "a:salealerts",
             style="success" if sale_alerts else None)],
        [btn("✏️ Bot messages", "a:texts")],
        [btn("🖼 Storefront logo", "a:set:shop_logo")],
        [btn("🎨 Button icons", "a:flair")],
        [btn("🏦 Payment accounts", "a:rails")],
        [back_btn("Back", "a:home")],
    )
