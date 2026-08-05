"""Every editable bot message, in one registry.

`t(key, **values)` returns the admin's version if they've set one, otherwise the
default below. Anything listed here shows up in the admin panel automatically —
adding a message means adding one row, not touching the UI.

Placeholders use {braces}. A missing or misspelt one renders as-is instead of
raising, so a typo in the panel can never take a screen down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import db
import flair


@dataclass(frozen=True)
class Msg:
    default: str
    label: str
    section: str
    fields: tuple[str, ...] = ()


MESSAGES: dict[str, Msg] = {
    # ---------------------------------------------------------- welcome
    # The icons here are {{slot}} references, so setting a button's premium
    # emoji in /flair updates the welcome text at the same time — one place to
    # change an icon rather than two.
    "welcome": Msg(
        "{{w_title}} <b>Welcome to {shop}!</b>\n\n"
        "Hey <b>{name}</b>! {{w_wave}}\n\n"
        "We offer premium digital products at the best prices. Fast, secure, "
        "and fully automated delivery.\n\n"
        "<blockquote>"
        "{{w_shop}} <b>Shop</b> — Browse &amp; buy products\n"
        "{{w_deposit}} <b>Deposit</b> — Add funds to your wallet\n"
        "{{w_profile}} <b>My Profile</b> — Balance, orders &amp; settings\n"
        "{{w_support}} <b>Support</b> — Get help\n"
        "{{w_refer}} <b>Refer &amp; Earn</b> — Invite friends &amp; earn rewards"
        "</blockquote>",
        "Welcome message", "Welcome", ("shop", "name")),
    "menu_footer": Msg(
        "Choose an option below to continue! {{w_point}}",
        "Line under the welcome", "Welcome"),
    "support_title": Msg(
        "{{sup_title}} <b>Need Help?</b>", "Support heading", "Support"),
    "support": Msg(
        "Contact our support team directly:",
        "Support message", "Support"),
    "support_unset": Msg(
        "<i>No support contact is configured yet — set SUPPORT_URL in the bot's "
        "settings.</i>", "Support: no contact configured", "Support"),

    # ------------------------------------------------------------- shop
    "shop_header": Msg(
        "{{m_cart}} <b>Choose Your Product:</b>",
        "Product list heading", "Shop"),
    "shop_meta": Msg(
        "<i>{live} of {total} in stock · updated {time}</i>",
        "Line under the heading", "Shop", ("live", "total", "time")),
    "shop_empty": Msg(
        "The catalogue is empty right now — check back soon.",
        "Empty catalogue", "Shop"),
    "product_price": Msg(
        "{{pd_price}} Price: <b>{price}</b>{unit}",
        "Product: price line", "Product page", ("price", "unit")),
    "product_was": Msg(
        " {{pd_was}} <s>{was}</s>",
        "Product: struck-through list price", "Product page", ("was",)),
    "product_tier": Msg(
        "{{pd_tier}} <i>Your pricing: {tier}</i>",
        "Product: your tier line", "Product page", ("tier",)),
    "product_desc": Msg(
        "{{pd_desc}} <blockquote>{description}</blockquote>",
        "Product: description block", "Product page", ("description",)),
    "product_delivery": Msg(
        "<i>Delivery is automatic after payment confirmation.</i>",
        "Delivery promise on a product", "Shop"),
    "product_soldout": Msg(
        "{{m_error}} <b>Sold out</b> — check back shortly.",
        "Sold out notice", "Shop", ()),
    "product_low": Msg(
        "{{m_warn}} Only <b>{left}</b> left.",
        "Low stock warning", "Shop", ("left",)),

    # --------------------------------------------------------- checkout
    "qty_title": Msg("<b>Select Quantity</b>", "Quantity screen title", "Checkout"),
    "qty_question": Msg(
        "How many {unit} do you want?",
        "Quantity question", "Checkout", ("unit",)),
    "summary_title": Msg("{{m_receipt}} <b>Order Summary</b>", "Order summary title", "Checkout"),
    "pay_choose": Msg("{{m_card}} Choose a payment method:", "Payment prompt", "Checkout"),
    "order_cancelled": Msg(
        "Order #{oid} cancelled.", "Order cancelled", "Checkout", ("oid",)),
    "order_expired": Msg(
        "{{m_clock}} Order #{oid} expired unpaid.", "Order expired", "Checkout", ("oid",)),
    "ref_received": Msg(
        "{{m_mail}} Reference received for order #{oid}. "
        "You'll get your product the moment an admin confirms it.",
        "Reference received", "Checkout", ("oid",)),
    "ref_received_topup": Msg(
        "{{m_mail}} Reference received. Once it's checked, the amount you sent is added "
        "to your balance.",
        "Reference received (deposit)", "Checkout"),
    "order_rejected": Msg(
        "{{m_error}} Order #{oid} was rejected — we couldn't match your payment. "
        "Contact support if you believe this is a mistake.",
        "Order rejected", "Checkout", ("oid",)),

    # --------------------------------------------------------- delivery
    "delivered_title": Msg(
        "{{m_ok}} <b>Order #{oid} delivered</b>",
        "Delivery header", "Delivery", ("oid",)),
    # Everything above the items. Trim it to just the first line if you'd
    # rather the buyer received the keys and nothing else.
    # {tx_line} is blank when the order was paid from balance, so the TxID row
    # disappears by itself rather than showing an empty value.
    "delivered_body": Msg(
        "{{m_ok}} <b>Order Successful!</b>\n\n"
        "🧾 Order: <b>#{oid}</b>\n"
        "📅 Date: <b>{date}</b>\n"
        "Product: <b>{product}</b>\n"
        "Quantity: <b>{qty}</b>\n"
        "Total: <b>{amount}</b>"
        "{tx_line}\n\n"
        "{emoji} <b>{product} × {qty}</b>\n",
        "Delivery message", "Delivery",
        ("oid", "product", "qty", "amount", "method", "date", "txid",
         "tx_line", "tx_link", "network", "emoji")),
    # Sent the moment the money is confirmed, before the items are looked up.
    # Delivery is usually instant, but the buyer shouldn't be watching an empty
    # chat while stock is allocated.
    "order_placed": Msg(
        "{{m_ok}} <b>Payment Verified &amp; Order Placed!</b>\n\n"
        "💵 Paid: <b>{amount}</b>\n"
        "🛒 Product Cost: <b>{cost}</b>\n"
        "💳 Remaining Balance: <b>{balance}</b>\n\n"
        "⏳ Delivering your items…",
        "Payment verified (before delivery)", "Delivery",
        ("amount", "cost", "balance", "product", "qty", "oid")),
    "topup_confirmed": Msg(
        "{{tu_title}} <b>Deposit Verified!</b>\n\n"
        "{{tu_added}} Credited: <b>{amount}</b>\n"
        "🌐 Network: <b>{network}</b>\n"
        "{{tu_balance}} New Balance: <b>{balance}</b>"
        "{tx_line}",
        "Top-up confirmed message", "Delivery",
        ("amount", "balance", "network", "txid", "tx_line", "tx_link")),
    "overpay_credited": Msg(
        "{{tu_added}} You sent <b>{sent}</b>, which is <b>{extra}</b> more than "
        "the order total.\n"
        "The difference has been added to your wallet — new balance "
        "<b>{balance}</b>.",
        "Overpayment credited to wallet", "Delivery",
        ("sent", "extra", "balance", "oid")),
    "refund_notice": Msg(
        "{{m_warn}} Order #{oid} could not be fulfilled because {reason}.\n"
        "{amount} has been credited to your wallet balance.",
        "Automatic refund notice", "Delivery", ("oid", "reason", "amount")),

    # ----------------------------------------------------------- orders
    "orders_title": Msg("{{m_box}} <b>ORDER HISTORY</b>", "Order history title", "Orders"),
    "orders_intro": Msg(
        "Select an order to view details.\n\n"
        "{{m_search}} <i>Tip: Send <code>/order &lt;ID&gt;</code> (e.g. <code>/order A12B</code>) "
        "to find a specific order by its ID.</i>",
        "Order history intro", "Orders"),
    "orders_empty": Msg("{{m_box}} You have no orders yet.", "No orders", "Orders"),
    "order_detail": Msg(
        "{{m_box}} <b>Order #{code}</b>\n\n"
        "{{m_pin}} Product: <b>{product}</b>\n"
        "{{m_num}} Quantity: <b>{qty}</b>\n"
        "{{m_money}} Total: <b>{total}</b>\n"
        "{{m_date}} Date: {date}\n"
        "{{m_ok}} Status: <b>{status}</b>",
        "Order details", "Orders",
        ("code", "product", "qty", "total", "date", "status")),

    # ------------------------------------------------------------ groups
    "group_hit": Msg(
        "{products} <b>Available now!</b>\n"
        "{{g_title}} Tap below to buy:",
        "Group reply when a product is mentioned", "Groups",
        ("products", "count", "shop")),

    # ---------------------------------------------------------- profile
    "profile_body": Msg(
        "{{pf_title}} <b>User Profile</b>\n\n"
        "{{pf_id}} ID: <code>{id}</code>\n"
        "{{pf_balance}} Balance: <b>{balance}</b>\n"
        "{{pf_joined}} Joined: {joined}",
        "My Profile screen", "Profile text", ("id", "balance", "joined", "name")),
    "stats_body": Msg(
        "{{st_title}} <b>My Stats</b>\n\n"
        "{{st_orders}} Orders: <b>{orders}</b> · {items} item(s)\n"
        "{{st_spent}} Total spent: <b>{spent}</b>\n"
        "{{st_deposit}} Total deposited: <b>{deposited}</b>\n"
        "{{st_wallet}} Balance: <b>{balance}</b>{held}\n\n"
        "{{st_refer}} Referrals: <b>{invited}</b> invited · {buyers} bought\n"
        "{{st_gift}} Referral earnings: <b>{earnings}</b>\n\n"
        "{favourite}",
        "My Stats screen", "Profile text",
        ("orders", "items", "spent", "deposited", "balance", "held",
         "invited", "buyers", "earnings", "favourite")),
    "stats_favourite": Msg(
        "{{st_fav}} Most bought: <b>{product}</b>",
        "Stats: most-bought line", "Profile text", ("product",)),
    "stats_none": Msg(
        "{{st_fav}} No purchases yet", "Stats: no purchases line", "Profile text"),

    # ----------------------------------------------------- refer & earn
    "refer_body": Msg(
        "{{rf_title}} <b>Refer &amp; Earn</b>\n\n"
        "{{rf_users}} Referred (24h): <b>{day}</b>\n"
        "{{rf_users}} Referred (7d): <b>{week}</b>\n"
        "{{rf_users}} Referred (Total): <b>{total}</b>\n\n"
        "{{rf_earned}} Total Earned: <b>{earned}</b>\n"
        "{{rf_available}} Available: <b>{available}</b>\n"
        "{{rf_moved}} Transferred: <b>{transferred}</b>\n\n"
        "<blockquote>{terms}</blockquote>\n\n"
        "{{rf_link}} Your Referral Link:\n{link}",
        "Refer &amp; Earn screen", "Refer & Earn",
        ("day", "week", "total", "earned", "available", "transferred", "terms", "link")),
    "refer_percent": Msg(
        "Earn <b>{percent}%</b> of every {what} by your referred users.",
        "Refer: percentage line", "Refer & Earn", ("percent", "what")),
    "refer_bonus": Msg(
        "<b>+{bonus}</b> bonus on their first purchase.",
        "Refer: first-purchase bonus line", "Refer & Earn", ("bonus",)),
    "refer_transfer": Msg(
        "Transfer earnings to your wallet anytime.",
        "Refer: transfer line", "Refer & Earn"),

    # ---------------------------------------------------------- account
    "deposit_title": Msg("{{dep_title}} <b>Deposit USDT</b>", "Deposit screen title", "Account"),
    "deposit_sub": Msg(
        "Choose how you'd like to pay.",
        "Deposit screen subtitle", "Account"),
    "withdraw_note": Msg(
        "{{m_warn}} <i>Double-check it — payouts can't be reversed.</i>",
        "Withdrawal warning", "Account"),
    "banned": Msg("{{m_ban}} You are banned from this shop.", "Banned notice", "Account"),
}


# Tags an admin is allowed to type by hand. Telegram escapes anything typed as
# plain text, so without this a literal <b> arrives as &lt;b&gt; and renders as
# visible tags instead of bold.
_ALLOWED = (r"/?(?:b|strong|i|em|u|ins|s|strike|del|code|blockquote|pre"
            r"|a|tg-emoji|span"                       # closing tags carry no attrs
            r"|pre language=&quot;[^&]*&quot;"
            r"|a href=&quot;[^&]*&quot;"
            r"|tg-emoji emoji-id=&quot;\d+&quot;"
            r"|span class=&quot;tg-spoiler&quot;)")
_TAG = re.compile(r"&lt;(" + _ALLOWED + r")&gt;")


_WRAPPER = re.compile(r"^\s*<pre>\s*(?:<code[^>]*>)?(.*?)(?:</code>)?\s*</pre>\s*$", re.S)
_CODE_LANG = re.compile(r'<code language="[^"]*">')


def normalise_pasted(html: str) -> str:
    """Undo what copying a code block does to a message.

    Telegram copies a <pre> block *as a code block*, so pasting it back wraps
    the whole message in <pre><code> and re-escapes every entity. Left alone
    that produces markup Telegram refuses, and the message stops sending.
    """
    out = _CODE_LANG.sub("<code>", html)
    m = _WRAPPER.match(out)
    if m:
        out = m.group(1)
    # each round trip doubles &amp; — collapse it back
    while "&amp;amp;" in out:
        out = out.replace("&amp;amp;", "&amp;")
    return out.strip()


def to_source(html: str) -> str:
    """The form an admin edits.

    Premium emoji become copy-safe tokens, and slot references are resolved to
    the emoji they currently point at — so everything on screen is something
    the admin can see and swap, with nothing to look up elsewhere.
    """
    return flair.expand_slots(flair.tokenise(html))


def restore_tags(html: str) -> str:
    """Turn escaped markup an admin typed back into real tags.

    Telegram's own formatting (bold, spoilers, premium emoji) already arrives as
    entities and is untouched by this — the two can be mixed in one message.
    """
    return _TAG.sub(lambda m: "<" + m.group(1).replace("&quot;", '"') + ">", html)


# Reference formats we accept, longest first:
#   EVM / TON tx hash      66 chars (0x + 64 hex)
#   Litecoin txid          64 chars
#   Binance transaction id ~20 chars (M_P_...)
#   UPI UTR                12 digits
REF_MIN, REF_MAX = 6, 128


def valid_ref(ref: str) -> bool:
    ref = (ref or "").strip()
    if not REF_MIN <= len(ref) <= REF_MAX:
        return False
    # a reference is one token — reject pasted sentences or several at once
    return not any(ch.isspace() for ch in ref)


def _safe_format(tpl: str, values: dict) -> str:
    class Keep(dict):
        def __missing__(self, key):        # leave unknown placeholders visible
            return "{" + key + "}"
    try:
        return tpl.format_map(Keep(values))
    except (ValueError, IndexError):
        return tpl                          # stray brace in the admin's text


async def raw(key: str) -> str:
    """The stored text exactly as an admin would edit it."""
    return await db.setting(f"text:{key}", MESSAGES[key].default)


async def t(key: str, **values) -> str:
    """Rendered message: placeholders filled, {{slot}} icons expanded, and
    premium emoji stripped automatically if Telegram has refused them."""
    tpl = await raw(key)
    # icons first: str.format treats {{slot}} as an escaped brace and would
    # collapse it to {slot} before the expander ever saw it
    return _safe_format(await flair.render(tpl), values)


def sections() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, msg in MESSAGES.items():
        out.setdefault(msg.section, []).append(key)
    return out


async def overrides() -> dict[str, str]:
    """Only the messages an admin has actually changed."""
    all_settings = await db.all_settings()
    return {k[5:]: v for k, v in all_settings.items() if k.startswith("text:")}
