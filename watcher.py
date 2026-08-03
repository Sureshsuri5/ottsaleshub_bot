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

    for oid in await db.expire_stale():
        o = await db.order(oid)
        try:
            import texts
            await bot.send_message(o["user_id"],
                                   await texts.t("order_expired", oid=oid))
        except Exception:
            pass

    for prov in payments.enabled():
        orders = await db.open_orders(prov.code)
        if not orders:
            continue
        for oid, ref in await prov.poll(orders):
            log.info("confirmed order %s via %s (%s)", oid, prov.code, ref)
            await delivery.settle(bot, oid, ref)
