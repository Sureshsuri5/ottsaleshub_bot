"""What a given buyer pays.

Two levers, in priority order:

1. an exact price set for that product on that tier  (tier_prices)
2. otherwise the list price minus the tier's discount

Everything the buyer sees and everything they're charged goes through
`price_for`, so a stale screen can never turn into a wrong charge — the amount
is recomputed server-side when the order is created.
"""
from __future__ import annotations

import db


async def user_tier_id(uid: int) -> int | None:
    u = await db.get_user(uid)
    return u["tier_id"] if u else None


def apply(list_price: float, discount: float) -> float:
    return round(list_price * (1 - (discount or 0) / 100), 2)


async def price_for(product, uid: int | None = None, tier_id: int | None = None) -> float:
    """Price of one unit for this buyer."""
    if uid is not None and tier_id is None:
        tier_id = await user_tier_id(uid)
    if tier_id is None:
        return round(product["price"], 2)

    exact = (await db.tier_prices(product["id"])).get(tier_id)
    if exact is not None:
        return round(exact, 2)
    t = await db.tier(tier_id)
    return apply(product["price"], t["discount"] if t else 0)


async def price_map(products, uid: int | None = None) -> dict[int, float]:
    """Prices for a whole list in one pass — used by the catalogue screens."""
    tier_id = await user_tier_id(uid) if uid is not None else None
    return {p["id"]: await price_for(p, tier_id=tier_id) for p in products}


async def label(uid: int) -> str:
    """Short badge for the buyer's tier, empty for standard pricing."""
    t = await db.tier(await user_tier_id(uid))
    if not t:
        return ""
    return f"{t['name']}" + (f" · −{t['discount']:g}%" if t["discount"] else "")
