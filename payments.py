"""Payment providers.

Every provider implements the same tiny interface, so adding a new one
(BTC, Litecoin, Stripe, Cashfree, ...) means writing one class and adding its
code to ENABLED_PROVIDERS — nothing else in the bot changes.

    quote(amount_fiat)  -> (pay_amount, unit)   convert price into the provider unit
    create(order)       -> Invoice              instructions shown to the buyer
    poll(orders)        -> list[(order_id, ref)] orders confirmed since last poll
"""
from __future__ import annotations

import io
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
import qrcode

import db
from config import cfg

log = logging.getLogger(__name__)


def _esc(s) -> str:
    """Escape text that goes inside an HTML message."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRONGRID = "https://api.trongrid.io"
# Etherscan V2: one endpoint, one key, chain picked with ?chainid=
ETHERSCAN = "https://api.etherscan.io/v2/api"

# Every EVM chain works the same way, so a chain is just data.
# Add a row here and it becomes a payment method — no new code.
# Each chain also carries a public JSON-RPC endpoint. That's the fallback when
# the block explorer is unavailable or the API plan doesn't cover the chain —
# reading Transfer logs straight from a node needs no key and no plan.
EVM_CHAINS = {
    "bep20": dict(title="₮ USDT/USDC BEP20", chain_id=56, decimals=18, network="BEP20",
                  rpc=("https://bsc-rpc.publicnode.com",
                       "https://binance.llamarpc.com",
                       "https://bsc-dataseed.binance.org",
                       "https://bsc-dataseed1.defibit.io",
                       "https://1rpc.io/bnb",
                       "https://rpc.ankr.com/bsc"),
                  contracts=("0x55d398326f99059fF775485246999027B3197955",   # USDT
                             "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d")),  # USDC
    "polygon": dict(title="◈ USDT Polygon", chain_id=137, decimals=6, network="Polygon",
                    rpc=("https://polygon-rpc.com",
                         "https://rpc.ankr.com/polygon",
                         "https://polygon-bor-rpc.publicnode.com"),
                    contracts=("0xc2132D05D31c914a87C6611C10748AEb04B58e8F",)),
    "arbitrum": dict(title="₮ USDT Arbitrum", chain_id=42161, decimals=6, network="Arbitrum",
                     rpc=("https://arb1.arbitrum.io/rpc",
                          "https://arbitrum-one-rpc.publicnode.com"),
                     contracts=("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",)),
    "base": dict(title="₮ USDC Base", chain_id=8453, decimals=6, network="Base",
                 rpc=("https://mainnet.base.org",
                      "https://base-rpc.publicnode.com"),
                 contracts=("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",)),
    "erc20": dict(title="₮ USDT ERC20", chain_id=1, decimals=6, network="Ethereum",
                  rpc=("https://eth.llamarpc.com",
                       "https://ethereum-rpc.publicnode.com"),
                  contracts=("0xdAC17F958D2ee523a2206206994597C13D831ec7",)),
}

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Public nodes cap how many blocks one eth_getLogs call may span and rate-limit
# on top. Start conservative, halve on refusal, and never ask for more than we
# actually need — after the first poll that's only the blocks since last time.
RPC_LOOKBACK_BLOCKS = 1200
RPC_WINDOW_START = 400
RPC_WINDOW_MIN = 40


@dataclass
class Invoice:
    text: str
    pay_amount: float
    pay_unit: str
    pay_address: str | None = None
    qr_payload: str | None = None      # string encoded into a QR image
    manual_ref: bool = False           # buyer must submit a reference number
    native_stars: int = 0              # non-zero -> send a Telegram Stars invoice
    pay_url: str | None = None         # hosted checkout to open in a browser


def qr_png(payload: str) -> bytes:
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------- balance
class BalanceProvider:
    group = "wallet"
    variable_deposit = False
    code = "balance"
    title = "👛 Wallet balance"
    instant = True
    unit = cfg.fiat

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        return round(amount, 2), cfg.fiat

    async def create(self, order) -> Invoice:  # never reached: settled instantly
        return Invoice(text="Paid from balance.", pay_amount=order["amount"], pay_unit=cfg.fiat)

    async def poll(self, orders) -> list[tuple[int, str]]:
        return []


# ----------------------------------------------------------------- stars
class StarsProvider:
    group = "wallet"
    variable_deposit = False
    """Telegram Stars. Verification is native: Telegram sends us a
    `successful_payment` update, so there is nothing to poll and nothing to
    trust from the user."""

    code = "stars"
    title = "⭐ Telegram Stars"
    instant = False
    unit = "XTR"

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        return max(1, round(amount / cfg.stars_rate)), "XTR"

    async def create(self, order) -> Invoice:
        stars, _ = self.quote(order["amount"])
        return Invoice(
            text="Tap the invoice below to pay with Telegram Stars. "
                 "Delivery is instant once Telegram confirms.",
            pay_amount=stars,
            pay_unit="XTR",
            native_stars=int(stars),
        )

    async def poll(self, orders) -> list[tuple[int, str]]:
        return []


# ---------------------------------------------------------------- crypto
class CryptoProvider:
    asks_amount = True
    """USDT on TRON (TRC-20).

    Single receiving address + a unique amount per order. Each pending order
    gets a random sub-cent offset, so an incoming transfer maps to exactly one
    order. TronGrid is polled for inbound TRC-20 transfers; a transfer counts
    only if it is newer than the order and its txid has never been used before.
    """

    group = "direct"
    variable_deposit = True
    code = "crypto"
    title = "₮ USDT (TRC-20)"
    instant = False
    unit = "USDT"

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        return round(amount / cfg.usdt_rate, 2), "USDT"

    async def unique_amount(self, amount_fiat: float) -> float:
        base, _ = self.quote(amount_fiat)
        if not cfg.unique_amounts:
            return base                      # ask for exactly what they chose
        for _ in range(60):
            candidate = round(base + random.randint(1, 899) / 10_000, 4)
            if not await db.amount_taken(candidate, "USDT"):
                return candidate
        return round(base + random.randint(900, 9999) / 10_000, 4)

    async def create(self, order) -> Invoice:
        amount = order["pay_amount"]
        addr = cfg.tron_address
        if order["kind"] == "topup" and not order["amount"]:
            return Invoice(
                text=("{{dep_tip}} You can send <b>any amount</b> — it will be added to your "
                      "balance.\n\n———————————————\n\n"
                      "{{dep_bank}} <b>USDT · TRON (TRC-20)</b>\n\n"
                      f"<code>{addr}</code>\n👆 <i>Tap to copy</i>\n\n"
                      "———————————————\n\n"
                      "After sending, paste your <b>Transaction Hash (TxID)</b> here "
                      "and we'll verify it <b>automatically</b>."),
                pay_amount=0, pay_unit="USDT", pay_address=addr,
                qr_payload=f"tron:{addr}", manual_ref=True)
        text = (
            f"<b>Send exactly {amount} USDT</b> (TRC-20 / TRON network)\n\n"
            f"<code>{addr}</code>\n\n"
            "{{dep_warn}} The amount must match to the last decimal — that is how the "
            "payment is matched to your order.\n"
            f"{{{{dep_clock}}}} This order expires in {cfg.order_ttl} minutes.\n\n"
            "Delivery is automatic after 1 network confirmation."
        )
        return Invoice(
            text=text,
            pay_amount=amount,
            pay_unit="USDT",
            pay_address=addr,
            qr_payload=f"tron:{addr}?amount={amount}",
        )

    async def _inbound(self) -> list[dict]:
        if not cfg.tron_address:
            return []
        headers = {"TRON-PRO-API-KEY": cfg.trongrid_key} if cfg.trongrid_key else {}
        url = (f"{TRONGRID}/v1/accounts/{cfg.tron_address}/transactions/trc20"
               f"?only_to=true&limit=100&contract_address={USDT_TRC20}")
        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json()
        except Exception:
            return []
        out = []
        for tx in data.get("data", []):
            dec = int(tx.get("token_info", {}).get("decimals", 6))
            out.append({"id": tx.get("transaction_id", ""),
                        "value": int(tx.get("value", 0)) / (10 ** dec)})
        return out

    async def verify_ref(self, ref: str) -> float | None:
        """Confirm a pasted TxID landed on our address and return its amount.

        This is what makes an any-amount deposit automatic: instead of matching
        a reserved figure, we read the figure straight off the chain.
        """
        ref = ref.strip().lower().removeprefix("0x")
        for tx in await self._inbound():
            if tx["id"].lower().removeprefix("0x") == ref:
                return round(tx["value"] * cfg.usdt_rate, 2)
        return None

    async def diagnose(self) -> str:
        if not cfg.tron_address:
            return "TRON_ADDRESS is not set"
        seen = await self._inbound()
        return (f"no inbound USDT transfers found for {cfg.tron_address[:10]}…"
                if not seen else f"not among the last {len(seen)} transfers")

    async def poll(self, orders) -> list[tuple[int, str]]:
        if not orders or not cfg.tron_address:
            return []
        headers = {"TRON-PRO-API-KEY": cfg.trongrid_key} if cfg.trongrid_key else {}
        url = (f"{TRONGRID}/v1/accounts/{cfg.tron_address}/transactions/trc20"
               f"?only_to=true&limit=100&contract_address={USDT_TRC20}")
        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json()
        except Exception:
            return []

        confirmed: list[tuple[int, str]] = []
        for tx in data.get("data", []):
            txid = tx.get("transaction_id")
            if not txid:
                continue
            decimals = int(tx.get("token_info", {}).get("decimals", 6))
            value = int(tx.get("value", 0)) / (10 ** decimals)
            ts = datetime.fromtimestamp(tx.get("block_timestamp", 0) / 1000, tz=timezone.utc)

            for o in orders:
                if abs(value - float(o["pay_amount"])) > 0.0000005:
                    continue
                created = datetime.strptime(o["created_at"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
                # the transfer must not predate the order (180s slack for clock skew)
                if (created - ts).total_seconds() > 180:
                    continue
                if await db.mark_seen(txid, o["id"]):
                    confirmed.append((o["id"], txid))
                break
        return confirmed


# ------------------------------------------------------------------- upi
class UpiProvider:
    group = "wallet"
    variable_deposit = False
    """UPI (India).

    Without a payment service provider there is no honest way to auto-verify a
    UPI transfer, so this provider has two modes:

      * WEBHOOK_ENABLED=false -> buyer submits the 12-digit UTR, the order goes
        to `awaiting_review` and an admin approves it in one tap.
      * WEBHOOK_ENABLED=true  -> a PSP (Razorpay/Cashfree/PhonePe) posts a
        signed callback to webhook.py and the order settles automatically.
    """

    code = "upi"
    title = "🇮🇳 UPI"
    instant = False
    unit = "INR"

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        """UPI always moves rupees, whatever the shop prices in.

        Quoting the shop figure directly would ask an Indian buyer for ₹1.50
        when the product costs $1.50 — so convert, and refuse rather than
        guess if no rate is configured.
        """
        rate = cfg.upi_rate
        if rate <= 0:
            return 0.0, "INR"
        return round(amount * rate, 2), "INR"

    async def create(self, order) -> Invoice:
        amt = f"{order['pay_amount']:.2f}"
        note = f"ORD{order['code'] or order['id']}"
        link = (f"upi://pay?pa={cfg.upi_vpa}&pn={cfg.upi_payee.replace(' ', '%20')}"
                f"&am={amt}&cu=INR&tn={note}")
        auto = cfg.webhook_enabled
        shown = (f"\n<i>({cfg.money(order['amount'])} at ₹{cfg.upi_rate:g} "
                 f"per {cfg.fiat})</i>" if cfg.fiat.upper() != "INR" else "")
        text = (
            f"<b>Pay ₹{amt} via UPI</b>{shown}\n\n"
            f"UPI ID: <code>{cfg.upi_vpa}</code>\n"
            f"Payee: <b>{_esc(cfg.upi_payee)}</b>\n"
            f"Reference note: <code>{note}</code>\n\n"
            "Scan the QR above or tap the pay link below."
        )
        text += (
            "\n\nDelivery is automatic once the payment clears."
            if auto else
            "\n\nAfter paying, tap <b>I've paid</b> and send the 12-digit UTR / "
            "transaction reference. Orders are usually approved within minutes."
        )
        return Invoice(
            text=text,
            pay_amount=float(amt),
            pay_unit="INR",
            pay_address=cfg.upi_vpa,
            qr_payload=link,
            manual_ref=not auto,
        )

    async def poll(self, orders) -> list[tuple[int, str]]:
        return []  # settled by webhook.py or by admin approval


class EvmTokenProvider:
    asks_amount = True
    variable_deposit = True
    """Stablecoin on any EVM chain, verified through Etherscan V2.

    Same unique-amount trick as TRON: each pending order gets its own amount, so
    an inbound transfer maps to exactly one order. One API key covers every
    chain — set ETHERSCAN_API_KEY and EVM_ADDRESS once and enable as many
    networks as you like.
    """

    group = "direct"
    instant = False

    def __init__(self, code: str, spec: dict):
        self.code = code
        self.title = spec["title"]
        self.chain_id = spec["chain_id"]
        self.contracts = spec["contracts"]
        self.decimals = spec["decimals"]
        self.network = spec["network"]
        env = os.getenv(f"EVM_RPC_{code.upper()}", "")
        self.rpcs = ([u.strip() for u in env.split(",") if u.strip()]
                     or list(spec.get("rpc", ())))
        self._rpc_at = 0                     # which endpoint we're using
        self.window = RPC_WINDOW_START       # blocks per getLogs call
        self._scanned_to = 0                 # highest block already examined
        self.rpc_error = ""                  # why the last RPC call failed
        self.rpc_ok = False                  # a node has answered at least once
        self.unit = "USDT" if "USDT" in spec["title"] else "USDC"
        self.last_error = ""      # whatever the explorer last complained about
        self.plan_blocked = False  # explorer refuses this chain on the current plan

    def quote(self, amount: float) -> tuple[float, str]:
        return round(amount / cfg.usdt_rate, 2), self.unit

    async def unique_amount(self, amount_fiat: float) -> float:
        base, _ = self.quote(amount_fiat)
        if not cfg.unique_amounts:
            return base
        for _ in range(60):
            candidate = round(base + random.randint(1, 899) / 10_000, 4)
            if not await db.amount_taken(candidate, self.unit):
                return candidate
        return round(base + random.randint(900, 9999) / 10_000, 4)

    async def create(self, order) -> Invoice:
        addr, net = cfg.evm_address, self.network
        tokens = "USDT / USDC" if len(self.contracts) > 1 else self.unit
        confirms = "3 confirmations (~9 sec)" if self.chain_id == 56 else "1 confirmation"
        variable = order["kind"] == "topup" and not order["amount"]

        head = [f"{self.title} — <b>Auto-Verify</b>", ""]
        if not variable:
            head = [
                "———————————————",
                f"{{{{dep_box}}}} Product: <b>{_esc(order['product_name'])}</b>",
                f"{{{{dep_num}}}} Quantity: <b>{order['qty']}</b>",
                f"{{{{dep_amount}}}} Total: <b>{cfg.money(order['amount'])}</b>",
                "———————————————", "",
                f"{self.title} — <b>Auto-Verify</b>", "",
            ]

        body = list(head)
        if variable:
            body += ["{{dep_amount}} Amount: <b>any</b> — credited exactly as received", ""]
        else:
            body += [f"{{{{dep_amount}}}} Amount: <b>{order['pay_amount']} {self.unit}</b>",
                     f"{{{{dep_clock}}}} Expires in: <b>{cfg.order_ttl} min</b>", ""]

        body += [f"{{{{dep_net}}}} Send to this address ({net}):",
                 f"<code>{addr}</code>", "{{dep_point}} <i>Tap to copy</i>", ""]

        if len(self.contracts) > 1:
            body.append(f"{{{{dep_ok}}}} {tokens} — both accepted on this address.")
        if variable:
            body.append("{{dep_ok}} Any amount is credited exactly as received.")
        else:
            body.append("{{dep_warn}} Send the exact amount above — that is how the payment "
                        "is matched to your order.")
        body += [f"{{{{dep_warn}}}} <b>{net} only.</b> Wrong network = lost funds.", "",
                 f"<i>Auto-verified after {confirms}.</i>"]

        return Invoice(
            text="\n".join(body),
            pay_amount=order["pay_amount"] or 0, pay_unit=self.unit, pay_address=addr,
            qr_payload=f"ethereum:{addr}", manual_ref=variable)

    # ---- public JSON-RPC fallback ------------------------------------------
    @property
    def rpc(self) -> str:
        return self.rpcs[self._rpc_at % len(self.rpcs)] if self.rpcs else ""

    async def _rpc(self, method: str, params: list, _tries: int | None = None):
        """One JSON-RPC call, moving to the next endpoint when one refuses.

        Public nodes fail constantly — rate limits, range caps, brief outages.
        Rotating rather than giving up is the difference between a rail that
        verifies and one that quietly doesn't.
        """
        if not self.rpcs:
            return None
        tries = len(self.rpcs) if _tries is None else _tries
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for _ in range(tries):
            url = self.rpc
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(url, json=payload,
                                      timeout=aiohttp.ClientTimeout(total=8)) as r:
                        data = await r.json()
            except Exception as e:
                self.rpc_error = f"{url}: {type(e).__name__}: {e}"
                log.debug("%s rpc %s failed: %s", self.code, url, e)
                self._rpc_at += 1
                continue
            err = (data or {}).get("error")
            if err:
                msg = str(err.get("message", "")).lower()
                # a range/rate complaint means ask for less, not ask again
                if "limit" in msg or "range" in msg or "too many" in msg:
                    self.window = max(RPC_WINDOW_MIN, self.window // 2)
                    log.debug("%s narrowed getLogs window to %s blocks",
                              self.code, self.window)
                self.rpc_error = f"{url}: {err}"
                log.debug("%s rpc %s error: %s", self.code, url, err)
                self._rpc_at += 1
                continue
            self.rpc_error = ""
            self.rpc_ok = True
            return (data or {}).get("result")
        # say what actually went wrong — "refused" alone is not diagnosable
        log.warning("%s: no RPC endpoint answered. Last error — %s",
                    self.code, self.rpc_error or "unknown")
        return None

    async def _inbound_rpc(self) -> list[dict]:
        """Inbound token transfers read straight from a node.

        Needs no API key. The first poll looks back a fixed span; after that we
        only ask for blocks we haven't seen, which keeps every later call tiny
        and well inside what a public node will serve.
        """
        if not cfg.evm_address or not self.rpcs:
            return []
        head = await self._rpc("eth_blockNumber", [])
        if not head:
            return []
        latest = int(head, 16)
        start = max(self._scanned_to + 1, latest - RPC_LOOKBACK_BLOCKS)
        if start > latest:
            return []
        topic_to = "0x" + cfg.evm_address.lower().removeprefix("0x").rjust(64, "0")

        out, hit_end = [], start
        for contract in self.contracts:
            frm = start
            while frm <= latest:
                to = min(frm + self.window - 1, latest)
                logs = await self._rpc("eth_getLogs", [{
                    "fromBlock": hex(frm), "toBlock": hex(to), "address": contract,
                    "topics": [TRANSFER_TOPIC, None, topic_to],
                }])
                if logs is None:                  # every endpoint refused
                    return out
                for lg in logs:
                    try:
                        value = int(lg.get("data", "0x0"), 16) / (10 ** self.decimals)
                    except (TypeError, ValueError):
                        continue
                    out.append({"id": lg.get("transactionHash", ""),
                                "value": value, "ts": 0})
                hit_end = max(hit_end, to)
                frm = to + 1
        self._scanned_to = hit_end
        return out

    async def _inbound(self) -> list[dict]:
        """Inbound transfers of every token this chain accepts.

        Explorer first (it carries timestamps), then a public node if the
        explorer is unavailable or the plan doesn't cover this chain.
        """
        if not cfg.evm_address:
            return []
        if not cfg.etherscan_key or self.plan_blocked:
            return await self._inbound_rpc()
        out, explorer_failed = [], False
        for contract in self.contracts:
            params = {
                "chainid": self.chain_id, "module": "account", "action": "tokentx",
                "contractaddress": contract, "address": cfg.evm_address,
                "page": 1, "offset": 100, "sort": "desc", "apikey": cfg.etherscan_key,
            }
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(ETHERSCAN, params=params,
                                     timeout=aiohttp.ClientTimeout(total=20)) as r:
                        data = await r.json()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                continue
            if str(data.get("status")) != "1":
                explorer_failed = True
                # Etherscan answers with status 0 and a reason — a plan
                # restriction, a bad key, or simply "no transactions found".
                # Keep it: it's the difference between a bug and a bill.
                note = str(data.get("result") or data.get("message") or "").strip()
                if note and "no transactions found" not in note.lower():
                    self.last_error = note
                    # "Free API access is not supported for this chain" — the rail
                    # still works, but only a human can confirm payments on it.
                    self.plan_blocked = "not supported for this chain" in note.lower()
                continue
            self.last_error = ""
            for tx in data.get("result", []):
                if tx.get("to", "").lower() != cfg.evm_address.lower():
                    continue
                dec = int(tx.get("tokenDecimal", self.decimals))
                out.append({"id": tx.get("hash", ""),
                            "value": int(tx.get("value", 0)) / (10 ** dec),
                            "ts": int(tx.get("timeStamp", 0))})
        if not out and explorer_failed:
            return await self._inbound_rpc()      # explorer said no, ask a node
        return out

    async def verify_ref(self, ref: str) -> float | None:
        ref = ref.strip().lower().removeprefix("0x")
        for tx in await self._inbound():
            if tx["id"].lower().removeprefix("0x") == ref:
                return round(tx["value"] * cfg.usdt_rate, 2)
        return None

    async def diagnose(self) -> str:
        if not cfg.evm_address:
            return "EVM_ADDRESS is not set"

        seen = await self._inbound()
        via_node = bool(self.rpcs) and (self.plan_blocked or not cfg.etherscan_key
                                       or bool(self.last_error))
        source = f"a public {self.network} node" if via_node else "the explorer"

        if not seen:
            if via_node:
                head = await self._rpc("eth_blockNumber", [])
                if not head:
                    return (f"no {self.network} node answered. Last error — "
                            f"{self.rpc_error[:180] or 'unknown'}. Set EVM_RPC_"
                            f"{self.code.upper()} to an endpoint you can reach")
                return (f"{source} reports no transfers to {cfg.evm_address[:10]}… "
                        f"in the last {RPC_LOOKBACK_BLOCKS} blocks — check the "
                        "address and that the token is USDT or USDC")
            if self.last_error:
                return f"the explorer refused the request — “{self.last_error[:160]}”"
            return (f"no inbound transfers for {cfg.evm_address[:10]}… on "
                    f"{self.network} — check the address and network match")
        return f"not among the last {len(seen)} transfers seen via {source}"

    async def poll(self, orders) -> list[tuple[int, str]]:
        if not orders:
            return []
        confirmed: list[tuple[int, str]] = []
        for tx in await self._inbound():          # already filtered to inbound
            txid, value = tx["id"], tx["value"]
            for o in orders:
                if not o["pay_amount"]:
                    continue
                if abs(value - float(o["pay_amount"])) > 0.0000005:
                    continue
                # ts is 0 for logs read from a node — those come from a bounded
                # recent block window, so they can't be stale and the age check
                # would reject every one of them
                if tx["ts"]:
                    ts = datetime.fromtimestamp(tx["ts"], tz=timezone.utc)
                    created = datetime.strptime(
                        o["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if (created - ts).total_seconds() > 180:   # tx predates the order
                        continue
                if await db.mark_seen(f"{self.code}:{txid}", o["id"]):
                    confirmed.append((o["id"], txid))
                break
        return confirmed


TONAPI = "https://tonapi.io"
USDT_TON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
BLOCKCHAIR = "https://api.blockchair.com/litecoin"


class TonJettonProvider:
    asks_amount = True
    """USDT on TON, read through TonAPI's jetton history for your address.

    Same unique-amount matching as the EVM chains. TonAPI is public; an API key
    only raises the rate limit.
    """

    group = "direct"
    variable_deposit = True
    code = "ton"
    title = "◈ USDT TON"
    unit = "USDT"
    network = "TON"

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        return round(amount / cfg.usdt_rate, 2), "USDT"

    async def unique_amount(self, amount_fiat: float) -> float:
        base, _ = self.quote(amount_fiat)
        if not cfg.unique_amounts:
            return base
        for _ in range(60):
            c = round(base + random.randint(1, 899) / 10_000, 4)
            if not await db.amount_taken(c, "USDT"):
                return c
        return round(base + random.randint(900, 9999) / 10_000, 4)

    async def create(self, order) -> Invoice:
        addr = cfg.ton_address
        if order["kind"] == "topup" and not order["amount"]:
            return Invoice(
                text=("{{dep_tip}} You can send <b>any amount</b> — it will be added to your "
                      "balance.\n\n———————————————\n\n"
                      "{{dep_bank}} <b>USDT · TON</b>\n\n"
                      f"<code>{addr}</code>\n👆 <i>Tap to copy</i>\n\n"
                      "{{dep_warn}} Send <b>USDT on TON</b> only.\n\n"
                      "———————————————\n\n"
                      "After sending, paste your <b>transaction hash</b> here and "
                      "we'll verify it <b>automatically</b>."),
                pay_amount=0, pay_unit="USDT", pay_address=addr,
                qr_payload=f"ton://transfer/{addr}", manual_ref=True)
        amount = order["pay_amount"]
        return Invoice(
            text=(f"<b>Send exactly {amount} USDT</b> on <b>TON</b>\n\n"
                  f"<code>{addr}</code>\n\n"
                  "{{dep_warn}} The amount must match to the last decimal.\n"
                  "⚠️ Send USDT on the TON network only.\n"
                  f"{{{{dep_clock}}}} This order expires in {cfg.order_ttl} minutes.\n\n"
                  "Delivery is automatic once the transfer confirms."),
            pay_amount=amount, pay_unit="USDT", pay_address=addr,
            qr_payload=f"ton://transfer/{addr}?amount={amount}")

    async def _inbound(self) -> list[dict]:
        if not cfg.ton_address:
            return []
        headers = {"Authorization": f"Bearer {cfg.tonapi_key}"} if cfg.tonapi_key else {}
        url = f"{TONAPI}/v2/accounts/{cfg.ton_address}/jettons/history?limit=100"
        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json()
        except Exception as e:
            log.warning("tonapi request failed: %s", e)
            return []

        out = []
        for ev in (data or {}).get("events", []):
            ts = int(ev.get("timestamp", 0))
            for act in ev.get("actions", []):
                jt = act.get("JettonTransfer") or {}
                if not jt:
                    continue
                jetton = jt.get("jetton") or {}
                if (jetton.get("address") or "") and USDT_TON_MASTER[-8:] not in str(
                        jetton.get("address")):
                    continue                       # a different jetton, ignore
                recipient = (jt.get("recipient") or {}).get("address", "")
                if recipient and cfg.ton_address[-8:] not in recipient:
                    continue                       # outgoing, ignore
                dec = int(jetton.get("decimals", 6))
                try:
                    value = int(jt.get("amount", 0)) / (10 ** dec)
                except (TypeError, ValueError):
                    continue
                out.append({"id": str(ev.get("event_id", "")), "value": value, "ts": ts})
        return out

    async def verify_ref(self, ref: str) -> float | None:
        ref = ref.strip().lower()
        for tx in await self._inbound():
            if tx["id"].lower() == ref:
                return round(tx["value"] * cfg.usdt_rate, 2)
        return None

    async def diagnose(self) -> str:
        if not cfg.ton_address:
            return "TON_ADDRESS is not set"
        seen = await self._inbound()
        return (f"no inbound USDT jetton transfers found for {cfg.ton_address[:10]}…"
                if not seen else f"not among the last {len(seen)} transfers")

    async def poll(self, orders) -> list[tuple[int, str]]:
        if not orders or not cfg.ton_address:
            return []
        confirmed = []
        for tx in await self._inbound():
            for o in orders:
                if not o["pay_amount"] or abs(tx["value"] - float(o["pay_amount"])) > 5e-7:
                    continue
                created = _epoch(o["created_at"])
                if tx["ts"] and tx["ts"] < created - 180:
                    continue
                if await db.mark_seen(f"ton:{tx['id']}", o["id"]):
                    confirmed.append((o["id"], tx["id"]))
                break
        return confirmed


class LitecoinProvider:
    asks_amount = True
    """Native LTC, matched on a unique amount via Blockchair's public API."""

    group = "direct"
    variable_deposit = True
    code = "ltc"
    title = "Ł Litecoin (LTC)"
    unit = "LTC"
    network = "Litecoin"

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        rate = cfg.ltc_rate or 1
        return round(amount / rate, 6), "LTC"

    async def unique_amount(self, amount_fiat: float) -> float:
        base, _ = self.quote(amount_fiat)
        if not cfg.unique_amounts:
            return base
        for _ in range(60):
            c = round(base + random.randint(1, 899) / 1_000_000, 8)
            if not await db.amount_taken(c, "LTC"):
                return c
        return round(base + random.randint(900, 9999) / 1_000_000, 8)

    async def create(self, order) -> Invoice:
        addr = cfg.ltc_address
        if order["kind"] == "topup" and not order["amount"]:
            return Invoice(
                text=("{{dep_tip}} You can send <b>any amount</b> — it will be added to your "
                      "balance.\n\n———————————————\n\n"
                      "{{dep_bank}} <b>Litecoin (LTC)</b>\n\n"
                      f"<code>{addr}</code>\n👆 <i>Tap to copy</i>\n\n"
                      "———————————————\n\n"
                      "After sending, paste your <b>transaction id</b> here and "
                      "we'll verify it <b>automatically</b>."),
                pay_amount=0, pay_unit="LTC", pay_address=addr,
                qr_payload=f"litecoin:{addr}", manual_ref=True)
        amount = order["pay_amount"]
        return Invoice(
            text=(f"<b>Send exactly {amount} LTC</b>\n\n"
                  f"<code>{addr}</code>\n\n"
                  "{{dep_warn}} The amount must match exactly — that is how it's matched "
                  "to your order.\n"
                  f"{{{{dep_clock}}}} This order expires in {cfg.order_ttl} minutes.\n\n"
                  "Delivery is automatic after 1 confirmation."),
            pay_amount=amount, pay_unit="LTC", pay_address=addr,
            qr_payload=f"litecoin:{addr}?amount={amount}")

    async def _inbound(self) -> list[dict]:
        if not cfg.ltc_address:
            return []
        url = (f"{BLOCKCHAIR}/outputs?q=recipient({cfg.ltc_address})"
               "&limit=100&s=time(desc)")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json()
        except Exception as e:
            log.warning("blockchair request failed: %s", e)
            return []
        out = []
        for o in (data or {}).get("data", []):
            try:
                value = int(o.get("value", 0)) / 1e8          # litoshis -> LTC
            except (TypeError, ValueError):
                continue
            ts = 0
            if o.get("time"):
                try:
                    ts = int(datetime.strptime(o["time"], "%Y-%m-%d %H:%M:%S")
                             .replace(tzinfo=timezone.utc).timestamp())
                except ValueError:
                    ts = 0
            out.append({"id": str(o.get("transaction_hash", "")), "value": value, "ts": ts})
        return out

    async def verify_ref(self, ref: str) -> float | None:
        ref = ref.strip().lower()
        for tx in await self._inbound():
            if tx["id"].lower() == ref:
                return round(tx["value"] * (cfg.ltc_rate or 0), 2) or None
        return None

    async def diagnose(self) -> str:
        if not cfg.ltc_address:
            return "LTC_ADDRESS is not set"
        if not cfg.ltc_rate:
            return "LTC_RATE is not set, so LTC can't be priced"
        seen = await self._inbound()
        return (f"no inbound payments found for {cfg.ltc_address[:12]}…"
                if not seen else f"not among the last {len(seen)} payments")

    async def poll(self, orders) -> list[tuple[int, str]]:
        if not orders or not cfg.ltc_address:
            return []
        confirmed = []
        for tx in await self._inbound():
            for o in orders:
                if not o["pay_amount"] or abs(tx["value"] - float(o["pay_amount"])) > 5e-9:
                    continue
                if tx["ts"] and tx["ts"] < _epoch(o["created_at"]) - 900:
                    continue
                if await db.mark_seen(f"ltc:{tx['id']}", o["id"]):
                    confirmed.append((o["id"], tx["id"]))
                break
        return confirmed


RAZORPAY_API = "https://api.razorpay.com/v1"


class RazorpayProvider:
    """UPI, cards and netbanking through a Razorpay payment link.

    Razorpay owns the collection, so unlike a personal VPA it can tell us when
    the money lands. Two independent paths confirm an order — the webhook if
    one is configured, and a status poll otherwise — because a missed callback
    should delay a delivery, not lose it.
    """

    group = "wallet"
    variable_deposit = False
    asks_amount = False
    code = "razorpay"
    title = "🇮🇳 UPI / Card (Razorpay)"
    instant = False
    unit = "INR"

    @property
    def ready(self) -> bool:
        return bool(cfg.razorpay_key and cfg.razorpay_secret)

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        rate = cfg.upi_rate
        return (round(amount * rate, 2) if rate > 0 else 0.0), "INR"

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(cfg.razorpay_key, cfg.razorpay_secret)

    async def _api(self, method: str, path: str, payload: dict | None = None):
        try:
            async with aiohttp.ClientSession(auth=self._auth()) as s:
                async with s.request(method, f"{RAZORPAY_API}{path}", json=payload,
                                     timeout=aiohttp.ClientTimeout(total=25)) as r:
                    data = await r.json()
                    if r.status >= 400:
                        log.warning("razorpay %s %s -> %s", method, path, data)
                        return None
                    return data
        except Exception as e:
            log.warning("razorpay request failed: %s", e)
            return None

    async def create(self, order) -> Invoice:
        amount = float(order["pay_amount"] or 0)
        ref = f"ORD{order['code'] or order['id']}"
        link = await self._api("POST", "/payment_links", {
            "amount": int(round(amount * 100)),          # paise
            "currency": "INR",
            "accept_partial": False,
            "reference_id": ref,
            "description": f"{order['product_name']} x{order['qty']}",
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"order_id": str(order["id"]), "ref": ref},
        })
        if not link or not link.get("short_url"):
            return Invoice(
                text=("⚠️ Couldn't reach Razorpay just now. Please pick another "
                      "payment method, or try again in a moment."),
                pay_amount=amount, pay_unit="INR", manual_ref=False)

        # the link id is what the poller checks later
        await db.set_order(order["id"], external_ref=link["id"])
        shown = (f"\n<i>({cfg.money(order['amount'])} at ₹{cfg.upi_rate:g} "
                 f"per {cfg.fiat})</i>" if cfg.fiat.upper() != "INR" else "")
        return Invoice(
            text=(f"<b>Pay ₹{amount:.2f}</b>{shown}\n\n"
                  "Tap <b>Pay now</b> to open Razorpay. UPI, cards and netbanking "
                  "all work.\n\n"
                  f"Reference: <code>{ref}</code>\n\n"
                  "<i>Your order is delivered automatically the moment Razorpay "
                  "confirms the payment — nothing to paste.</i>"),
            pay_amount=amount, pay_unit="INR",
            pay_url=link["short_url"], qr_payload=link["short_url"])

    async def verify_ref(self, ref: str) -> float | None:
        """Accepts a payment link id or a payment id, for a buyer who pastes one."""
        ref = ref.strip()
        path = ("/payment_links/" + ref) if ref.startswith("plink_") else ("/payments/" + ref)
        data = await self._api("GET", path)
        if not data:
            return None
        paid = str(data.get("status", "")).lower() in {"paid", "captured"}
        if not paid:
            return None
        rupees = float(data.get("amount_paid") or data.get("amount") or 0) / 100
        rate = cfg.upi_rate or 1
        return round(rupees / rate, 2)

    async def diagnose(self) -> str:
        if not self.ready:
            return "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set"
        if cfg.upi_rate <= 0:
            return "no rupee rate — set INR_RATE or SECOND_CURRENCY=INR with SECOND_RATE"
        probe = await self._api("GET", "/payment_links?count=1")
        return ("the API rejected the key — check it's the live key pair and not "
                "a test one" if probe is None else "payment not found against this link")

    async def poll(self, orders) -> list[tuple[int, str]]:
        """Ask Razorpay about each open link.

        This is what makes the rail work without a webhook, and what saves an
        order when a callback is missed.
        """
        if not orders or not self.ready:
            return []
        confirmed = []
        for o in orders:
            plink = (o["external_ref"] or "")
            if not plink.startswith("plink_"):
                continue
            data = await self._api("GET", f"/payment_links/{plink}")
            if not data or str(data.get("status", "")).lower() != "paid":
                continue
            ref = plink
            for pay in (data.get("payments") or []):
                if str(pay.get("status", "")).lower() == "captured":
                    ref = pay.get("payment_id") or pay.get("id") or plink
                    break
            if await db.mark_seen(f"rzp:{ref}", o["id"]):
                confirmed.append((o["id"], ref))
        return confirmed


# ---------------------------------------------------- manual transfers
# Rails where the buyer sends to an account id you own (Binance Pay, Bybit,
# bKash, PayPal...). There is no public ledger to poll, so the buyer pastes a
# TxID / Order ID and an admin confirms it — one tap in the panel.
MANUAL_RAILS = {
    "binance": dict(title="◈ Binance Pay", heading="Binance Pay / Internal Transfer",
                    label="Binance ID"),
    "bybit":   dict(title="⬡ Bybit Pay",   heading="Bybit Pay / Internal Transfer",
                    label="Bybit UID"),
    "bkash":   dict(title="⬢ bKash",       heading="bKash / Send Money",
                    label="bKash number"),
    "paypal":  dict(title="◐ PayPal",      heading="PayPal / Friends & Family",
                    label="PayPal email"),
}
RAIL_ACCOUNTS: dict[str, str] = {}      # code -> account id, filled from settings


async def reload_rails() -> None:
    RAIL_ACCOUNTS.clear()
    for code in MANUAL_RAILS:
        v = (await db.setting(f"rail:{code}", "")).strip()
        if v:
            RAIL_ACCOUNTS[code] = v


BINANCE_API = "https://api.binance.com"


class BinancePayProvider:
    """Binance Pay / C2C transfers to your Binance ID, verified automatically.

    A read-only API key on the *receiving* account lets us poll
    GET /sapi/v1/pay/transactions, which lists incoming Pay and C2C transfers
    with a transaction id and a signed amount (positive = money in). The buyer
    pastes their Transaction ID and we confirm it against that ledger, so the
    amount credited comes from Binance rather than from what the buyer claims.

    Without API credentials this behaves exactly like any other manual rail:
    the reference goes to your review queue instead.
    """

    group = "direct"
    variable_deposit = True

    def __init__(self, code: str, spec: dict):
        self.code = code
        self.title = spec["title"]
        self.heading = spec["heading"]
        self.label = spec["label"]
        self.unit = cfg.fiat

    @property
    def automatic(self) -> bool:
        return bool(cfg.binance_key and cfg.binance_secret)

    # An internal transfer has no address to reserve an amount against, so the
    # buyer sends whatever they like and pastes the Transaction ID. With API
    # keys that paste is checked against Binance and credited for the amount
    # Binance reports — automatic, just triggered by the paste rather than a poll.
    asks_amount = False

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        return round(amount, 2), cfg.fiat

    async def create(self, order) -> Invoice:
        account = RAIL_ACCOUNTS.get(self.code, "not configured")
        lines = [
            "{{dep_tip}} You can send <b>any amount</b> — it will be added to your balance.",
            "", "———————————————", "",
            f"{{{{dep_bank}}}} <b>{self.heading}</b>", "",
            f"<b>{self.label}:</b>", f"<code>{account}</code>",
            "{{dep_point}} <i>Tap to copy</i>", "", "———————————————", "",
        ]
        lines.append(
            "After sending, paste your <b>Transaction ID</b> here and we'll verify "
            "it <b>automatically</b>." if self.automatic else
            "After sending, paste your <b>Transaction ID</b> or <b>Order ID</b> here "
            "and we'll confirm it.")
        return Invoice(text="\n".join(lines), pay_amount=order["amount"] or 0,
                       pay_unit=cfg.fiat, pay_address=account, manual_ref=True)

    # ---- signed Binance request -------------------------------------------
    async def _signed_get(self, path: str, params: dict) -> dict | None:
        import hashlib
        import hmac as _hmac
        import time as _time
        import urllib.parse as _url

        params = {**params, "timestamp": int(_time.time() * 1000), "recvWindow": 10_000}
        qs = _url.urlencode(params)
        sig = _hmac.new(cfg.binance_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{BINANCE_API}{path}?{qs}&signature={sig}"
        try:
            async with aiohttp.ClientSession(
                    headers={"X-MBX-APIKEY": cfg.binance_key}) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    data = await r.json()
        except Exception as e:
            log.warning("binance request failed: %s", e)
            return None
        if isinstance(data, dict) and data.get("code") not in (None, "000000", 200):
            log.warning("binance API error: %s", data)
            return None
        return data

    async def _inbound(self) -> list[dict]:
        """Incoming Pay/C2C transfers, newest first."""
        if not self.automatic:
            return []
        data = await self._signed_get("/sapi/v1/pay/transactions", {"limit": 100})
        out = []
        for tx in (data or {}).get("data", []):
            try:
                amount = float(tx.get("amount", 0))
            except (TypeError, ValueError):
                continue
            if amount <= 0:                     # negative is money leaving, ignore
                continue
            # The app shows a different reference depending on where you look —
            # Transaction ID, Order ID, or the order number on a Pay receipt.
            # Accept any of them rather than making the buyer find the right one.
            ids = {str(tx.get(f, "")).strip()
                   for f in ("transactionId", "orderId", "transactionScene",
                             "payerInfo", "orderNo", "id")
                   if isinstance(tx.get(f), (str, int)) and str(tx.get(f)).strip()}
            out.append({"id": str(tx.get("transactionId", "")),
                        "ids": ids,
                        "value": amount,
                        "currency": (tx.get("currency") or "").upper(),
                        "ts": int(tx.get("transactionTime", 0))})
        return out

    def _to_fiat(self, tx: dict) -> float:
        """Stablecoins convert at the configured USDT rate; a payment already in
        the shop currency is taken at face value."""
        cur = tx["currency"]
        if cur in {cfg.fiat.upper(), (cfg.symbol or "").upper()}:
            return round(tx["value"], 2)
        if cur in {"USDT", "USDC", "BUSD", "FDUSD"}:
            return round(tx["value"] * cfg.usdt_rate, 2)
        return 0.0                              # unknown asset -> needs a human

    async def verify_ref(self, ref: str) -> float | None:
        ref = ref.strip().lstrip("#").lower()
        for tx in await self._inbound():
            known = {i.lower() for i in tx.get("ids", set()) if i}
            if ref and ref in known:
                return self._to_fiat(tx) or None
        return None

    async def diagnose(self) -> str:
        if not self.automatic:
            return ("no Binance API key set — this rail confirms manually. Add "
                    "BINANCE_API_KEY and BINANCE_API_SECRET (read-only) to automate it")
        seen = await self._inbound()
        if not seen:
            return ("the API returned no incoming Pay/C2C transfers — check the key "
                    "belongs to the account that RECEIVES the money and has "
                    "Reading enabled")
        # show what the API actually calls these, so a mismatch is obvious
        sample = ", ".join(sorted(seen[0].get("ids", set()))[:3]) or "none"
        return (f"not among the last {len(seen)} incoming transfers. The most "
                f"recent one is identified as: {sample}")

    async def poll(self, orders) -> list[tuple[int, str]]:
        """Fixed-amount orders settle without the buyer pasting anything."""
        if not orders or not self.automatic:
            return []
        confirmed = []
        for tx in await self._inbound():
            fiat = self._to_fiat(tx)
            if not fiat:
                continue
            for o in orders:
                if not o["amount"] or abs(fiat - float(o["amount"])) > 0.009:
                    continue
                if tx["ts"] and tx["ts"] / 1000 < _epoch(o["created_at"]) - 180:
                    continue
                if await db.mark_seen(f"binance:{tx['id']}", o["id"]):
                    confirmed.append((o["id"], tx["id"]))
                break
        return confirmed


def _epoch(created_at: str) -> float:
    return datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()


class ManualTransferProvider:
    asks_amount = False
    group = "direct"
    variable_deposit = True
    instant = False

    def __init__(self, code: str, spec: dict):
        self.code = code
        self.title = spec["title"]
        self.heading = spec["heading"]
        self.label = spec["label"]
        self.unit = cfg.fiat

    @staticmethod
    def quote(amount: float) -> tuple[float, str]:
        return round(amount, 2), cfg.fiat

    async def create(self, order) -> Invoice:
        account = RAIL_ACCOUNTS.get(self.code, "not configured")
        variable = order["kind"] == "topup"
        lines = []
        if variable:
            lines.append("{{dep_tip}} You can send <b>any amount</b> — it will be added to "
                         "your balance.")
        else:
            lines.append(f"💰 Send exactly <b>{cfg.money(order['amount'])}</b>.")
        lines += ["", "———————————————", "",
                  f"{{{{dep_bank}}}} <b>{self.heading}</b>", "",
                  f"<b>{self.label}:</b>", f"<code>{account}</code>",
                  "{{dep_point}} <i>Tap to copy</i>", "",
                  "———————————————", "",
                  "After sending, paste your <b>Transaction Hash (TxID)</b> or "
                  "<b>Order ID</b> here and we'll confirm it."]
        return Invoice(text="\n".join(lines), pay_amount=order["amount"] or 0,
                       pay_unit=cfg.fiat, pay_address=account, manual_ref=True)

    async def poll(self, orders) -> list[tuple[int, str]]:
        return []       # confirmed by an admin, not by a chain


REGISTRY: dict[str, object] = {
    p.code: p() for p in (BalanceProvider, StarsProvider, CryptoProvider, UpiProvider)
}
for _code, _spec in EVM_CHAINS.items():
    REGISTRY[_code] = EvmTokenProvider(_code, _spec)
REGISTRY["razorpay"] = RazorpayProvider()
REGISTRY["ton"] = TonJettonProvider()
REGISTRY["ltc"] = LitecoinProvider()
for _code, _spec in MANUAL_RAILS.items():
    REGISTRY[_code] = (BinancePayProvider if _code == "binance"
                       else ManualTransferProvider)(_code, _spec)


def is_variable(code: str) -> bool:
    """Rails that accept any amount rather than an exact one."""
    return bool(getattr(REGISTRY.get(code), "variable_deposit", False))


def deposit_rails() -> list:
    return [p for p in enabled() if p.code != "balance"]


def groups() -> dict[str, list]:
    """Enabled providers bucketed by menu group, preserving configured order."""
    out: dict[str, list] = {}
    for p in enabled():
        out.setdefault(getattr(p, "group", "wallet"), []).append(p)
    return out


def _ready(code: str) -> bool:
    """A rail with nothing to pay into is worse than a missing button."""
    needs = {
        "crypto": cfg.tron_address,
        "ton": cfg.ton_address,
        "ltc": cfg.ltc_address and cfg.ltc_rate,
        "upi": cfg.upi_vpa,
        "razorpay": cfg.razorpay_key and cfg.razorpay_secret and cfg.upi_rate > 0,
        **{c: cfg.evm_address and cfg.etherscan_key for c in EVM_CHAINS},
        **{c: RAIL_ACCOUNTS.get(c) for c in MANUAL_RAILS},
    }
    return bool(needs.get(code, True))


def enabled() -> list:
    """Rails offered at checkout.

    Wallet balance is always included: it isn't a payment rail you switch on,
    it's where deposits land. Leaving it out of ENABLED_PROVIDERS would let
    people fund a wallet they could never spend.
    """
    codes = list(cfg.providers)
    if "balance" not in codes:
        codes.insert(0, "balance")
    return [REGISTRY[c] for c in codes if c in REGISTRY and _ready(c)]


def misconfigured() -> list[str]:
    return [c for c in cfg.providers if c in REGISTRY and not _ready(c)]


def requirement(code: str) -> str:
    """What's missing before this rail can be offered, in plain words."""
    if code in MANUAL_RAILS:
        return f"{MANUAL_RAILS[code]['label']} — set it in this screen"
    return {
        "crypto": "TRON_ADDRESS in .env",
        "razorpay": "RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET and a rupee rate in .env",
        "ton": "TON_ADDRESS in .env",
        "ltc": "LTC_ADDRESS and LTC_RATE in .env",
        "upi": "UPI_VPA in .env",
        **{c: "EVM_ADDRESS and ETHERSCAN_API_KEY in .env"
           for c in EVM_CHAINS},
    }.get(code, "")


def status() -> list[dict]:
    """Every enabled rail with whether it's usable and why not."""
    out = []
    for code in cfg.providers:
        prov = REGISTRY.get(code)
        if not prov:
            out.append({"code": code, "title": code, "ready": False,
                        "need": "unknown rail — check the spelling"})
            continue
        ready = _ready(code)
        auto = hasattr(prov, "verify_ref") or code in ("balance", "stars")
        # an explorer plan restriction only forces manual approval when the
        # node fallback isn't working either
        blocked = (getattr(prov, "plan_blocked", False)
                   and not getattr(prov, "rpc_ok", False))
        out.append({"code": code, "title": prov.title, "ready": ready,
                    "need": "" if ready else requirement(code),
                    "auto": auto and not blocked,
                    "manual_only": blocked,
                    "via": ("node" if getattr(prov, "rpc_ok", False)
                            and getattr(prov, "plan_blocked", False) else "")})
    return out


async def probe() -> None:
    """Ask each explorer once at startup, so the admin learns about a plan
    restriction from /status rather than from a buyer's failed payment."""
    for prov in enabled():
        if hasattr(prov, "_inbound"):
            try:
                await prov._inbound()
            except Exception:
                pass


# Where a buyer can see their own transaction. A payment they can verify
# themselves is a payment they don't open a support ticket about.
EXPLORERS = {
    "crypto":   "https://tronscan.org/#/transaction/{tx}",
    "bep20":    "https://bscscan.com/tx/{tx}",
    "erc20":    "https://etherscan.io/tx/{tx}",
    "polygon":  "https://polygonscan.com/tx/{tx}",
    "arbitrum": "https://arbiscan.io/tx/{tx}",
    "base":     "https://basescan.org/tx/{tx}",
    "ton":      "https://tonviewer.com/transaction/{tx}",
    "ltc":      "https://blockchair.com/litecoin/transaction/{tx}",
}

# Chain shown to the buyer, as they'd recognise it from their wallet.
NETWORKS = {
    "crypto":   "Tron (TRC-20)",
    "bep20":    "BNB Smart Chain",
    "erc20":    "Ethereum",
    "polygon":  "Polygon",
    "arbitrum": "Arbitrum",
    "base":     "Base",
    "ton":      "TON",
    "ltc":      "Litecoin",
    "balance":  "Wallet balance",
    "stars":    "Telegram Stars",
    "upi":      "UPI",
    "razorpay": "Razorpay",
    "binance":  "Binance Pay",
    "bybit":    "Bybit Pay",
    "bkash":    "bKash",
    "paypal":   "PayPal",
}


def explorer_url(code: str, txid: str) -> str:
    """Public link to a transaction, or '' when the rail has no explorer.

    Only hash-shaped references get a link: manual rails store whatever the
    buyer typed in the same field, and linking that produces a dead URL.
    """
    tx = (txid or "").strip()
    tpl = EXPLORERS.get(code, "")
    if not tpl or not tx or len(tx) < 16 or " " in tx:
        return ""
    return tpl.format(tx=tx)


def network_label(code: str) -> str:
    """Human name of the chain or rail a payment arrived on."""
    if code in NETWORKS:
        return NETWORKS[code]
    chain = EVM_CHAINS.get(code)
    if chain:
        return chain["network"]
    p = get(code)
    return getattr(p, "title", code) if p else code


def get(code: str):
    return REGISTRY.get(code)
