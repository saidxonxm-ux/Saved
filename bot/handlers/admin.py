import asyncio
import csv
import html
import io
import logging
import re
import time
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message)

from .. import config, db

log = logging.getLogger(__name__)
router = Router()
router.message.filter(lambda m: m.from_user is not None and m.from_user.id in config.ADMIN_IDS)
router.callback_query.filter(lambda c: c.from_user.id in config.ADMIN_IDS)
esc = html.escape


class Bc(StatesGroup):
    msg = State()
    button = State()
    confirm = State()


class Ch(StatesGroup):
    wait = State()


class Ub(StatesGroup):
    wait = State()


class Dm(StatesGroup):
    wait = State()


class Lim(StatesGroup):
    wait = State()


class Ad(StatesGroup):
    wait = State()


BC_STOP = False


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=a, callback_data=b) for a, b in row]
                                                 for row in rows])


HOME = kb([[("📊 Statistika", "adm:stats"), ("📢 Reklama", "adm:bc")],
           [("📌 Majburiy obuna", "adm:ch"), ("🔧 Sozlamalar", "adm:set")],
           [("👤 Foydalanuvchi", "adm:user"), ("👮 Adminlar", "adm:adm")],
           [("👥 CSV", "adm:csv")]])
BACK = kb([[("⬅️ Panel", "adm:home")]])


async def edit(call: CallbackQuery, text: str, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


def when(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "—"


# ---------------- panel ----------------
@router.message(Command("admin"))
async def admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=HOME)


@router.message(Command("grant"))
async def grant_premium(m: Message, command: CommandObject):
    """/grant <user_id> <kun> — premiumni qo'lda berish (sovg'a, to'lov muammosi)."""
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(x.isdigit() for x in parts) or not 0 < int(parts[1]) <= 3650:
        return await m.answer("Format: <code>/grant USER_ID KUN</code> (KUN: 1–3650)")
    uid, days = int(parts[0]), int(parts[1])
    if not await db.get_user(uid):
        return await m.answer("Bunday foydalanuvchi topilmadi (u botga /start bosgan bo'lishi kerak).")
    until = await db.extend_premium(uid, days)
    await m.answer(f"✅ <code>{uid}</code>: premium {when(until)} gacha.")


@router.message(Command("health"))
async def health(m: Message):
    """Bot holati: yt-dlp versiyasi, cookies/proksi, YouTube/Instagram kutish rejimi, disk."""
    import importlib.metadata as md
    import shutil
    from pathlib import Path
    from .. import services
    try:
        ytv = md.version("yt-dlp")
    except Exception:
        ytv = "?"
    cookies = bool(config.COOKIES_FILE and Path(config.COOKIES_FILE).exists())
    yl, il = services.yt_block_left(), services.ig_cool_left()
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        disk = f"{du.free // 2**20} MB bo'sh / {du.total // 2**20} MB"
    except Exception:
        disk = "?"
    try:
        tmp_n = sum(1 for _ in config.TMP_DIR.iterdir())
    except Exception:
        tmp_n = 0
    ok, no = "✅", "❌"
    text = ("🩺 <b>Bot holati</b>\n\n"
            f"yt-dlp: <code>{ytv}</code>\n"
            f"🍪 Cookies: {ok if cookies else no + ' (o`rnatilmagan)'}\n"
            f"🌐 Proksi: {ok if config.PROXY_URL else no + ' (o`rnatilmagan)'}\n"
            f"▶️ YouTube: {('⏳ kutish rejimi, ' + str(yl // 60) + ' daq') if yl else ok + ' ishlayapti'}\n"
            f"📸 Instagram: {('⏳ cheklangan, ' + str(il // 60) + ' daq') if il else ok + ' ishlayapti'}\n"
            f"🎤 Ovoz→matn (Groq): {ok if config.GROQ_API_KEY else no}\n"
            f"💾 Disk: {disk}\n"
            f"📁 Vaqtinchalik fayllar: {tmp_n}")
    await m.answer(text)


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=HOME)


@router.callback_query(F.data == "adm:home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(call, "🛠 <b>Admin panel</b>", HOME)
    await call.answer()


# ---------------- statistika ----------------
@router.callback_query(F.data == "adm:stats")
async def stats(call: CallbackQuery):
    s = await db.summary()
    td, al = s["today"], s["all"]
    pays, stars = await db.payments_sum()
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{s['total']}</b>\n"
        f"🆕 Bugun qo'shildi: <b>{s['new_today']}</b>\n"
        f"🟢 24 soatda faol: <b>{s['online_24h']}</b>\n\n"
        f"📥 Yuklash: bugun <b>{td.get('download', 0)}</b> • jami <b>{al.get('download', 0)}</b>\n"
        f"🎵 Qo'shiq qidirish: bugun <b>{td.get('search', 0)}</b>\n"
        f"👥 Referal: bugun <b>{td.get('referral', 0)}</b> • jami <b>{al.get('referral', 0)}</b>\n"
        f"💎 Faol premium: <b>{await db.premium_count()}</b> • to'lovlar: <b>{pays}</b> ({stars} ⭐)")
    await edit(call, text, kb([[("🔄 Yangilash", "adm:stats")], [("⬅️ Panel", "adm:home")]]))
    await call.answer()


@router.callback_query(F.data == "adm:csv")
async def export_csv(call: CallbackQuery):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "username", "first_name", "lang", "joined_at", "last_seen", "banned", "active", "downloads"])
    for r in await db.all_users():
        w.writerow([r["id"], r["username"], r["first_name"], r["lang"], when(r["joined_at"]),
                    when(r["last_seen"]), r["banned"], r["active"], r["downloads"]])
    await call.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), "users.csv"))
    await call.answer()


# ---------------- sozlamalar ----------------
def settings_view():
    on = lambda v: "✅ YOQILGAN" if v else "❌ O'CHIQ"
    limit = int(db.setting("daily_limit", "0") or 0)
    text = ("🔧 <b>Sozlamalar</b>\n\n"
            f"🛠 Texnik ish rejimi: {on(db.setting('maint', '0') == '1')}\n"
            f"📌 Majburiy obuna: {on(db.setting('sub_on', '1') == '1')}\n"
            f"📅 Kunlik limit: <b>{limit or '∞ (cheksiz)'}</b>")
    return text, kb([[("🛠 Texnik ish", "st:maint")], [("📌 Majburiy obuna", "st:sub")],
                     [("📅 Kunlik limit", "st:limit")], [("⬅️ Panel", "adm:home")]])


@router.callback_query(F.data == "adm:set")
async def settings(call: CallbackQuery):
    text, markup = settings_view()
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data.in_({"st:maint", "st:sub"}))
async def toggle_setting(call: CallbackQuery):
    key, default = ("maint", "0") if call.data == "st:maint" else ("sub_on", "1")
    await db.set_setting(key, "0" if db.setting(key, default) == "1" else "1")
    text, markup = settings_view()
    await edit(call, text, markup)
    await call.answer("Saqlandi")


@router.callback_query(F.data == "st:limit")
async def ask_limit(call: CallbackQuery, state: FSMContext):
    await state.set_state(Lim.wait)
    await call.message.answer("Bitta foydalanuvchi uchun kunlik yuklash limitini raqam bilan yuboring.\n"
                              "0 = cheksiz. Bekor qilish: /cancel")
    await call.answer()


@router.message(Lim.wait)
async def set_limit(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        return await m.answer("Faqat raqam yuboring.")
    await db.set_setting("daily_limit", str(int(m.text.strip())))
    await state.clear()
    text, markup = settings_view()
    await m.answer(text, reply_markup=markup)


# ---------------- majburiy obuna ----------------
async def channels_view():
    chans = await db.list_channels()
    on = db.setting("sub_on", "1") == "1"
    lines = [f"{i}. {esc(c['title'])} — <code>{c['chat_id']}</code>" for i, c in enumerate(chans, 1)] or ["Kanallar yo'q."]
    text = (f"📌 <b>Majburiy obuna</b>: {'✅ yoqilgan' if on else '❌ o`chiq'}\n\n" + "\n".join(lines) +
            "\n\n<i>Bot kanalda admin bo'lishi shart.</i>")
    rows = [[(f"🗑 {c['title'][:25]}", f"ch:del:{c['chat_id']}")] for c in chans]
    rows += [[("➕ Kanal qo'shish", "ch:add")], [("🔔 Yoqish/o'chirish", "st:sub_ch")], [("⬅️ Panel", "adm:home")]]
    return text, kb(rows)


@router.callback_query(F.data == "adm:ch")
async def channels(call: CallbackQuery):
    text, markup = await channels_view()
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "st:sub_ch")
async def toggle_sub_from_channels(call: CallbackQuery):
    await db.set_setting("sub_on", "0" if db.setting("sub_on", "1") == "1" else "1")
    text, markup = await channels_view()
    await edit(call, text, markup)
    await call.answer("Saqlandi")


@router.callback_query(F.data.startswith("ch:del:"))
async def del_channel(call: CallbackQuery):
    await db.del_channel(int(call.data.split(":")[2]))
    text, markup = await channels_view()
    await edit(call, text, markup)
    await call.answer("O'chirildi")


@router.callback_query(F.data == "ch:add")
async def add_channel_ask(call: CallbackQuery, state: FSMContext):
    await state.set_state(Ch.wait)
    await call.message.answer(
        "Kanalni qo'shish uchun avval botni kanalga <b>admin</b> qiling, so'ng:\n"
        "• kanaldan biror xabarni <b>forward</b> qiling, yoki\n"
        "• <code>@kanal_nomi</code> / <code>-100...</code> ID yuboring\n\nBekor qilish: /cancel")
    await call.answer()


@router.message(Ch.wait)
async def add_channel(m: Message, state: FSMContext, bot: Bot):
    fo = getattr(m, "forward_origin", None)
    chat_ref = None
    if fo is not None and getattr(fo, "chat", None):
        chat_ref = fo.chat.id
    else:
        raw = (m.text or "").strip()
        raw = re.sub(r"^https?://t\.me/", "@", raw)
        if raw.lstrip("-").isdigit():
            chat_ref = int(raw)
        elif raw.startswith("@"):
            chat_ref = raw
    if chat_ref is None:
        return await m.answer("❌ Tushunmadim. Forward qiling yoki @username / ID yuboring.")
    try:
        chat = await bot.get_chat(chat_ref)
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception as e:
        return await m.answer(f"❌ Kanal topilmadi yoki bot unda yo'q: <code>{esc(str(e))[:150]}</code>")
    if me.status not in ("administrator", "creator"):
        return await m.answer("❌ Bot bu kanalda admin emas. Avval admin qiling.")
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try:
            link = (await bot.create_chat_invite_link(chat.id)).invite_link
        except Exception:
            link = chat.invite_link
    if not link:
        return await m.answer("❌ Havola olinmadi. Botga «Foydalanuvchilarni taklif qilish» huquqini bering.")
    await db.add_channel(chat.id, chat.title or str(chat.id), link)
    await state.clear()
    text, markup = await channels_view()
    await m.answer("✅ Qo'shildi.\n\n" + text, reply_markup=markup)


# ---------------- foydalanuvchi ----------------
async def user_card(uid: int):
    r = await db.get_user(uid)
    if not r:
        return None
    text = (f"👤 <b>{esc(r['first_name'] or '—')}</b> {('@' + r['username']) if r['username'] else ''}\n"
            f"🆔 <code>{r['id']}</code> • 🌐 {r['lang']}\n"
            f"📅 Qo'shildi: {when(r['joined_at'])}\n🕒 Oxirgi: {when(r['last_seen'])}\n"
            f"📥 Yuklashlar: {r['downloads']}\n"
            f"Holat: {'🚫 bloklangan' if r['banned'] else '✅ faol'}"
            f"{'' if r['active'] else ' (botni to`xtatgan)'}")
    ban_btn = ("✅ Blokdan chiqarish", f"u:tg:{uid}") if r["banned"] else ("🚫 Bloklash", f"u:tg:{uid}")
    return text, kb([[ban_btn, ("✉️ Xabar", f"u:msg:{uid}")], [("⬅️ Panel", "adm:home")]])


@router.callback_query(F.data == "adm:user")
async def ask_user(call: CallbackQuery, state: FSMContext):
    await state.set_state(Ub.wait)
    await call.message.answer("Foydalanuvchi ID sini yuboring. Bekor qilish: /cancel")
    await call.answer()


@router.message(Ub.wait)
async def find_user(m: Message, state: FSMContext):
    if not (m.text or "").strip().isdigit():
        return await m.answer("Faqat ID (raqam) yuboring.")
    card = await user_card(int(m.text.strip()))
    if not card:
        return await m.answer("Bunday foydalanuvchi topilmadi.")
    await state.clear()
    await m.answer(card[0], reply_markup=card[1])


@router.message(Command("user"))
async def cmd_user(m: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        return await m.answer("Ishlatish: /user 123456789")
    card = await user_card(int(command.args.strip()))
    await m.answer(card[0], reply_markup=card[1]) if card else await m.answer("Topilmadi.")


@router.callback_query(F.data.startswith("u:tg:"))
async def toggle_ban(call: CallbackQuery):
    uid = int(call.data.split(":")[2])
    r = await db.get_user(uid)
    if not r:
        return await call.answer("Topilmadi", show_alert=True)
    if uid in config.ADMIN_IDS:
        return await call.answer("Adminni bloklab bo'lmaydi", show_alert=True)
    await db.set_banned(uid, not r["banned"])
    card = await user_card(uid)
    await edit(call, card[0], card[1])
    await call.answer("Bajarildi")


@router.callback_query(F.data.startswith("u:msg:"))
async def dm_ask(call: CallbackQuery, state: FSMContext):
    await state.set_state(Dm.wait)
    await state.update_data(target=int(call.data.split(":")[2]))
    await call.message.answer("Yuboriladigan xabarni yozing (istalgan turdagi). Bekor qilish: /cancel")
    await call.answer()


@router.message(Dm.wait)
async def dm_send(m: Message, state: FSMContext, bot: Bot):
    target = (await state.get_data())["target"]
    await state.clear()
    try:
        await bot.copy_message(target, m.chat.id, m.message_id)
        await m.answer("✅ Yuborildi.", reply_markup=HOME)
    except Exception as e:
        await m.answer(f"❌ Yuborilmadi: <code>{esc(str(e))[:150]}</code>", reply_markup=HOME)


# ---------------- reklama (broadcast) ----------------
@router.callback_query(F.data == "adm:bc")
async def bc_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(Bc.msg)
    await call.message.answer("📢 Reklama xabarini yuboring (matn, rasm, video — istalgani).\nBekor qilish: /cancel")
    await call.answer()


@router.message(Bc.msg)
async def bc_msg(m: Message, state: FSMContext):
    await state.update_data(chat=m.chat.id, mid=m.message_id)
    await state.set_state(Bc.button)
    await m.answer("Tugma qo'shasizmi? Format:\n<code>Matn | https://link</code>\n"
                   "Tugma kerak bo'lmasa /skip yuboring.")


@router.message(Bc.button)
async def bc_button(m: Message, state: FSMContext, bot: Bot):
    markup = None
    if (m.text or "").strip() != "/skip":
        parts = [p.strip() for p in (m.text or "").split("|", 1)]
        if len(parts) != 2 or not parts[1].startswith(("http://", "https://", "tg://")):
            return await m.answer("❌ Format noto'g'ri. Misol: <code>Kanalga o'tish | https://t.me/kanal</code>")
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=parts[0], url=parts[1])]])
    d = await state.get_data()
    await state.update_data(btn=markup.model_dump(exclude_none=True) if markup else None)
    await state.set_state(Bc.confirm)
    await m.answer("👇 Ko'rinishi:")
    await bot.copy_message(m.chat.id, d["chat"], d["mid"], reply_markup=markup)
    await m.answer("Kimlarga yuboramiz?", reply_markup=kb([
        [("👥 Hammaga", "bc:aud:all")],
        [("🇺🇿 UZ", "bc:aud:uz"), ("🇷🇺 RU", "bc:aud:ru"), ("🇬🇧 EN", "bc:aud:en")],
        [("❌ Bekor", "adm:home")]]))


@router.callback_query(Bc.confirm, F.data.startswith("bc:aud:"))
async def bc_audience(call: CallbackQuery, state: FSMContext):
    aud = call.data.split(":")[2]
    ids = await db.audience(None if aud == "all" else aud)
    await state.update_data(aud=aud)
    await edit(call, f"📢 Qabul qiluvchilar: <b>{len(ids)}</b> ta. Yuboramizmi?",
               kb([[("✅ Yuborish", "bc:go"), ("❌ Bekor", "adm:home")]]))
    await call.answer()


@router.callback_query(Bc.confirm, F.data == "bc:go")
async def bc_go(call: CallbackQuery, state: FSMContext, bot: Bot):
    global BC_STOP
    d = await state.get_data()
    await state.clear()
    aud = d.get("aud", "all")
    ids = await db.audience(None if aud == "all" else aud)
    markup = InlineKeyboardMarkup.model_validate(d["btn"]) if d.get("btn") else None
    BC_STOP = False
    stop_kb = kb([[("⏹ To'xtatish", "bc:stop")]])
    status = await call.message.edit_text(f"📤 Yuborilmoqda... 0/{len(ids)}", reply_markup=stop_kb)
    await call.answer()
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        if BC_STOP:
            break
        try:
            await bot.copy_message(uid, d["chat"], d["mid"], reply_markup=markup)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(uid, d["chat"], d["mid"], reply_markup=markup)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            fail += 1
            await db.set_inactive(uid)
        except Exception:
            fail += 1
        if i % 25 == 0:
            try:
                await status.edit_text(f"📤 Yuborilmoqda... {i}/{len(ids)}", reply_markup=stop_kb)
            except TelegramBadRequest:
                pass
        await asyncio.sleep(0.04)
    done = "⏹ To'xtatildi" if BC_STOP else "✅ Tugadi"
    await status.edit_text(f"{done}\n\nYetkazildi: <b>{ok}</b>\nYetkazilmadi: <b>{fail}</b>", reply_markup=BACK)


@router.callback_query(F.data == "bc:stop")
async def bc_stop(call: CallbackQuery):
    global BC_STOP
    BC_STOP = True
    await call.answer("To'xtatilmoqda...")


# ---------------- adminlar ----------------
async def admins_view():
    rows_db = await db.list_admins()
    lines = [f"👑 <code>{uid}</code> — egasi" for uid in sorted(config.OWNER_IDS)]
    for r in rows_db:
        u = await db.get_user(r["id"])
        name = f" — {esc(u['first_name'] or '')}" if u else ""
        lines.append(f"👮 <code>{r['id']}</code>{name}")
    text = "👮 <b>Adminlar</b>\n\n" + "\n".join(lines) + "\n\n<i>Adminlarni faqat egalar qo'sha va o'chira oladi.</i>"
    rows = [[(f"🗑 {r['id']}", f"ad:del:{r['id']}")] for r in rows_db]
    rows += [[("➕ Admin qo'shish", "ad:add")], [("⬅️ Panel", "adm:home")]]
    return text, kb(rows)


@router.callback_query(F.data == "adm:adm")
async def admins(call: CallbackQuery):
    text, markup = await admins_view()
    await edit(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "ad:add")
async def admin_add_ask(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in config.OWNER_IDS:
        return await call.answer("Faqat bot egasi admin qo'sha oladi", show_alert=True)
    await state.set_state(Ad.wait)
    await call.message.answer("Yangi adminning <b>Telegram ID</b> raqamini yuboring yoki uning xabarini forward qiling.\n"
                              "ID ni @userinfobot orqali bilish mumkin. Bekor qilish: /cancel")
    await call.answer()


@router.message(Ad.wait)
async def admin_add(m: Message, state: FSMContext, bot: Bot):
    fo = getattr(m, "forward_origin", None)
    uid = None
    if fo is not None and getattr(fo, "sender_user", None):
        uid = fo.sender_user.id
    elif (m.text or "").strip().isdigit():
        uid = int((m.text or "").strip())
    if uid is None:
        return await m.answer("❌ Tushunmadim. Faqat ID raqam yuboring yoki xabarni forward qiling.")
    await db.add_admin(uid, m.from_user.id)
    config.ADMIN_IDS.add(uid)
    await state.clear()
    try:
        await bot.send_message(uid, "👮 Siz botga admin etib tayinlandingiz. /admin")
    except Exception:
        pass
    text, markup = await admins_view()
    await m.answer("✅ Admin qo'shildi.\n\n" + text, reply_markup=markup)


@router.callback_query(F.data.startswith("ad:del:"))
async def admin_del(call: CallbackQuery):
    if call.from_user.id not in config.OWNER_IDS:
        return await call.answer("Faqat bot egasi adminni o'chira oladi", show_alert=True)
    uid = int(call.data.split(":")[2])
    if uid in config.OWNER_IDS:
        return await call.answer("Egani o'chirib bo'lmaydi", show_alert=True)
    await db.del_admin(uid)
    config.ADMIN_IDS.discard(uid)
    text, markup = await admins_view()
    await edit(call, text, markup)
    await call.answer("O'chirildi")
