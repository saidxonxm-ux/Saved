import asyncio
import html
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, Message)

from .. import config, db, services, warm
from ..gate import missing_channels
from ..i18n import t

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.chat.type == "private")

esc = html.escape
URL_RE = re.compile(r"https?://[^\s<>\"']+")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".wav"}
TRACK = {"igsh", "igshid", "si", "fbclid", "feature"}

CACHE: dict[str, Path] = {}     # tugmalar uchun vaqtincha fayllar
QUERIES: dict[str, str] = {}    # Shazam natijasi -> qidiruv matni
BUSY: set[int] = set()
VOICE: dict[str, Path] = {}      # til tanlashni kutayotgan ovozli xabarlar


# ---------------- yordamchilar ----------------
def extract_url(m: Message) -> str | None:
    mt = URL_RE.search(m.text or "")
    if mt:
        return mt.group(0).rstrip(").,;")
    for e in m.entities or []:
        if e.type == "text_link" and e.url:
            return e.url
    return None


def is_link(m: Message) -> bool:
    return extract_url(m) is not None


def is_plain(m: Message) -> bool:
    return bool(m.text) and not m.text.startswith("/")


def norm_url(url: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not (k.startswith("utm_") or k in TRACK)]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), urlencode(q), ""))


@asynccontextmanager
async def job(uid: int):
    if uid in BUSY:
        yield False
        return
    BUSY.add(uid)
    try:
        yield True
    finally:
        BUSY.discard(uid)


def remember(path: Path) -> str:
    key = uuid.uuid4().hex[:10]
    CACHE[key] = path
    return key


def actions_kb(key: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_music"), callback_data=f"music:{key}")],
        [InlineKeyboardButton(text=t(lang, "btn_circle"), callback_data=f"circle:{key}"),
         InlineKeyboardButton(text=t(lang, "btn_mp3"), callback_data=f"mp3:{key}")],
    ])


def fid(msg: Message) -> list[str] | None:
    if msg.video:
        return ["video", msg.video.file_id]
    if msg.photo:
        return ["photo", msg.photo[-1].file_id]
    if msg.audio:
        return ["audio", msg.audio.file_id]
    if msg.document:
        return ["document", msg.document.file_id]
    return None


def caption(title: str, uname: str) -> str:
    base = esc((title or "").strip()[:180])
    tail = f"📥 @{uname}"
    return f"{base}\n\n{tail}" if base else tail


async def source_file(call: CallbackQuery, key: str, bot: Bot) -> Path | None:
    p = CACHE.get(key)
    if p and p.exists():
        return p
    m = call.message
    media = (m.video or m.video_note) if isinstance(m, Message) else None
    if media and (media.file_size or 0) <= config.MAX_DOWNLOAD_FROM_TG:
        dst = services.tmp(".mp4", "src")
        await bot.download(media, destination=dst)
        CACHE[key] = dst
        return dst
    return None


# ---------------- yuborish ----------------
async def fit(path: Path) -> Path | None:
    if path.stat().st_size <= config.MAX_UPLOAD:
        return path
    if path.suffix.lower() in VIDEO_EXT:
        small = await services.shrink(path)
        path.unlink(missing_ok=True)
        return small
    path.unlink(missing_ok=True)
    return None


async def send_one(m: Message, path: Path, cap: str | None, lang: str) -> list[str] | None:
    ext = path.suffix.lower()
    f = FSInputFile(path, chunk_size=512 * 1024)
    if ext in VIDEO_EXT:
        sent = await m.answer_video(f, caption=cap, supports_streaming=True,
                                    reply_markup=actions_kb(remember(path), lang))
        return fid(sent)
    try:
        if ext in IMAGE_EXT:
            sent = await m.answer_photo(f, caption=cap)
        elif ext in AUDIO_EXT:
            sent = await m.answer_audio(f, caption=cap)
        else:
            sent = await m.answer_document(f, caption=cap)
    except TelegramBadRequest:
        sent = await m.answer_document(FSInputFile(path), caption=cap)
    path.unlink(missing_ok=True)
    return fid(sent)


async def deliver(m: Message, files: list[Path], cap: str, lang: str) -> list[list[str]]:
    ready = []
    for p in files:
        q = await fit(p)
        if q:
            ready.append(q)
        else:
            await m.answer(t(lang, "too_big"))
    items: list[list[str]] = []
    visual = [p for p in ready if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT]
    rest = ready if len(visual) < 2 else [p for p in ready if p not in visual]
    first = True
    if len(visual) >= 2:
        for i in range(0, len(visual), 10):
            chunk = visual[i:i + 10]
            if len(chunk) == 1:
                r = await send_one(m, chunk[0], cap if first else None, lang)
                first = False
                items += [r] if r else []
                continue
            group = []
            for p in chunk:
                c = cap if first else None
                first = False
                if p.suffix.lower() in VIDEO_EXT:
                    group.append(InputMediaVideo(media=FSInputFile(p), caption=c, supports_streaming=True))
                else:
                    group.append(InputMediaPhoto(media=FSInputFile(p), caption=c))
            try:
                msgs = await m.answer_media_group(group)
                items += [x for x in (fid(s) for s in msgs) if x]
                for p in chunk:
                    p.unlink(missing_ok=True)
            except TelegramBadRequest:
                for p in chunk:
                    r = await send_one(m, p, None, lang)
                    items += [r] if r else []
    for p in rest:
        r = await send_one(m, p, cap if first else None, lang)
        first = False
        items += [r] if r else []
    return items


async def send_cached(m: Message, key: str, cap: str, lang: str) -> bool:
    row = await db.cache_get(key)
    if not row:
        return False
    items = json.loads(row["payload"])
    try:
        if len(items) >= 2 and all(k in ("video", "photo") for k, _ in items) and len(items) <= 10:
            group = [InputMediaVideo(media=f, caption=cap if i == 0 else None) if k == "video"
                     else InputMediaPhoto(media=f, caption=cap if i == 0 else None)
                     for i, (k, f) in enumerate(items)]
            await m.answer_media_group(group)
        else:
            for i, (k, f) in enumerate(items):
                c = cap if i == 0 else None
                if k == "video":
                    await m.answer_video(f, caption=c, reply_markup=actions_kb(uuid.uuid4().hex[:10], lang))
                elif k == "photo":
                    await m.answer_photo(f, caption=c)
                elif k == "audio":
                    await m.answer_audio(f, caption=c)
                else:
                    await m.answer_document(f, caption=c)
        return True
    except TelegramBadRequest:
        await db.cache_del(key)
        return False


# ---------------- boshlash / til / yordam ----------------
def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
    ]])


async def welcome_kb(bot: Bot, lang: str) -> InlineKeyboardMarkup:
    uname = (await bot.me()).username
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_group"), url=f"https://t.me/{uname}?startgroup=true")],
        [InlineKeyboardButton(text=t(lang, "btn_lang"), callback_data="set:lang")],
    ])


@router.message(CommandStart())
async def start(m: Message, u, lang: str, bot: Bot):
    if not u["lang_set"]:
        await m.answer(t(lang, "lang_pick"), reply_markup=lang_kb())
        return
    await m.answer(t(lang, "hello", name=esc(m.from_user.first_name or "")) + t(lang, "help"),
                   reply_markup=await welcome_kb(bot, lang))


@router.message(Command("lang"))
async def cmd_lang(m: Message, lang: str):
    await m.answer(t(lang, "lang_pick"), reply_markup=lang_kb())


@router.message(Command("help"))
async def cmd_help(m: Message, lang: str, bot: Bot):
    await m.answer(t(lang, "help"), reply_markup=await welcome_kb(bot, lang))


@router.callback_query(F.data.startswith("lang:"))
async def pick_lang(call: CallbackQuery, bot: Bot):
    new = call.data.split(":", 1)[1]
    if new not in ("uz", "ru", "en"):
        return await call.answer()
    await db.set_lang(call.from_user.id, new)
    await call.answer(t(new, "lang_ok"))
    if isinstance(call.message, Message):
        try:
            await call.message.edit_text(t(new, "hello", name=esc(call.from_user.first_name or "")) + t(new, "help"),
                                         reply_markup=await welcome_kb(bot, new))
        except TelegramBadRequest:
            pass


@router.callback_query(F.data == "chk")
async def check_sub(call: CallbackQuery, bot: Bot, lang: str):
    miss = await missing_channels(bot, call.from_user.id, force=True)
    if miss:
        return await call.answer(t(lang, "sub_fail"), show_alert=True)
    await call.answer()
    if isinstance(call.message, Message):
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass
    await bot.send_message(call.from_user.id, t(lang, "sub_ok"))


# ---------------- sozlamalar / tarix ----------------
def settings_kb(u, lang: str) -> InlineKeyboardMarkup:
    mark = lambda v: "✅" if v else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "set_circle", v=mark(u["auto_circle"])), callback_data="set:auto_circle")],
        [InlineKeyboardButton(text=t(lang, "set_music", v=mark(u["auto_music"])), callback_data="set:auto_music")],
        [InlineKeyboardButton(text=t(lang, "btn_lang"), callback_data="set:lang")],
    ])


@router.message(Command("settings"))
async def cmd_settings(m: Message, u, lang: str):
    await m.answer(t(lang, "settings"), reply_markup=settings_kb(u, lang))


@router.callback_query(F.data.startswith("set:"))
async def on_setting(call: CallbackQuery, lang: str):
    what = call.data.split(":", 1)[1]
    if not isinstance(call.message, Message):
        return await call.answer()
    if what == "lang":
        await call.answer()
        await call.message.answer(t(lang, "lang_pick"), reply_markup=lang_kb())
        return
    if what in ("auto_circle", "auto_music"):
        await db.toggle_flag(call.from_user.id, what)
    u = await db.get_user(call.from_user.id)
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=settings_kb(u, lang))
    except TelegramBadRequest:
        pass


# ---------------- link orqali yuklash ----------------
YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^\s]*&)?v=|embed/|live/)|youtu\.be/)([\w-]{11})")


async def send_by_url(m: Message, r: dict, cap: str, lang: str) -> list[str] | None:
    """Telegramning o'zi havoladan oladi: server yuklamaydi ham, yubormaydi ham."""
    try:
        if r["kind"] == "photo":
            sent = await m.answer_photo(r["url"], caption=cap)
        else:
            sent = await m.answer_video(r["url"], caption=cap, supports_streaming=True,
                                        reply_markup=actions_kb(uuid.uuid4().hex[:10], lang))
        return fid(sent)
    except Exception as e:
        log.info("Havola orqali yuborilmadi: %s", str(e)[:150])
        return None


async def record(uid: int, key: str, url: str, title: str, items: list) -> None:
    if items:
        await db.cache_put(key, items, title)
    await db.incr("download")
    await db.bump_downloads(uid)


IG_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)", re.I)
TT_RE = re.compile(r"tiktok\.com/.*?/video/(\d+)", re.I)
TW_RE = re.compile(r"(?:twitter|x)\.com/.*?/status/(\d+)", re.I)


def canon_key(url: str, height: int = 720) -> str:
    """Bir xil kontent turli havolalarda ham bitta keshga tushsin (tracking parametrlarsiz)."""
    suffix = f"#h{height}" if height != 720 else ""
    for pre, rx in (("ig", IG_RE), ("tt", TT_RE), ("x", TW_RE), ("yt", YT_RE)):
        mt = rx.search(url)
        if mt:
            return f"{pre}:{mt.group(1)}{suffix}"
    return norm_url(url) + suffix


STORY_RE = re.compile(r"instagram\.com/stories/(?!highlights)([\w.]+)", re.I)


async def handle_link(m: Message, bot: Bot, lang: str, url: str, height: int, uid: int):
    story = STORY_RE.search(url)
    if story:
        # bitta story havolasi bo'lsa ham, foydalanuvchining hamma faol stories'i olinadi
        url = f"https://www.instagram.com/stories/{story.group(1)}/"
    async with job(uid) as ok:
        if not ok:
            return await m.answer(t(lang, "busy"))
        key = canon_key(url, height)
        uname = (await bot.me()).username
        row = None if story else await db.cache_get(key)   # stories 24 soatda o'zgaradi: keshlanmaydi
        if row and await send_cached(m, key, caption(row["title"], uname), lang):
            return await record(uid, key, url, row["title"], [])

        asyncio.create_task(bot.send_chat_action(m.chat.id, "upload_video"))

        def skip_msg(e):
            return t(lang, "dl_live") if e.reason == "live" else t(lang, "dl_long", m=config.MAX_VIDEO_SEC // 60)

        # 1) tezkor yo'l (YouTube'dan tashqari)
        if height == 720 and not story and not services.is_youtube(url):
            try:
                r = await services.resolve(url)
            except services.Skipped as e:
                return await m.answer(skip_msg(e))
            except Exception as e:
                log.info("resolve o'tmadi (%s): %s", url, str(e)[:160])
                r = None
            if r:
                items = await send_by_url(m, r, caption(r["title"], uname), lang)
                if items:
                    return await record(uid, key, url, r["title"], [items])

        # 2) oddiy yuklash
        status = await m.answer(t(lang, "dl_start"))
        try:
            res = await services.download(url, height, 30 if story else 10)
        except services.Skipped as e:
            return await status.edit_text(skip_msg(e))
        except Exception as e:
            log.warning("Yuklashda xato (%s): %s", url, str(e)[:300])
            if story:
                return await status.edit_text(t(lang, "stories_none"))
            try:
                alt = await services.scrape_media(url)
            except Exception:
                alt = None
            if alt:
                items = await send_by_url(m, alt, caption(alt["title"], uname), lang)
                if items:
                    await status.delete()
                    return await record(uid, key, url, alt["title"], [items])
            return await status.edit_text(t(lang, "dl_fail"))
        if not res.files:
            return await status.edit_text(t(lang, "dl_empty"))
        try:
            items = await deliver(m, res.files, caption(res.title, uname), lang)
        except Exception:
            log.exception("Yuborishda xato")
            return await status.edit_text(t(lang, "dl_fail"))
        await status.delete()
        if items:
            await record(uid, key, url, res.title, [] if story else items)


@router.message(is_link)
async def on_link(m: Message, bot: Bot, lang: str):
    url = extract_url(m)
    mt = YT_RE.search(url)
    if mt:
        vid = mt.group(1)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="360p", callback_data=f"yq:360:{vid}"),
            InlineKeyboardButton(text="480p", callback_data=f"yq:480:{vid}"),
            InlineKeyboardButton(text="720p", callback_data=f"yq:720:{vid}"),
            InlineKeyboardButton(text=t(lang, "btn_mp3"), callback_data=f"yq:0:{vid}"),
        ]])
        return await m.answer(t(lang, "yt_pick"), reply_markup=kb)
    await handle_link(m, bot, lang, url, 720, m.from_user.id)


@router.callback_query(F.data.startswith("yq:"))
async def yt_quality(call: CallbackQuery, bot: Bot, lang: str):
    try:
        _, h, vid = call.data.split(":")
        h = int(h)
    except ValueError:
        return await call.answer()
    if not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    await call.answer()
    msg = call.message
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    uid = call.from_user.id
    url = f"https://www.youtube.com/watch?v={vid}"
    if h == 0:
        async with job(uid) as ok:
            if not ok:
                return await msg.answer(t(lang, "busy"))
            await send_song(msg, lang, uid, url, f"yt:{vid}", None)
    else:
        await handle_link(msg, bot, lang, url, h, uid)


# ---------------- video / audio yuborilganda ----------------
async def run_circle(msg: Message, src: Path, lang: str, uid: int):
    status = await msg.answer(t(lang, "circle_prep"))
    out = await services.make_circle(src)
    if not out:
        return await status.edit_text(t(lang, "circle_fail"))
    try:
        await msg.answer_video_note(FSInputFile(out), length=640)
        await db.incr("circle")
    finally:
        out.unlink(missing_ok=True)
    await status.delete()


async def run_mp3(msg: Message, src: Path, lang: str):
    out = await services.to_mp3(src)
    if not out:
        return await msg.answer(t(lang, "mp3_fail"))
    try:
        if out.stat().st_size > config.MAX_UPLOAD:
            await msg.answer(t(lang, "too_big"))
        else:
            await msg.answer_audio(FSInputFile(out))
    finally:
        out.unlink(missing_ok=True)


async def show_shazam(msg: Message, info: dict, lang: str):
    await db.incr("shazam")
    qkey = uuid.uuid4().hex[:10]
    QUERIES[qkey] = f"{info['artist']} - {info['title']}"
    rows = [[InlineKeyboardButton(text=t(lang, "btn_full"), callback_data=f"sg:{qkey}")]]
    if info.get("url"):
        rows.append([InlineKeyboardButton(text="🔗 Shazam", url=info["url"])])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = t(lang, "music_found", title=esc(info["title"]), artist=esc(info["artist"]))
    if info.get("cover"):
        try:
            return await msg.answer_photo(info["cover"], caption=text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await msg.answer(text, reply_markup=kb)


async def identify(msg: Message, src: Path, lang: str):
    status = await msg.answer(t(lang, "music_search"))
    try:
        info = await services.recognize(src)
    except Exception:
        log.exception("Shazam xato")
        return await status.edit_text(t(lang, "music_err"))
    if not info:
        return await status.edit_text(t(lang, "music_none"))
    await status.delete()
    await show_shazam(msg, info, lang)


async def song_from_video(msg: Message, src: Path, lang: str, uid: int):
    """Videodagi musiqani aniqlaydi va to'liq qo'shiqni darhol yuboradi."""
    status = await msg.answer(t(lang, "music_search"))
    try:
        info = await services.recognize(src)
    except Exception:
        log.exception("Shazam xato")
        return await status.edit_text(t(lang, "music_err"))
    if not info:
        return await status.edit_text(t(lang, "music_none"))
    await status.delete()
    q = f"{info['artist']} - {info['title']}"
    if await send_song(msg, lang, uid, f"ytsearch1:{q}", f"q:{q.lower()[:80]}", q, quiet_fail=True):
        return
    # to'liq qo'shiq topilmadi (YouTube bloklangan, SoundCloud'da yo'q): videoning o'z audiosini yuboramiz
    out = await services.to_mp3(src)
    if not out:
        return await msg.answer(t(lang, "song_fail"))
    try:
        if out.stat().st_size > config.MAX_UPLOAD:
            return await msg.answer(t(lang, "too_big"))
        await msg.answer_audio(FSInputFile(out), title=info["title"][:60], performer=info["artist"][:60],
                               caption=t(lang, "music_fallback"))
    finally:
        out.unlink(missing_ok=True)


async def voice_search(msg: Message, src: Path, speech_lang: str, lang: str):
    """Ovozni foydalanuvchi aytgan tilda matnga o'giradi. Nima eshitganini ko'rsatmaydi, eng yaqin qo'shiqlarni chiqaradi."""
    status = await msg.answer(t(lang, "music_search"))
    try:
        texts = await services.transcribe_all(src, speech_lang)
    except Exception:
        log.exception("STT xato")
        texts = []
    if not texts:
        return await status.edit_text(t(lang, "voice_none"))
    await status.delete()
    await show_search(msg, texts[0], lang, texts[1:], hide_q=True)


@router.message(F.video | F.video_note)
async def on_video(m: Message, bot: Bot, u, lang: str):
    media = m.video or m.video_note
    uid = m.from_user.id
    if (media.file_size or 0) > config.MAX_DOWNLOAD_FROM_TG:
        return await m.answer(t(lang, "tg20"))
    do_circle = bool(u["auto_circle"]) and not m.video_note
    do_music = bool(u["auto_music"])
    async with job(uid) as ok:
        if not ok:
            return await m.answer(t(lang, "busy"))
        src = services.tmp(".mp4", "src")
        await bot.download(media, destination=src)
        if not (do_circle or do_music):
            return await m.answer(t(lang, "ask_action"), reply_markup=actions_kb(remember(src), lang),
                                  reply_to_message_id=m.message_id)
        try:
            if do_circle:
                await run_circle(m, src, lang, uid)
            if do_music:
                await identify(m, src, lang)
        finally:
            src.unlink(missing_ok=True)


@router.message(F.audio | F.voice)
async def on_audio(m: Message, bot: Bot, lang: str):
    media = m.audio or m.voice
    if (media.file_size or 0) > config.MAX_DOWNLOAD_FROM_TG:
        return await m.answer(t(lang, "tg20"))
    if m.voice and (m.voice.duration or 0) <= 60:
        src = services.tmp(".ogg", "voice")
        await bot.download(media, destination=src)
        key = uuid.uuid4().hex[:10]
        VOICE[key] = src
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data=f"vl:{key}:uz"),
             InlineKeyboardButton(text="🇷🇺 Русский", callback_data=f"vl:{key}:ru"),
             InlineKeyboardButton(text="🇬🇧 English", callback_data=f"vl:{key}:en")],
            [InlineKeyboardButton(text=t(lang, "btn_shazam"), callback_data=f"vl:{key}:sh")],
        ])
        return await m.answer(t(lang, "voice_lang"), reply_markup=kb, reply_to_message_id=m.message_id)
    async with job(m.from_user.id) as ok:
        if not ok:
            return await m.answer(t(lang, "busy"))
        src = services.tmp(".bin", "aud")
        await bot.download(media, destination=src)
        try:
            await identify(m, src, lang)
        finally:
            src.unlink(missing_ok=True)


@router.callback_query(F.data.startswith("vl:"))
async def voice_lang_chosen(call: CallbackQuery, lang: str):
    try:
        _, key, code = call.data.split(":")
    except ValueError:
        return await call.answer()
    src = VOICE.get(key)
    if not src or not src.exists() or not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    async with job(call.from_user.id) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        await call.answer()
        VOICE.pop(key, None)
        msg = call.message
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
        try:
            if code == "sh":
                await identify(msg, src, lang)
            else:
                await voice_search(msg, src, code, lang)
        finally:
            src.unlink(missing_ok=True)


def is_action(c: CallbackQuery) -> bool:
    return (c.data or "").split(":", 1)[0] in ("circle", "music", "mp3")


@router.callback_query(is_action)
async def on_action(call: CallbackQuery, bot: Bot, lang: str):
    action, key = call.data.split(":", 1)
    if not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    uid = call.from_user.id
    async with job(uid) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        src = await source_file(call, key, bot)
        if not src:
            return await call.answer(t(lang, "file_old"), show_alert=True)
        await call.answer()
        if action == "circle":
            await run_circle(call.message, src, lang, uid)
        elif action == "music":
            await song_from_video(call.message, src, lang, uid)
        else:
            await run_mp3(call.message, src, lang)


# ---------------- qo'shiq qidirish va yuklash ----------------
def song_kb(uname: str, lang: str, sid: int | None = None, faved: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎧 8D", callback_data="fx:8d"),
         InlineKeyboardButton(text="🏟 Concert Hall", callback_data="fx:hall")],
        [InlineKeyboardButton(text="🐢 Slowed", callback_data="fx:slow")],
    ]
    if sid:
        rows.append([InlineKeyboardButton(text=t(lang, "fav_on" if faved else "fav_off"), callback_data=f"fv:{sid}")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_group"), url=f"https://t.me/{uname}?startgroup=true")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


FX = {
    "8d": ("aformat=channel_layouts=stereo,apulsator=hz=0.125", "8D"),
    "hall": ("aecho=0.8:0.9:700|1100:0.3|0.2", "Concert Hall"),
    "slow": ("asetrate=44100*0.86,aresample=44100,aecho=0.8:0.88:40:0.3", "Slowed"),
}


@router.callback_query(F.data.startswith("fx:"))
async def song_fx(call: CallbackQuery, bot: Bot, lang: str):
    kind = call.data.split(":", 1)[1]
    msg = call.message
    if kind not in FX or not isinstance(msg, Message) or not msg.audio:
        return await call.answer(t(lang, "file_old"), show_alert=True)
    aud = msg.audio
    if (aud.file_size or 0) > config.MAX_DOWNLOAD_FROM_TG:
        return await call.answer(t(lang, "tg20"), show_alert=True)
    async with job(call.from_user.id) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        await call.answer()
        status = await msg.answer(t(lang, "fx_wait"))
        src = services.tmp(".m4a", "fx")
        out = None
        try:
            await bot.download(aud, destination=src)
            out = await services.apply_fx(src, FX[kind][0])
            if not out:
                return await status.edit_text(t(lang, "tool_fail"))
            uname = (await bot.me()).username
            await msg.answer_audio(FSInputFile(out, chunk_size=512 * 1024),
                                   title=f"{aud.title or 'Audio'} ({FX[kind][1]})"[:64], performer=(aud.performer or "")[:60],
                                   caption=t(lang, "song_caption", bot=uname))
            await status.delete()
        finally:
            src.unlink(missing_ok=True)
            if out:
                out.unlink(missing_ok=True)


def song_label(song) -> str:
    return f"{song.artist} - {song.title}" if song.artist else song.title


async def send_song(msg: Message, lang: str, uid: int, target: str, cache_key: str,
                    title: str | None = None, quiet_fail: bool = False) -> bool:
    """Qo'shiqni yuboradi. Muvaffaqiyatli bo'lsa True. quiet_fail=True bo'lsa, xato xabari yuborilmaydi."""
    uname = (await msg.bot.me()).username
    cap = t(lang, "song_caption", bot=uname)
    row = await db.cache_get(cache_key)
    if row:
        try:
            sid = await db.song_seen(cache_key, target, title or row["title"] or "")
            kb = song_kb(uname, lang, sid, await db.fav_has(uid, sid))
            await msg.answer_audio(json.loads(row["payload"])[0][1], caption=cap, reply_markup=kb)
            await db.incr("download")
            await db.song_bump(sid)
            return True
        except TelegramBadRequest:
            await db.cache_del(cache_key)
    status = None if services.prefetched_ready(target) else await msg.answer(t(lang, "song_wait"))
    try:
        song = await services.get_prefetched(target) or await services.download_song(target, title)
    except Exception as e:
        log.warning("Qo'shiq xato (%s): %s", target, str(e)[:300])
        song = None

    async def fail(key: str) -> bool:
        if quiet_fail:
            if status:
                try:
                    await status.delete()
                except TelegramBadRequest:
                    pass
        else:
            await (status.edit_text(t(lang, key)) if status else msg.answer(t(lang, key)))
        return False

    if not song:
        return await fail("song_fail")
    try:
        if song.path.stat().st_size > config.MAX_UPLOAD:
            return await fail("too_big")
        label = song_label(song)
        sid = await db.song_seen(cache_key, target, label)
        kb = song_kb(uname, lang, sid, await db.fav_has(uid, sid))
        t_up = time.monotonic()
        sent = await msg.answer_audio(FSInputFile(song.path, chunk_size=512 * 1024),
                                      title=song.title[:60], performer=song.artist[:60], caption=cap, reply_markup=kb)
        log.info("TIMING telegramga yuborish %.1fs (%d KB)", time.monotonic() - t_up, song.path.stat().st_size // 1024)
        await db.cache_put(cache_key, [["audio", sent.audio.file_id]], label)
        await db.incr("download")
        await db.bump_downloads(uid)
        await db.song_bump(sid)
        if status:
            await status.delete()
        return True
    finally:
        song.path.unlink(missing_ok=True)


# ---------------- /top va sevimlilar ----------------
FAV_PAGE = 8


@router.message(Command("top"))
async def cmd_top(m: Message, lang: str):
    rows = await db.top_songs(10)
    if not rows:
        return await m.answer(t(lang, "top_empty"))
    lines = [f"{i}. {esc(r['title'][:60])} — {r['n']}×" for i, r in enumerate(rows, 1)]
    btns = [InlineKeyboardButton(text=str(i), callback_data=f"tp:{r['id']}") for i, r in enumerate(rows, 1)]
    kb = InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 5] for i in range(0, len(btns), 5)])
    await m.answer(t(lang, "top_title") + "\n\n" + "\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("tp:"))
async def pick_saved(call: CallbackQuery, lang: str):
    try:
        sid = int(call.data.split(":", 1)[1])
    except ValueError:
        return await call.answer()
    row = await db.song_get(sid)
    if not row or not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    async with job(call.from_user.id) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        await call.answer()
        await send_song(call.message, lang, call.from_user.id, row["target"], row["ckey"], row["title"])


async def fav_view(uid: int, lang: str, page: int):
    total = await db.fav_count(uid)
    if not total:
        return t(lang, "fav_empty"), None
    pages = (total + FAV_PAGE - 1) // FAV_PAGE
    page = max(0, min(page, pages - 1))
    rows = await db.fav_list(uid, page * FAV_PAGE, FAV_PAGE)
    start = page * FAV_PAGE + 1
    lines = [f"{i}. {esc(r['title'][:60])}" for i, r in enumerate(rows, start)]
    btns = [InlineKeyboardButton(text=str(i), callback_data=f"tp:{r['id']}") for i, r in enumerate(rows, start)]
    kbrows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"fl:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"fl:{page + 1}"))
    if nav:
        kbrows.append(nav)
    return f"{t(lang, 'fav_title')} ({total})\n\n" + "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kbrows)


@router.message(Command("fav"))
async def cmd_fav(m: Message, lang: str):
    text, kb = await fav_view(m.from_user.id, lang, 0)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("fl:"))
async def fav_page(call: CallbackQuery, lang: str):
    try:
        page = int(call.data.split(":", 1)[1])
    except ValueError:
        return await call.answer()
    text, kb = await fav_view(call.from_user.id, lang, page)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("fv:"))
async def fav_toggle(call: CallbackQuery, lang: str):
    try:
        sid = int(call.data.split(":", 1)[1])
    except ValueError:
        return await call.answer()
    if not await db.song_get(sid):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    added = await db.fav_toggle(call.from_user.id, sid)
    uname = (await call.bot.me()).username
    try:
        await call.message.edit_reply_markup(reply_markup=song_kb(uname, lang, sid, added))
    except TelegramBadRequest:
        pass
    await call.answer(t(lang, "fav_added" if added else "fav_removed"))


PAGE = 10
SEARCH: dict[str, dict] = {}


def fmt_dur(s: int) -> str:
    return f"{s // 60}:{s % 60:02d}" if s else ""


def search_text(q: str, res: list, page: int, hide: bool = False) -> str:
    chunk = res[page * PAGE:(page + 1) * PAGE]
    lines = [f"{page * PAGE + i}. {esc(r['title'][:70])} <b>{fmt_dur(r['duration'])}</b>".rstrip()
             for i, r in enumerate(chunk, 1)]
    head = "🎤" if hide else f"🔍 <code>{esc(q)}</code>"
    return f"{head}\n\n" + "\n".join(lines)


def search_kb(sid: str, res: list, page: int) -> InlineKeyboardMarkup:
    n = len(res[page * PAGE:(page + 1) * PAGE])
    rows, row = [], []
    for i in range(n):
        gi = page * PAGE + i
        row.append(InlineKeyboardButton(text=str(gi + 1), callback_data=f"sp:{sid}:{gi}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️", callback_data=f"sn:{sid}:{page - 1}"),
                 InlineKeyboardButton(text="❌", callback_data=f"sx:{sid}"),
                 InlineKeyboardButton(text="➡️", callback_data=f"sn:{sid}:{page + 1}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("sp:"))
async def pick_song(call: CallbackQuery, lang: str):
    try:
        _, sid, idx = call.data.split(":")
        item = SEARCH[sid]["res"][int(idx)]
    except (ValueError, KeyError, IndexError):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    if not isinstance(call.message, Message):
        return await call.answer()
    async with job(call.from_user.id) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        await call.answer()
        await send_song(call.message, lang, call.from_user.id, item["target"], "u:" + item["target"], item["title"])


@router.callback_query(F.data.startswith("sn:"))
async def search_page(call: CallbackQuery, lang: str):
    try:
        _, sid, page = call.data.split(":")
        page = int(page)
        s_ = SEARCH[sid]
    except (ValueError, KeyError):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    pages = (len(s_["res"]) + PAGE - 1) // PAGE
    if page < 0 or page >= pages or not isinstance(call.message, Message):
        return await call.answer()
    await call.answer()
    try:
        await call.message.edit_text(search_text(s_["q"], s_["res"], page, s_.get("hide", False)),
                                     reply_markup=search_kb(sid, s_["res"], page))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("sx:"))
async def search_close(call: CallbackQuery):
    SEARCH.pop(call.data.split(":", 1)[1], None)
    await call.answer()
    if isinstance(call.message, Message):
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("sg:"))
async def full_song(call: CallbackQuery, lang: str):
    q = QUERIES.get(call.data.split(":", 1)[1])
    if not q or not isinstance(call.message, Message):
        return await call.answer(t(lang, "file_old"), show_alert=True)
    async with job(call.from_user.id) as ok:
        if not ok:
            return await call.answer(t(lang, "busy"), show_alert=True)
        await call.answer()
        await send_song(call.message, lang, call.from_user.id, f"ytsearch1:{q}", f"q:{q.lower()[:80]}", q)


async def show_search(msg: Message, q: str, lang: str, extra: list[str] | None = None, hide_q: bool = False):
    q = q.strip()[:100]
    if len(q) < 2:
        return await msg.answer(t(lang, "short_q"))
    status = await msg.answer("🎤 ..." if hide_q else t(lang, "search_wait", q=esc(q)))
    queries = [q] + [x[:100] for x in (extra or []) if x.strip().lower() != q.lower()]
    t_s = time.monotonic()
    results = await services.search_multi(queries, lang)
    log.info("TIMING qidiruv jami %.1fs natija=%d", time.monotonic() - t_s, len(results))
    if not results:
        return await status.edit_text(t(lang, "search_none"))
    await db.incr("search")
    warm.note_query(q)   # mashhur so'rovlarni sanaymiz (oldindan tayyorlash uchun)
    sid = uuid.uuid4().hex[:8]
    SEARCH[sid] = {"q": q, "res": results, "hide": hide_q}
    await status.edit_text(search_text(q, results, 0, hide_q), reply_markup=search_kb(sid, results, 0))
    if config.PREFETCH > 0:   # 1-2-natija fonda yuklanadi (keshda yo'qlari): tugma bosilganda tayyor bo'ladi
        fresh = [r for r in results[:config.PREFETCH] if not await db.cache_get("u:" + r["target"])]
        services.prefetch(fresh)


USER_RE = re.compile(r"@([A-Za-z0-9._]{2,30})")


@router.message(is_plain)
async def on_text(m: Message, bot: Bot, lang: str):
    mt = USER_RE.fullmatch(m.text.strip())
    if mt:   # @username -> Instagram stories
        return await handle_link(m, bot, lang, f"https://www.instagram.com/stories/{mt.group(1)}/", 720, m.from_user.id)
    await show_search(m, m.text, lang)
