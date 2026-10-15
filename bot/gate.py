import asyncio
import logging
import time

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import config, db
from .i18n import t

log = logging.getLogger(__name__)

_ok_until: dict[int, float] = {}   # obuna bo'lgan foydalanuvchilar uchun qisqa kesh
_last: dict[int, float] = {}       # flood nazorati


def _is_missing(m) -> bool:
    if m.status in ("left", "kicked"):
        return True
    return m.status == "restricted" and not getattr(m, "is_member", True)


async def missing_channels(bot: Bot, uid: int, force: bool = False) -> list:
    if db.setting("sub_on", "1") != "1":
        return []
    chans = await db.list_channels()
    if not chans:
        return []
    if not force and _ok_until.get(uid, 0) > time.monotonic():
        return []
    res = await asyncio.gather(*(bot.get_chat_member(c["chat_id"], uid) for c in chans), return_exceptions=True)
    miss = []
    for ch, r in zip(chans, res):
        if isinstance(r, Exception):
            # bot kanalda admin emas yoki kanal o'chgan: foydalanuvchini bloklamaymiz
            log.warning("Obuna tekshirib bo'lmadi (%s): %s", ch["chat_id"], r)
            continue
        if _is_missing(r):
            miss.append(ch)
    if miss:
        _ok_until.pop(uid, None)
    else:
        _ok_until[uid] = time.monotonic() + config.SUB_CACHE_TTL
    return miss


def sub_keyboard(chans, lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📢 {c['title']}", url=c["link"])] for c in chans]
    rows.append([InlineKeyboardButton(text=t(lang, "btn_check"), callback_data="chk")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _flooding(uid: int, gap: float = 0.5) -> bool:
    now = time.monotonic()
    prev = _last.get(uid, 0.0)
    _last[uid] = now
    return now - prev < gap


def prune() -> None:
    now = time.monotonic()
    for k in [k for k, v in _ok_until.items() if v < now]:
        _ok_until.pop(k, None)
    for k in [k for k, v in _last.items() if now - v > 60]:
        _last.pop(k, None)


class Gate(BaseMiddleware):
    """Foydalanuvchini yozadi, ban/texnik ish/flood/majburiy obunani tekshiradi."""

    async def __call__(self, handler, event, data):
        user = event.from_user
        if user is None or user.is_bot:
            return await handler(event, data)
        row = await db.upsert_user(user)
        lang = row["lang"]
        data["u"], data["lang"] = row, lang

        if user.id in config.ADMIN_IDS:
            return await handler(event, data)

        # To'lov xabari har doim o'tadi: Telegram yulduzlarni allaqachon yechgan, premium berilishi shart
        if isinstance(event, Message) and event.successful_payment is not None:
            return await handler(event, data)

        is_cb = isinstance(event, CallbackQuery)
        bot: Bot = data["bot"]

        async def say(key: str, alert: bool = True):
            if is_cb:
                await event.answer(t(lang, key), show_alert=alert)
            else:
                await event.answer(t(lang, key))

        if row["banned"]:
            return await say("banned")
        if db.setting("maint", "0") == "1":
            return await say("maint")

        text_msg = isinstance(event, Message) and bool(event.text)
        if (is_cb or text_msg) and _flooding(user.id):
            if is_cb:
                await event.answer(t(lang, "flood"))
            return None

        paid = isinstance(event, Message) and event.successful_payment is not None
        passthrough = paid or (is_cb and (event.data == "chk" or (event.data or "").startswith("lang:")))
        if not passthrough:
            miss = await missing_channels(bot, user.id)
            if miss:
                if is_cb:
                    await event.answer()
                target = event.chat.id if isinstance(event, Message) else user.id
                await bot.send_message(target, t(lang, "sub_needed"), reply_markup=sub_keyboard(miss, lang))
                return None
        return await handler(event, data)
