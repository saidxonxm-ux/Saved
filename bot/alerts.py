"""Adminlarga ogohlantirish: xatolar, bloklanishlar. Istalgan oqimdan (thread ham) chaqirsa bo'ladi."""
import asyncio
import logging
import time

from . import config

log = logging.getLogger(__name__)
_bot = None
_loop: asyncio.AbstractEventLoop | None = None
_last: dict[str, float] = {}


def setup(bot, loop) -> None:
    global _bot, _loop
    _bot, _loop = bot, loop


async def _send(text: str) -> None:
    for aid in list(config.ADMIN_IDS):
        try:
            await _bot.send_message(aid, text)
        except Exception:
            pass


def notify(key: str, text: str, cooldown: int = 1800) -> None:
    """Bir xil key uchun cooldown (soniya) ichida takror yubormaydi."""
    now = time.monotonic()
    if now - _last.get(key, -1e9) < cooldown:
        return
    _last[key] = now
    log.info("ALERT %s: %s", key, text[:120])
    if _bot is None or _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_send(text), _loop)
    except Exception:
        log.exception("alert yuborilmadi")
