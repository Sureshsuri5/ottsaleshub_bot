"""Watch-only HD wallet: one fresh deposit address per order.

The server holds an **xpub**, never a seed. That is enough to derive every
address and watch it arrive, and not enough to move a single token. If this box
is ever compromised the attacker gets a list of addresses and nothing more.

Addresses come from the standard Ethereum path, `m/44'/60'/0'/0/i`, so the
seed can be restored in MetaMask, Trust, Ledger or anything else that speaks
BIP44 — you are never dependent on this code to reach your own money.

The index only ever moves forward (see `db.next_deriv_index`), so an address is
issued to exactly one order, once, and an expired order's address is never
handed to anybody else.

EVM addresses are chain-independent: the same derived address receives on BSC,
Polygon, Ethereum, Arbitrum and Base. One address per order covers every EVM
rail the shop offers.
"""
from __future__ import annotations

import logging

from config import cfg

log = logging.getLogger(__name__)

PATH = "m/44'/60'/0'/0/{i}"

_acct = None          # cached Bip44 object built from the xpub
_error = ""           # why it isn't usable, for /status


def _load():
    """Build the watch-only account once. Failures are remembered, not raised:
    a bad xpub must not take the bot down, it must make the rail unavailable
    and say why."""
    global _acct, _error
    if _acct is not None or _error:
        return _acct
    xpub = (cfg.evm_xpub or "").strip()
    if not xpub:
        _error = "EVM_XPUB is not set"
        return None
    try:
        from bip_utils import Bip44, Bip44Coins
        _acct = Bip44.FromExtendedKey(xpub, Bip44Coins.ETHEREUM)
        # Prove it derives before anything relies on it. An xpub taken from the
        # wrong depth parses fine and then yields addresses whose keys the
        # owner's seed cannot reach — which is only discovered once a buyer has
        # already sent money there.
        _acct.AddressIndex(0).PublicKey().ToAddress()
    except ImportError:
        _error = "bip_utils is not installed"
        _acct = None
    except Exception as e:
        _error = f"EVM_XPUB is not a usable account xpub ({type(e).__name__})"
        _acct = None
    return _acct


def ready() -> bool:
    return _load() is not None


def problem() -> str:
    """Why per-order addresses are off, in words an admin can act on."""
    _load()
    return _error


def address(index: int) -> str:
    """The deposit address for one derivation index.

    Raises rather than returning a fallback: quietly handing back the shop's
    main address would mix an unattributable payment into the shared wallet,
    which is the exact problem per-order addresses exist to remove.
    """
    acct = _load()
    if acct is None:
        raise RuntimeError(f"HD wallet unavailable: {_error}")
    if index < 0:
        raise ValueError("derivation index must not be negative")
    return acct.AddressIndex(index).PublicKey().ToAddress()


def preview(count: int = 3) -> list[str]:
    """First few addresses, for checking against your own wallet.

    Import the seed into MetaMask and compare. If these don't match, stop and
    fix the xpub before taking a single payment.
    """
    if not ready():
        return []
    return [address(i) for i in range(count)]
