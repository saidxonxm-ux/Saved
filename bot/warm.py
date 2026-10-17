"""Mashhur qo'shiqlarni oldindan tayyorlash: so'rovlar sanaladi, eng ko'p so'ralganlari shaxsiy kanalga
yuklanadi va file_id keshga yoziladi. Foydalanuvchi bosganda qo'shiq darrov keladi."""
import asyncio
import json
import logging
import time

from . import config, db, services

log = logging.getLogger(__name__)

COUNTS: dict[str, int] = {}     # so'rov -> necha marta so'ralgan
WARMED: dict[str, int] = {}     # tayyorlangan so'rovlar -> vaqt
_busy = [False]


def chat_id() -> int | None:
    if config.CACHE_CHAT_ID:
        return config.CACHE_CHAT_ID
    v = db.setting("cache_chat", "")
    return int(v) if v.lstrip("-").isdigit() and v else None


def norm(q: str) -> str:
    return " ".join(q.lower().split())[:80]


def note_query(q: str) -> None:
    k = norm(q)
    if len(k) < 3:
        return
    COUNTS[k] = COUNTS.get(k, 0) + 1
    if len(COUNTS) > 3000:   # eng kam so'ralganlarini tashlaymiz
        for kk, _ in sorted(COUNTS.items(), key=lambda x: x[1])[:1000]:
            COUNTS.pop(kk, None)


async def load() -> None:
    try:
        COUNTS.update({k: int(v) for k, v in json.loads(db.setting("popular_q", "{}") or "{}").items()})
        WARMED.update({k: int(v) for k, v in json.loads(db.setting("warmed_q", "{}") or "{}").items()})
    except Exception:
        log.exception("warm.load xatosi")


async def save() -> None:
    top = dict(sorted(COUNTS.items(), key=lambda x: -x[1])[:300])
    warmed = dict(sorted(WARMED.items(), key=lambda x: -x[1])[:600])
    await db.set_setting("popular_q", json.dumps(top, ensure_ascii=False))
    await db.set_setting("warmed_q", json.dumps(warmed, ensure_ascii=False))


async def warm_query(bot, q: str, cid: int, lang: str = "uz") -> str:
    """Bitta so'rovning 1-natijasini tayyorlaydi. Qaytaradi: 'ok' | 'bor' | 'topilmadi' | 'xato'."""
    from aiogram.types import FSInputFile
    try:
        res = await services.search_multi([q], lang)
        if not res:
            return "topilmadi"
        item = res[0]
        key = "u:" + item["target"]
        if await db.cache_get(key):
            WARMED[norm(q)] = int(time.time())
            return "bor"
        song = await services.download_song(item["target"], item["title"])
        if not song:
            return "xato"
        try:
            if song.path.stat().st_size > config.MAX_UPLOAD:
                return "xato"
            sent = await bot.send_audio(cid, FSInputFile(song.path), title=song.title[:60],
                                        performer=song.artist[:60], disable_notification=True)
            await db.cache_put(key, [["audio", sent.audio.file_id]], song.title)
            WARMED[norm(q)] = int(time.time())
            return "ok"
        finally:
            song.path.unlink(missing_ok=True)
    except Exception as e:
        log.warning("warm_query xato (%s): %s", q, str(e)[:150])
        return "xato"


async def warm_many(bot, queries: list[str], cid: int) -> dict[str, int]:
    """Ketma-ket tayyorlaydi (asosiy yuklashlarga xalaqit bermaslik uchun band bo'lsa kutadi)."""
    stats = {"ok": 0, "bor": 0, "topilmadi": 0, "xato": 0}
    if _busy[0]:
        return stats
    _busy[0] = True
    try:
        for q in queries:
            while services.JOBS.locked():
                await asyncio.sleep(3)
            stats[await warm_query(bot, q, cid)] += 1
            await asyncio.sleep(2)
        await save()
    finally:
        _busy[0] = False
    return stats


async def loop(bot) -> None:
    await load()
    if config.WARM_HOURS <= 0:
        return
    await asyncio.sleep(600)
    while True:
        try:
            await save()
            cid = chat_id()
            if cid:
                todo = [q for q, n in sorted(COUNTS.items(), key=lambda x: -x[1])
                        if q not in WARMED and n >= 2][:config.WARM_TOP]
                if todo:
                    st = await warm_many(bot, todo, cid)
                    log.info("TIMING warm: %s", st)
        except Exception:
            log.exception("warm.loop xatosi")
        await asyncio.sleep(config.WARM_HOURS * 3600)
