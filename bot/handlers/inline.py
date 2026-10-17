"""Inline qidiruv: istalgan chatda @bot nom — keshdagi tayyor qo'shiqlar darrov chiqadi."""
import hashlib
import json
import logging

from aiogram import Router
from aiogram.types import InlineQuery, InlineQueryResultCachedAudio, InlineQueryResultsButton

from .. import config, db, translit
from ..gate import missing_channels

log = logging.getLogger(__name__)
router = Router()


def variants(q: str) -> list[str]:
    vs = [q.casefold()]
    a = translit.alt(q)
    if a:
        vs.append(a.casefold())
    return vs


def hit(title: str, vs: list[str]) -> bool:
    tl = title.casefold()
    return any(all(w in tl for w in v.split()) for v in vs if v.strip())


def result(key: str, fid: str) -> InlineQueryResultCachedAudio:
    return InlineQueryResultCachedAudio(id=hashlib.sha1(key.encode()).hexdigest()[:24], audio_file_id=fid)


@router.inline_query()
async def on_inline(q: InlineQuery):
    uid = q.from_user.id
    u = await db.get_user(uid)
    if u is not None and u["banned"]:
        return await q.answer([], cache_time=5)
    if db.setting("maint", "0") == "1" and uid not in config.ADMIN_IDS:   # texnik ish rejimi
        return await q.answer([], cache_time=5)
    if await missing_channels(q.bot, uid):   # majburiy obuna bu yerda ham amal qiladi
        return await q.answer([], cache_time=5, is_personal=True,
                              button=InlineQueryResultsButton(text="📌 Avval obuna bo'ling", start_parameter="sub"))
    text = (q.query or "").strip()
    items: list[tuple[str, str]] = []
    if len(text) >= 2:
        vs = variants(text)
        for key, title, fid in await db.audio_cache_titles():
            if hit(title, vs):
                items.append((key, fid))
                if len(items) >= 20:
                    break
    else:   # bo'sh so'rov: eng mashhur qo'shiqlar
        for r in await db.top_songs(10):
            row = await db.cache_get(r["ckey"])
            try:
                if row and json.loads(row["payload"])[0][0] == "audio":
                    items.append((r["ckey"], json.loads(row["payload"])[0][1]))
            except Exception:
                continue
    await q.answer([result(k, f) for k, f in items], cache_time=20, is_personal=False,
                   button=InlineQueryResultsButton(text="🔎 Botda qidirish", start_parameter="s"))
