"""Egalar/adminlar uchun: baza zaxirasi va tiklash, mashhur qo'shiqlarni oldindan tayyorlash."""
import asyncio
import html
import logging
import re
import sqlite3
from pathlib import Path

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import backup, config, db, warm

log = logging.getLogger(__name__)
router = Router()
router.message.filter(lambda m: m.from_user is not None and m.from_user.id in config.ADMIN_IDS)
router.callback_query.filter(lambda c: c.from_user.id in config.ADMIN_IDS)
esc = html.escape

PENDING: dict[int, Path] = {}      # tiklash kutayotgan fayllar
AWAIT_CHAT: set[int] = set()        # kesh kanali uchun forward kutayotgan egalar
_TASKS: set[asyncio.Task] = set()


def is_owner(uid: int) -> bool:
    return uid in config.OWNER_IDS


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)


# ---------------- zaxira ----------------
@router.message(Command("backup"))
async def cmd_backup(m: Message, bot: Bot):
    if not is_owner(m.from_user.id):
        return await m.answer("Faqat egalar uchun.")
    wait = await m.answer("⏳ Zaxira tayyorlanmoqda...")
    n = await backup.send_backup(bot, {m.from_user.id}, "Qo'lda so'ralgan zaxira.")
    await wait.edit_text("✅ Zaxira yuborildi." if n else "❌ Zaxira yuborilmadi.")


def _count_users(path: Path) -> int:
    con = sqlite3.connect(str(path))
    try:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        con.close()


def _is_restore(m: Message) -> bool:
    return bool(m.document and (m.caption or "").strip().lower().startswith("/restore"))


@router.message(_is_restore)
async def restore_file(m: Message, bot: Bot):
    uid = m.from_user.id
    if not is_owner(uid):
        return await m.answer("Tiklash faqat egalar uchun.")
    doc = m.document
    if doc.file_size and doc.file_size > config.MAX_DOWNLOAD_FROM_TG:
        return await m.answer("❌ Fayl 20 MB dan katta (Telegram botga shuncha yuklashga ruxsat bermaydi).")
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    raw = config.TMP_DIR / f"restore_in_{uid}"
    out = config.TMP_DIR / f"restore_{uid}.db"
    try:
        await bot.download(doc, destination=raw)
        err = await asyncio.to_thread(backup.validate_sync, raw, out)
    finally:
        raw.unlink(missing_ok=True)
    if err:
        out.unlink(missing_ok=True)
        return await m.answer(f"❌ {esc(err)}")
    n = await asyncio.to_thread(_count_users, out)
    PENDING[uid] = out
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ha, tiklash", callback_data="rs:yes"),
        InlineKeyboardButton(text="❌ Yo'q", callback_data="rs:no")]])
    await m.answer(f"Fayl yaroqli. Ichida <b>{n}</b> foydalanuvchi bor.\n"
                   f"⚠️ Hozirgi baza almashtiriladi (avval uning zaxirasi sizga yuboriladi). Davom etamizmi?",
                   reply_markup=kb)


@router.callback_query(lambda c: c.data in ("rs:yes", "rs:no"))
async def restore_confirm(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    if not is_owner(uid):
        return await call.answer("Faqat egalar uchun.", show_alert=True)
    out = PENDING.pop(uid, None)
    if call.data == "rs:no" or not out or not out.exists():
        if out:
            out.unlink(missing_ok=True)
        await call.message.edit_text("Bekor qilindi." if call.data == "rs:no" else "Fayl eskirgan, qaytadan yuboring.")
        return await call.answer()
    await call.message.edit_text("⏳ Avval joriy bazaning zaxirasi yuborilmoqda...")
    await backup.send_backup(bot, {uid}, "Tiklashdan oldingi holat.")
    try:
        await db.reopen(out)
        for r in await db.list_admins():
            config.ADMIN_IDS.add(r["id"])
        await warm.load()
        await call.message.edit_text("✅ Baza tiklandi.")
    except Exception as e:
        log.exception("Tiklash xatosi")
        await call.message.edit_text(f"❌ Tiklashda xato: {esc(str(e)[:150])}")
    finally:
        out.unlink(missing_ok=True)
    await call.answer()


# ---------------- mashhur qo'shiqlarni oldindan tayyorlash ----------------
@router.message(Command("warmchat"))
async def cmd_warmchat(m: Message, command: CommandObject, bot: Bot):
    uid = m.from_user.id
    if not is_owner(uid):
        return await m.answer("Faqat egalar uchun.")
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit() or not arg:
        AWAIT_CHAT.add(uid)
        return await m.answer(
            "Tayyor qo'shiqlar saqlanadigan <b>shaxsiy kanal</b> ulash:\n"
            "1) Telegram'da yangi shaxsiy kanal oching.\n"
            "2) Botni kanalga <b>administrator</b> qiling.\n"
            "3) Kanaldan istalgan postni shu yerga <b>forward</b> qiling (yoki <code>/warmchat -100...</code> deb ID yozing).")
    await _connect_chat(m, bot, int(arg))


async def _connect_chat(m: Message, bot: Bot, cid: int):
    try:
        await bot.send_message(cid, "✅ Saved Go: kesh kanali ulandi.")
    except Exception as e:
        return await m.answer(f"❌ Kanalga yozib bo'lmadi: {esc(str(e)[:120])}\nBot kanalda administrator bo'lishi kerak.")
    await db.set_setting("cache_chat", str(cid))
    AWAIT_CHAT.discard(m.from_user.id)
    await m.answer(f"✅ Ulandi: <code>{cid}</code>\nEndi <code>/warm</code> ni ishlating.")


def _fwd_chat(m: Message):
    o = getattr(m, "forward_origin", None)
    chat = getattr(o, "chat", None)
    return chat if chat is not None and getattr(chat, "type", "") == "channel" else None


@router.message(lambda m: m.from_user.id in AWAIT_CHAT and _fwd_chat(m) is not None)
async def got_forward(m: Message, bot: Bot):
    await _connect_chat(m, bot, _fwd_chat(m).id)


@router.message(Command("warm"))
async def cmd_warm(m: Message, command: CommandObject, bot: Bot):
    cid = warm.chat_id()
    if not cid:
        return await m.answer("Avval kesh kanalini ulang: /warmchat")
    arg = (command.args or "").strip()
    if not arg:
        top = sorted(warm.COUNTS.items(), key=lambda x: -x[1])[:8]
        lines = "\n".join(f"{n}× {esc(q)}" + (" ✅" if q in warm.WARMED else "") for q, n in top) or "hali yo'q"
        return await m.answer(
            f"🔥 <b>Oldindan tayyorlash</b>\nKanal: <code>{cid}</code>\nTayyor: {len(warm.WARMED)} ta\n\n"
            f"Eng ko'p so'ralganlar:\n{lines}\n\n"
            "• <code>/warm auto</code> — mashhurlarini hozir tayyorlash\n"
            "• <code>/warm Yurak; Shoxrux; Ulug'bek</code> — o'zingiz yozgan so'rovlarni tayyorlash")
    if arg.lower() == "auto":
        queries = [q for q, _ in sorted(warm.COUNTS.items(), key=lambda x: -x[1]) if q not in warm.WARMED][:config.WARM_TOP]
    else:
        queries = [x.strip() for x in re.split(r"[;\n]", arg) if x.strip()][:30]
    if not queries:
        return await m.answer("Tayyorlash uchun so'rov yo'q.")
    await m.answer(f"⏳ {len(queries)} ta so'rov fonda tayyorlanmoqda. Tugagach xabar beraman.")

    async def job():
        st = await warm.warm_many(bot, queries, cid)
        await m.answer("✅ Tayyor: {ok}, avval bor: {bor}, topilmadi: {topilmadi}, xato: {xato}".format(**st))

    _spawn(job())
