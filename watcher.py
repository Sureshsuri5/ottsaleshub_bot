"""Background loop: polls each pollable provider and expires stale orders."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

import db
import delivery
import payments
from config import cfg

log = logging.getLogger(__name__)


async def run(bot: Bot) -> None:
    log.info("payment watcher started (every %ss)", cfg.poll_interval)
    while True:
        try:
            await _tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watcher tick failed")
        await asyncio.sleep(cfg.poll_interval)


_last_prune = 0.0


async def _tick(bot: Bot) -> None:
    global _last_prune
    import time
    if cfg.keep_dead_orders and time.time() - _last_prune > 3600:
        _last_prune = time.time()
        removed = await db.prune_dead_orders(cfg.keep_dead_orders)
        if removed:
            log.info("pruned %s abandoned order(s) older than %s days",
                     removed, cfg.keep_dead_orders)

    # buyers who sent too little hear about it in the same pass
    try:
        await delivery.notify_restock(bot)
    except Exception:
        log.exception("could not send restock alerts")

    try:
        await delivery.notify_underpaid(bot)
    except Exception:
        log.exception("could not send part-payment notices")

    for oid in await db.expire_stale():
        # an expired order was holding wallet balance it never spent
        await db.release_balance(oid)
        o = await db.order(oid)
        try:
            import texts
            await bot.send_message(o["user_id"],
                                   await texts.t("order_expired", oid=oid))
        except Exception:
            pass

    async def _poll_one(prov) -> list[tuple[int, str]]:
        """One rail's poll, isolated.

        Rails were polled one after another, so the slowest set the pace for
        all of them — a chain whose public nodes are timing out could hold up
        verification on a chain that was answering instantly. Each now runs on
        its own, and one that hangs is abandoned for this tick rather than
        allowed to stall the cycle.
        """
        try:
            orders = await db.open_orders(prov.code)
            if not orders:
                return []
            return await asyncio.wait_for(prov.poll(orders), timeout=cfg.poll_timeout)
        except asyncio.TimeoutError:
            log.warning("rail '%s' did not answer within %ss — skipped this tick",
                        prov.code, cfg.poll_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll failed for rail '%s'", prov.code)
        return []

    rails = payments.enabled()
    results = await asyncio.gather(*(_poll_one(p) for p in rails))

    # Settle in sequence: delivery allocates stock, and serialising it here
    # keeps two confirmations in the same tick from racing for the same units.
    for prov, found in zip(rails, results):
        for oid, ref in found:
            log.info("confirmed order %s via %s (%s)", oid, prov.code, ref)
            await delivery.settle(bot, oid, ref)
