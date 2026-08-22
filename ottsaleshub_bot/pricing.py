"""What a given buyer pays.

Three levers, in priority order:

1. a price set for that buyer on that product         (user_prices)
2. an exact price set for that product on that tier   (tier_prices)
3. otherwise the list price minus the tier's discount

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


async def price_for(product, uid: int | None = None, tier_id: int | None = None,
                    overrides: dict[int, float] | None = None) -> float:
    """Price of one unit for this buyer.

    Three levers now, in priority order: a price set for this buyer on this
    product, then the tier's exact price, then the tier discount off list.

    The per-user price wins outright and is not discounted further — it is a
    figure someone agreed with this customer, so stacking a tier percentage on
    top would quietly charge something nobody chose.

    `overrides` lets a caller pricing a whole list pass the buyer's custom
    prices once instead of this function fetching them per product.
    """
    if uid is not None:
        if overrides is None:
            overrides = await db.user_prices(uid)
        exact_user = overrides.get(product["id"])
        if exact_user is not None:
            return round(exact_user, 2)

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
    """Prices for a whole list in one pass — used by the catalogue screens.

    Both the tier and the buyer's overrides are resolved once here. Passing
    only tier_id down, as this used to, meant a custom price showed correctly
    at checkout but not on the shelf — the buyer saw one number and was charged
    another, which reads as a bug in the shop even though the charge was right.
    """
    tier_id = await user_tier_id(uid) if uid is not None else None
    overrides = await db.user_prices(uid) if uid is not None else {}
    return {p["id"]: await price_for(p, uid=uid, tier_id=tier_id,
                                     overrides=overrides) for p in products}


async def label(uid: int) -> str:
    """Short badge for the buyer's tier, empty for standard pricing."""
    t = await db.tier(await user_tier_id(uid))
    if not t:
        return ""
    return f"{t['name']}" + (f" · −{t['discount']:g}%" if t["discount"] else "")
