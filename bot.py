from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, ErrorEvent, Message,
                           TelegramObject, Update)
from aiogram.types import (BotCommand, BotCommandScopeChat,
                           BotCommandScopeAllPrivateChats, MenuButtonWebApp,
                           MenuButtonCommands, WebAppInfo)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import db
import flair
import handlers_admin
import handlers_group
import handlers_user
import watcher
import webapp
from config import cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("shopbot")


class MaintenanceMiddleware(BaseMiddleware):
    """Holds the shop closed while an admin works on it.

    Runs before everything else, so no handler fires and nothing can be bought,
    deposited or withdrawn. Admins pass through untouched — you need the panel
    working to turn it back off.

    The payment watcher deliberately keeps running underneath. Someone who
    paid a minute before you flipped the switch still gets their goods; taking
    the poller down would leave real money confirmed on-chain and undelivered.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        if await db.setting("maintenance", "0") != "1":
            return await handler(event, data)
        user = data.get("event_from_user")
        if user and cfg.is_admin(user.id):
            return await handler(event, data)
        import texts
        inner = event.event
        # Groups get silence, not a notice. The shop being closed is between
        # the bot and its buyers; announcing it to a chat full of people who
        # merely mentioned a product name is noise in someone else's room.
        chat = getattr(inner, "chat", None)
        if chat is not None and getattr(chat, "type", "private") != "private":
            return None
        note = await texts.t("maintenance")
        if isinstance(inner, CallbackQuery):
            await inner.answer(note[:190], show_alert=True)
        elif isinstance(inner, Message):
            await inner.answer(note)
        return None


class BanMiddleware(BaseMiddleware):
    """Registers every user and blocks banned ones before handlers run."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and not user.is_bot:
            row = await db.upsert_user(user.id, user.username, user.first_name)
            if row["is_banned"] and not cfg.is_admin(user.id):
                inner = event.event
                if isinstance(inner, CallbackQuery):
                    await inner.answer("You are banned from this shop.", show_alert=True)
                elif isinstance(inner, Message):
                    import texts
                    await inner.answer(await texts.t("banned"))
                return None
        return await handler(event, data)


EMOJI_ERRORS = ("custom_emoji", "emoji", "icon")


async def on_error(event: ErrorEvent) -> bool:
    """aiogram hands this a single ErrorEvent carrying the update and the error."""
    exception = event.exception
    msg = str(exception).lower()

    # Telegram refuses button icons unless the bot owner has Premium (or the bot
    # owns a Fragment username). Drop icons for the rest of the run rather than
    # letting a Buy button fail.
    if any(w in msg for w in EMOJI_ERRORS) and "not found" not in msg:
        flair.disable_icons(str(exception))
        return True

    # Anything else is a real bug. Log it and tell the admin who hit it, rather
    # than leaving them tapping a command that silently does nothing.
    log.exception("unhandled update error: %s", exception)
    try:
        upd = event.update
        inner = upd.message or upd.callback_query
        user = inner.from_user if inner else None
        if user and cfg.is_admin(user.id):
            # to the admin privately — never back into whatever chat it happened in
            await upd.bot.send_message(
                user.id,
                "⚠️ That action failed:\n"
                f"<code>{type(exception).__name__}: {str(exception)[:300]}</code>")
    except Exception:
        pass
    return True


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.errors.register(on_error)
    dp.update.middleware(MaintenanceMiddleware())
    dp.update.middleware(BanMiddleware())
    # The shop is a private-chat experience. In a group the bot only posts the
    # sales feed, and a menu carrying a Mini App button is rejected there
    # outright (BUTTON_TYPE_INVALID), so don't answer group messages at all.
    for r in (handlers_admin.router, handlers_user.router):
        r.message.filter(F.chat.type == ChatType.PRIVATE)
        r.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    dp.include_router(handlers_admin.router)   # admin first: its filter narrows it
    dp.include_router(handlers_user.router)
    dp.include_router(handlers_group.router)   # groups: product mentions only
    return dp


USER_COMMANDS = [
    ("start", "Open the shop"),
    ("menu", "Main menu"),
    ("order", "Look up an order by its ID"),
]
# Not published to Telegram — see _publish_commands(). Kept as the one place
# that lists what an admin can type, so it doesn't drift out of the README.
ADMIN_COMMANDS = USER_COMMANDS + [
    ("admin", "Admin panel"),
    ("status", "Bot status and diagnostics"),
    ("check", "Test a transaction hash"),
    ("texts", "Edit bot messages"),
    ("flair", "Button icons"),
    ("ids", "Get sticker / emoji ids"),
    ("backup", "Download a full database backup"),
    ("wallet", "Which deposit accounts hold funds"),
    ("emojitest", "Check premium emoji against Telegram"),
    ("announcetest", "Check group announcement setup"),
]


async def _publish_commands(bot: Bot) -> None:
    """Publish the buyer command list, and only that.

    Admin commands are deliberately not registered anywhere. They all work
    exactly as before — typing /admin or /backup still does — but they stay off
    the menu beside the chat box, which is a list buyers can also see the shape
    of. Fewer things on the menu also means the three commands that matter to a
    buyer aren't buried under ten that don't.
    """
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in USER_COMMANDS],
            scope=BotCommandScopeAllPrivateChats())
        # clear any admin list published by an earlier version, or it lingers
        # in Telegram's cache for those chats indefinitely
        for admin_id in cfg.admin_ids:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception as e:
        log.warning("could not publish the command list: %s", e)


async def _publish_menu_button(bot: Bot) -> None:
    """Put the shop on the blue button beside the message box.

    Only when the URL is public https — Telegram rejects anything else, and a
    failed call would leave whatever was set before in place.
    """
    # The button beside the chat box: either it opens the Mini App, or it lists
    # the bot's commands. It can't do both, so which one is a shop decision
    # rather than a fixed choice — the Mini App is still one tap away on the
    # menu itself.
    want_app = await db.setting("menu_button", "commands") == "app"
    try:
        if want_app and cfg.miniapps_live and flair.MINIAPP_ON:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text=cfg.shop_name[:16] or "Shop",
                    web_app=WebAppInfo(url=cfg.webapp_url)))
            log.info("menu button opens the Mini App")
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            log.info("menu button lists commands")
    except Exception as e:
        log.warning("could not set the menu button: %s", e)


async def keepalive() -> None:
    """Hit our own /health on a timer so the platform sees inbound traffic.

    Only useful on a host that sleeps when idle, and only possible when there's
    a public URL to hit. Failures are logged at debug: a missed ping is not
    worth a line in the log every few minutes.
    """
    import aiohttp
    url = f"{cfg.webapp_url}/health"
    log.info("keepalive pinging %s every %s min", url, cfg.keepalive)
    while True:
        await asyncio.sleep(cfg.keepalive * 60)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    log.debug("keepalive -> %s", r.status)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("keepalive failed: %s", e)


async def main() -> None:
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN is missing — copy .env.example to .env and fill it in.")
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS is empty — nobody can open the admin panel.")

    await db.init(cfg.db_path)
    filled = await db.backfill_codes()
    if filled:
        log.info('assigned reference codes to %s existing order(s)', filled)

    # Link previews are off by default: a channel or group link in the welcome
    # text would otherwise render a large preview card that pushes the buttons
    # off screen. Individual sends can still opt back in.
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = build_dispatcher()

    me = await bot.get_me()
    flair.BOT_USERNAME = me.username or ""
    await flair.reload()
    import payments
    await payments.reload_rails()

    # Publish the command list. Typing "/" then shows exactly what this build
    # supports — the fastest way to spot that an old copy is running.
    await _publish_commands(bot)
    await _publish_menu_button(bot)

    tasks = [asyncio.create_task(watcher.run(bot))]
    if cfg.keepalive > 0 and cfg.webapp_url.startswith("https://"):
        tasks.append(asyncio.create_task(keepalive()))
    elif cfg.keepalive > 0:
        log.warning("KEEPALIVE_MINUTES is set but there's no public https URL to ping.")
    app = webapp.build_app(bot) if cfg.webapp_enabled else None
    runner = None

    import payments
    live = [p.code for p in payments.enabled()]
    log.info("running as @%s | providers: %s | mode: %s",
             me.username, ", ".join(live) or "none", "webhook" if cfg.use_webhook else "polling")
    warn = cfg.rate_warning()
    if warn:
        log.warning("=" * 68)
        log.warning("CHECK YOUR CONFIG: %s", warn)
        log.warning("=" * 68)

    await payments.probe()   # warms plan/RPC state; failures surface in /check
    for r in payments.status():
        if r.get("manual_only"):
            log.warning("rail '%s' works but cannot auto-verify on your current "
                        "API plan — payments there need manual approval", r["code"])
        elif r.get("via") == "node":
            log.info("rail '%s' verifies via a public %s node "
                     "(explorer plan doesn't cover this chain)",
                     r["code"], r["title"].split()[-1])
    for code in payments.misconfigured():
        log.warning("payment rail '%s' is enabled but has no address/account set — "
                    "hidden until you configure it", code)
    if cfg.webapp_enabled and not cfg.miniapps_live:
        log.warning("WEBAPP_URL is not a public https:// address — Mini App buttons are "
                    "hidden and the bot falls back to the inline admin panel.")

    try:
        if cfg.use_webhook:
            if app is None:
                raise SystemExit("Webhook mode needs WEBAPP_ENABLED=true.")
            # Secret path + Telegram's own secret header: two independent checks that
            # an update really came from Telegram and not from someone port-scanning.
            secret = cfg.webhook_secret_tg or secrets.token_urlsafe(24)
            path = f"/tg/{secrets.token_hex(8)}" if not cfg.webhook_secret_tg else "/tg/updates"
            SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path)
            setup_application(app, dp, bot=bot)
            runner = await webapp.serve(app)
            try:
                await bot.set_webhook(
                    f"{cfg.webapp_url}{path}", secret_token=secret,
                    drop_pending_updates=True,
                    allowed_updates=dp.resolve_used_update_types())
            except Exception as e:
                # A dead tunnel or a typo in WEBAPP_URL shouldn't take the bot
                # down — long polling needs no inbound address, so use it.
                log.error("could not register the webhook at %s: %s", cfg.webapp_url, e)
                log.warning("falling back to polling. Set BOT_MODE=polling to make this "
                            "the default, or fix WEBAPP_URL to a reachable https address.")
                await bot.delete_webhook(drop_pending_updates=True)
                await dp.start_polling(bot)
            else:
                log.info("webhook registered at %s%s", cfg.webapp_url, path)
                await asyncio.Event().wait()      # serve until the platform stops us
        else:
            if app is not None:
                runner = await webapp.serve(app)
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        if runner:
            await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
