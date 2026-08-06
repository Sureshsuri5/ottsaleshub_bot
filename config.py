from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _ints(key: str) -> list[int]:
    raw = os.getenv(key, "")
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def _public_url() -> str:
    """Explicit WEBAPP_URL wins; otherwise read the host's own injected domain."""
    explicit = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    for var in ("RENDER_EXTERNAL_URL", "KOYEB_PUBLIC_DOMAIN", "RAILWAY_PUBLIC_DOMAIN",
                "FLY_APP_NAME"):
        v = os.getenv(var, "").strip()
        if not v:
            continue
        if var == "FLY_APP_NAME":
            v = f"{v}.fly.dev"
        return v.rstrip("/") if v.startswith("http") else f"https://{v}".rstrip("/")
    return ""


def _list(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=lambda: _ints("ADMIN_IDS"))
    db_path: str = os.getenv("DB_PATH", "shop.db")
    shop_name: str = os.getenv("SHOP_NAME", "Shop")
    support_url: str = os.getenv("SUPPORT_URL", "")

    fiat: str = os.getenv("FIAT_CURRENCY", "INR")
    symbol: str = os.getenv("CURRENCY_SYMBOL", "₹")
    decimals: int = int(os.getenv("PRICE_DECIMALS", "2"))
    # optional second currency, shown as a toggle in the storefront. Display
    # only — orders are always priced and settled in the primary currency.
    second_code: str = os.getenv("SECOND_CURRENCY", "")
    second_symbol: str = os.getenv("SECOND_SYMBOL", "")
    second_rate: float = float(os.getenv("SECOND_RATE", "1"))

    providers: list[str] = field(
        default_factory=lambda: _list("ENABLED_PROVIDERS", "binance,bep20,polygon,ton")
    )

    stars_rate: float = float(os.getenv("STARS_RATE", "1.6"))

    tron_address: str = os.getenv("TRON_ADDRESS", "")
    trongrid_key: str = os.getenv("TRONGRID_API_KEY", "")
    # How many units of YOUR shop currency one USDT is worth.
    # 1 for a USD shop; ~90 for a rupee shop. Getting this wrong multiplies
    # every crypto payment, so it's checked at startup.
    usdt_rate: float = float(os.getenv("USDT_RATE", "1"))

    evm_address: str = os.getenv("EVM_ADDRESS", "")
    etherscan_key: str = os.getenv("ETHERSCAN_API_KEY", "")

    # read-only API key on the Binance account that receives payments
    binance_key: str = os.getenv("BINANCE_API_KEY", "")
    binance_secret: str = os.getenv("BINANCE_API_SECRET", "")

    ton_address: str = os.getenv("TON_ADDRESS", "")
    tonapi_key: str = os.getenv("TONAPI_KEY", "")
    ltc_address: str = os.getenv("LTC_ADDRESS", "")
    ltc_rate: float = float(os.getenv("LTC_RATE", "0"))

    # Rupees per one unit of the shop currency. Falls back to SECOND_RATE when
    # the second currency is INR, so a USD shop showing ₹ prices needs nothing
    # extra. A rupee shop leaves it at 1.
    inr_rate: float = float(os.getenv("INR_RATE", "0"))

    # Razorpay: the bot creates a payment link per order, Razorpay collects,
    # and either the webhook or a status poll settles it.
    razorpay_key: str = os.getenv("RAZORPAY_KEY_ID", "")
    razorpay_secret: str = os.getenv("RAZORPAY_KEY_SECRET", "")

    upi_vpa: str = os.getenv("UPI_VPA", "")
    upi_payee: str = os.getenv("UPI_PAYEE_NAME", "Shop")

    # Mini Apps (storefront + admin panel), served by webapp.py
    webapp_enabled: bool = _bool("WEBAPP_ENABLED", True)
    webapp_url: str = _public_url()
    webapp_host: str = os.getenv("WEBAPP_HOST", "0.0.0.0")
    # PaaS platforms inject the port; PORT wins over WEBAPP_PORT when present.
    webapp_port: int = int(os.getenv("PORT") or os.getenv("WEBAPP_PORT", "8080"))
    # 'polling' works anywhere; 'webhook' is right for a PaaS (one port, no long
    # poll, survives sleep/wake). 'auto' picks webhook when a public URL exists.
    bot_mode: str = os.getenv("BOT_MODE", "auto").strip().lower()
    webhook_secret_tg: str = os.getenv("TG_WEBHOOK_SECRET", "") or os.getenv("ADMIN_PANEL_TOKEN", "")
    # Where the admin panel is served. Anything other than the default keeps it
    # off the list of paths scanners try — they probe /admin constantly.
    admin_path: str = "/" + os.getenv("ADMIN_PATH", "admin").strip("/")

    panel_token: str = os.getenv("ADMIN_PANEL_TOKEN", "")

    webhook_enabled: bool = _bool("WEBHOOK_ENABLED", False)
    webhook_host: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    webhook_port: int = int(os.getenv("WEBHOOK_PORT", "8080"))
    webhook_path: str = os.getenv("WEBHOOK_PATH", "/psp/webhook")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")

    sales_chat: str = os.getenv("SALES_CHAT_ID", "")
    # Where restock announcements go. Falls back to the sales feed chat, since
    # most shops want both in the same place.
    restock_chat: str = os.getenv("RESTOCK_CHAT_ID", "")

    channel_url: str = os.getenv("CHANNEL_URL", "")
    group_url: str = os.getenv("GROUP_URL", "")
    # paid to the referrer when someone they invited completes their FIRST purchase
    ref_bonus: float = float(os.getenv("REFERRAL_BONUS", "0"))
    # ...plus this share of everything they spend afterwards, as a percentage
    ref_percent: float = float(os.getenv("REFERRAL_PERCENT", "0"))
    # count deposits toward the percentage as well as purchases
    ref_on_deposit: bool = _bool("REFERRAL_ON_DEPOSIT", True)

    # Add a tiny random offset to each crypto amount (10.0134 instead of 10).
    # Off by default: buyers prefer the round figure they typed. Turn it on if
    # you expect several people paying the same amount at the same moment.
    unique_amounts: bool = _bool("UNIQUE_AMOUNTS", False)
    # Watch-only account xpub at m/44'/60'/0'/0. When set, every order gets its
    # own freshly derived deposit address instead of sharing one, and payments
    # are matched by address rather than by amount. Public key only — this
    # cannot spend, and the seed must never be put on the server.
    evm_xpub: str = os.getenv("EVM_XPUB", "").strip()

    min_deposit: float = float(os.getenv("MIN_DEPOSIT", "1"))
    min_withdrawal: float = float(os.getenv("MIN_WITHDRAWAL", "10"))
    withdraw_methods: list[str] = field(
        default_factory=lambda: _list("WITHDRAW_METHODS", "USDT TRC-20,USDT BEP20,UPI"))
    api_enabled: bool = _bool("DEVELOPER_API", True)

    # IANA name, e.g. Asia/Kolkata. Dates shown to buyers use this.
    timezone: str = os.getenv("TIMEZONE", "UTC")

    # Render's free plan spins a service down after ~15 minutes without
    # inbound traffic, which stops the payment watcher — a crypto transfer made
    # while it sleeps isn't confirmed until something wakes it, and the order
    # can expire first. Pinging our own public /health keeps it up. 0 = off.
    # An external uptime monitor does the same job from outside; use that if
    # you'd rather not spend the instance hours.
    keepalive: int = int(os.getenv("KEEPALIVE_MINUTES", "0"))

    order_ttl: int = int(os.getenv("ORDER_TTL_MINUTES", "30"))
    # abandoned checkouts are deleted after this many days. 0 keeps them forever.
    keep_dead_orders: int = int(os.getenv("KEEP_ABANDONED_ORDERS_DAYS", "7"))
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
    # Longest a single rail may take before the watcher gives up on it for this
    # tick. Rails are polled in parallel, so this caps the whole cycle, not the
    # sum of them.
    poll_timeout: int = int(os.getenv("POLL_TIMEOUT_SECONDS", "40"))
    # How long an expired order's deposit address keeps being watched, so a
    # late payment is credited instead of stranded. 0 turns it off.
    late_hours: int = int(os.getenv("LATE_PAYMENT_HOURS", "48"))
    low_stock: int = int(os.getenv("LOW_STOCK_ALERT", "3"))

    @property
    def upi_rate(self) -> float:
        """How many rupees one unit of the shop currency is worth."""
        if self.fiat.upper() == "INR":
            return 1.0
        if self.inr_rate > 0:
            return self.inr_rate
        if self.second_code.upper() == "INR" and self.second_rate > 0:
            return self.second_rate
        return 0.0                      # unknown — UPI can't quote safely

    def rate_warning(self) -> str:
        """Catch a USDT_RATE that can't be right for the shop currency.

        A mismatch silently multiplies every crypto payment — a $1 deposit
        credited as $90 — so it's better to shout than to quietly overpay.
        """
        if "upi" in self.providers and self.upi_rate <= 0:
            return (f"UPI is enabled but there's no rupee rate. Your shop is in "
                    f"{self.fiat}, so set INR_RATE (or SECOND_CURRENCY=INR with "
                    "SECOND_RATE) or UPI will ask for the wrong amount.")
        if self.usdt_rate <= 0:
            return "USDT_RATE must be greater than 0 — crypto payments can't be priced."
        if self.fiat.upper() in {"USD", "USDT", "USDC"} and abs(self.usdt_rate - 1) > 0.05:
            return (f"USDT_RATE is {self.usdt_rate:g} but your shop currency is "
                    f"{self.fiat}. One USDT is one {self.fiat}, so every crypto "
                    f"payment is credited {self.usdt_rate:g}x. Set USDT_RATE=1")
        return ""

    def money(self, amount: float) -> str:
        """Money always shows the same number of decimals — a price list where
        some rows say 5 and others 4.50 reads as sloppy."""
        return f"{self.symbol}{amount:,.{self.decimals}f}"

    def is_admin(self, uid: int) -> bool:
        return uid in self.admin_ids

    @property
    def use_webhook(self) -> bool:
        if self.bot_mode == "webhook":
            return True
        if self.bot_mode == "polling":
            return False
        return bool(self.webapp_url.startswith("https://"))

    @property
    def miniapps_live(self) -> bool:
        """Mini App buttons only work over public HTTPS."""
        return self.webapp_enabled and self.webapp_url.startswith("https://")


cfg = Config()
