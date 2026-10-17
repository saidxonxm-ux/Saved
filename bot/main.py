import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from . import alerts, backup, config, db, gate, warm
from .gate import Gate
from .handlers import admin, extra, inline, ops, user

log = logging.getLogger("bot")

COMMANDS = {
    "uz": [("start", "Boshlash"), ("top", "Mashhur qo'shiqlar"), ("fav", "Sevimlilar"), ("settings", "Sozlamalar"), ("lang", "Til")],
    "ru": [("start", "Начать"), ("top", "Популярные песни"), ("fav", "Избранное"), ("settings", "Настройки"), ("lang", "Язык")],
    "en": [("start", "Start"), ("top", "Popular songs"), ("fav", "Favorites"), ("settings", "Settings"), ("lang", "Language")],
}


async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        try:
            now = time.time()
            if config.TMP_DIR.exists():
                for p in config.TMP_DIR.iterdir():
                    if p.is_file() and now - p.stat().st_mtime > config.FILE_TTL:
                        p.unlink(missing_ok=True)
            for k, p in list(user.CACHE.items()):
                if not p.exists():
                    user.CACHE.pop(k, None)
            while len(user.QUERIES) > 500:
                user.QUERIES.pop(next(iter(user.QUERIES)))
            while len(user.SEARCH) > 1500:
                user.SEARCH.pop(next(iter(user.SEARCH)))
            gate.prune()
        except Exception:
            log.exception("Tozalashda xato")


async def on_error(event: ErrorEvent) -> bool:
    log.error("Handler xatosi: %s", event.exception, exc_info=event.exception)
    exc = event.exception
    alerts.notify("err:" + type(exc).__name__,
                  f"❗️ Botda xato: {type(exc).__name__}: {str(exc)[:200]}", 900)
    return True


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if config.COOKIES_B64:
        try:
            import base64
            cf = config.DATA_DIR / "cookies.txt"
            cf.write_bytes(base64.b64decode(config.COOKIES_B64))
            config.COOKIES_FILE = str(cf)
            log.info("cookies.txt o'rnatildi")
        except Exception:
            log.exception("COOKIES_B64 o'qilmadi")
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=64))
    await db.init(config.DB_PATH)
    for r in await db.list_admins():
        config.ADMIN_IDS.add(r["id"])

    bot = Bot(config.BOT_TOKEN, session=AiohttpSession(timeout=600),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Gate())
    dp.callback_query.outer_middleware(Gate())
    dp.include_router(admin.router)   # admin birinchi
    dp.include_router(ops.router)     # zaxira/tiklash/oldindan tayyorlash (adminlar uchun)
    dp.include_router(inline.router)  # inline qidiruv (@bot nom)
    dp.include_router(user.router)
    dp.include_router(extra.router)
    dp.include_router(extra.group)
    dp.errors.register(on_error)

    for code, cmds in COMMANDS.items():
        try:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in cmds],
                                      language_code=code if code != "uz" else "uz")
        except Exception:
            log.warning("Komandalarni o'rnatib bo'lmadi (%s)", code)
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS["uz"]])
    except Exception:
        pass

    me = await bot.me()
    log.info("Bot ishga tushdi: @%s", me.username)
    for aid in config.ADMIN_IDS:
        try:
            await bot.send_message(aid, f"✅ @{me.username} ishga tushdi. /admin")
        except Exception:
            pass

    alerts.setup(bot, asyncio.get_running_loop())
    task = asyncio.create_task(cleanup_loop())
    flusher = asyncio.create_task(db.flush_loop())
    backer = asyncio.create_task(backup.backup_loop(bot))
    warmer = asyncio.create_task(warm.loop(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        flusher.cancel()
        backer.cancel()
        warmer.cancel()
        try:
            await warm.save()
        except Exception:
            pass
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())
