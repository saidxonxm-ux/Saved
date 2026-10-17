import asyncio
import html as _html
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import alerts, config, translit

try:
    from shazamio import Shazam
except Exception:  # pragma: no cover
    Shazam = None

log = logging.getLogger(__name__)

JOBS = asyncio.Semaphore(config.MAX_JOBS)
FF = asyncio.Semaphore(config.FFMPEG_JOBS)
RSEM = asyncio.Semaphore(config.RESOLVE_JOBS)
SSEM = asyncio.Semaphore(8)
_shazam = None


class Skipped(Exception):
    """Yuklash ataylab o'tkazib yuborildi: reason = 'long' | 'live'."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Result:
    files: list[Path] = field(default_factory=list)
    title: str = ""


@dataclass
class Song:
    path: Path
    title: str
    artist: str


def tmp(ext: str, prefix: str = "f") -> Path:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    return config.TMP_DIR / f"{prefix}_{uuid.uuid4().hex[:10]}{ext}"


# ---------------- ffmpeg ----------------
async def ffmpeg(*args: str, timeout: int = 300) -> bool:
    async with FF:
        return await _ffmpeg(*args, timeout=timeout)


async def _ffmpeg(*args: str, timeout: int = 300) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    if proc.returncode != 0:
        log.warning("ffmpeg xato: %s", err.decode(errors="ignore")[-400:])
    return proc.returncode == 0


async def make_circle(src: Path) -> Path | None:
    out = tmp(".mp4", "circle")
    ok = await ffmpeg(
        "-i", str(src), "-t", "60",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-maxrate", "900k", "-bufsize", "1800k",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(out))
    return out if ok and out.exists() else None


async def to_mp3(src: Path) -> Path | None:
    out = tmp(".mp3", "audio")
    ok = await ffmpeg("-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(out))
    return out if ok and out.exists() else None


async def shrink(src: Path) -> Path | None:
    """Katta videoni 480p ga kichraytiradi. Sig'masa None."""
    out = tmp(".mp4", "small")
    ok = await ffmpeg(
        "-i", str(src), "-vf", "scale=-2:'min(480,ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out), timeout=900)
    if ok and out.exists() and out.stat().st_size <= config.MAX_UPLOAD:
        return out
    out.unlink(missing_ok=True)
    return None


# ---------------- Shazam ----------------
async def recognize(src: Path) -> dict | None:
    global _shazam
    if Shazam is None:
        return None
    if _shazam is None:
        _shazam = Shazam()
    sample = tmp(".mp3", "sample")
    try:
        if not await ffmpeg("-i", str(src), "-vn", "-t", "30", "-ac", "1", "-ar", "44100", str(sample)):
            return None
        fn = getattr(_shazam, "recognize", None) or getattr(_shazam, "recognize_song")
        res = await fn(str(sample))
    finally:
        sample.unlink(missing_ok=True)
    track = (res or {}).get("track")
    if not track:
        return None
    return {
        "title": track.get("title", "?"),
        "artist": track.get("subtitle", "?"),
        "url": track.get("url"),
        "cover": (track.get("images") or {}).get("coverarthq") or (track.get("images") or {}).get("coverart"),
    }


# ---------------- yt-dlp ----------------
YT_HOSTS = ("youtube.com", "youtu.be")
IMG_EXT = {"jpg", "jpeg", "png", "webp"}
NO_SCRAPE = ("instagram.com", "facebook.com", "fb.watch", "youtube.com", "youtu.be", "tiktok.com")
YT_CLIENTS = [["android_vr"], ["tv"], None]   # None = standart
# bu xabarlar YouTube serverni bloklaganini bildiradi: boshqa klient sinaladi, hammasi o'tmasa kutish rejimi yoqiladi
YT_BLOCK_HINTS = ("sign in", "bot", "not available", "reloaded", "403", "forbidden", "unable to download",
                  "po token", "429", "too many requests")
_best = [0]
_yt_block_until = [0.0]   # YouTube bloklasa, shu vaqtgacha urinmaymiz (sekinlashishning oldini oladi)


def yt_blocked() -> bool:
    return time.monotonic() < _yt_block_until[0]


def yt_auth() -> bool:
    """YouTube uchun cookies yoki proksi sozlanganmi."""
    return bool(config.PROXY_URL or (config.COOKIES_FILE and Path(config.COOKIES_FILE).exists()))


def is_youtube(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in YT_HOSTS) or u.startswith("ytsearch")


def _ig_public(url: str) -> bool:
    """Instagram post/reels (stories emas): cookies'siz ishlaydi, cookies bilan esa 400 xato berishi mumkin."""
    u = url.lower()
    return "instagram.com" in u and "/stories/" not in u


def needs_proxy(url: str) -> bool:
    """Proksi faqat kerakli joyda: YouTube va Instagram stories. Qolganlari (SoundCloud, reels...) proksisiz tez va bepul."""
    u = url.lower()
    return is_youtube(u) or "/stories/" in u


def _base(clients=None, cookies: bool = True, proxy: bool = False) -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True,
         "socket_timeout": 20, "retries": 2, "noplaylist": True}
    if cookies and config.COOKIES_FILE and Path(config.COOKIES_FILE).exists():
        o["cookiefile"] = config.COOKIES_FILE
    if proxy and config.PROXY_URL:
        o["proxy"] = config.PROXY_URL
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    return o


def _with_clients(target: str, fn):
    """YouTube 'bot' tekshiruvi chiqsa, boshqa klientlar bilan urinib ko'radi."""
    if not is_youtube(target):
        return fn(None)
    if yt_blocked():
        raise DownloadError("YouTube vaqtincha bloklangan (kutish rejimi)")
    n = len(YT_CLIENTS)
    last = None
    for k in range(n):
        idx = (_best[0] + k) % n
        try:
            res = fn(YT_CLIENTS[idx])
            _best[0] = idx
            _yt_block_until[0] = 0.0
            return res
        except Skipped:
            raise
        except DownloadError as e:
            last = e
            msg = str(e).lower()
            if not any(k in msg for k in YT_BLOCK_HINTS):
                raise
            log.info("YouTube klient %s o'tmadi: %s", YT_CLIENTS[idx], msg[:80])
    wait = 600 if yt_auth() else 6 * 3600   # cookies/proksi bo'lmasa, tez-tez urinish foydasiz: har urinish foydalanuvchini ~6 s kutdiradi
    _yt_block_until[0] = time.monotonic() + wait
    log.warning("YouTube bloklangan: %d daqiqa SoundCloud'ga o'tildi (cookies/proksi: %s)", wait // 60, yt_auth())
    alerts.notify("yt_block", "⚠️ YouTube serverni blokladi. Qo'shiqlar SoundCloud'dan olinmoqda, YouTube havolalari ishlamaydi.\n"
                              "Yechim: yashash proksisi (PROXY_URL) yoki YouTube cookies.", 6 * 3600)
    raise last


def video_format(h: int) -> str:
    return (f"bv*[height<={h}][vcodec^=avc]+ba[acodec^=mp4a]"
            f"/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
            f"/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b[height<={h}]/b")


def _download_once(url: str, height: int, clients, max_items: int = 10, cookies: bool = True,
                   proxy: bool | None = None) -> Result:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:8]
    skip: dict[str, str] = {}

    def flt(info, *, incomplete):
        if info.get("is_live"):
            skip["r"] = "live"
            return "live"
        d = info.get("duration")
        if d and d > config.MAX_VIDEO_SEC:
            skip["r"] = "long"
            return "long"
        return None

    opts = _base(clients, cookies, needs_proxy(url) if proxy is None else proxy) | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}_%(id).50s.%(ext)s"),
        "format": video_format(height), "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 4, "http_chunk_size": 10485760,
        "playlist_items": f"1-{max_items}", "match_filter": flt,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    found: list[Path] = []
    entries = (info or {}).get("entries")
    for e in (list(entries) if entries else [info]):
        for d in (e or {}).get("requested_downloads") or []:
            p = Path(d.get("filepath", ""))
            if p.is_file() and p not in found:
                found.append(p)
    if not found:
        found = sorted(config.TMP_DIR.glob(f"{prefix}_*"))
    bad = {".part", ".ytdl", ".json", ".vtt", ".temp"}
    found = [p for p in found if p.suffix.lower() not in bad]
    if not found and skip:
        raise Skipped(skip["r"])
    return Result(found, (info or {}).get("title") or "")


# ---------------- Instagram: zaxira yo'llar (embed sahifasi, rasmli postlar) ----------------
_IG_CODE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)", re.I)
_ig_cool = [0.0]   # Instagram anonim so'rovni cheklasa, shu vaqtgacha yt-dlp bilan urinmaymiz


def ig_cooling() -> bool:
    return time.monotonic() < _ig_cool[0]


def _ig_limited(err) -> bool:
    m = str(err).lower()
    return "login page" in m or "rate-limit" in m or "rate limit" in m


def _http_get(url: str, proxy: str | None = None, timeout: int = 15, limit: int = 3_000_000) -> bytes:
    import urllib.request
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
        return r.read(limit)


def _ig_embed_urls(html_text: str, key: str = "display_url") -> list[str]:
    """Embed sahifasidagi havolalar: key='display_url' (rasm) yoki 'video_url' (video)."""
    found: list[str] = []
    for m in re.finditer(r'\\?"%s\\?"\s*:\s*\\?"(.+?)\\?"\s*[,}]' % key, html_text):
        found.append(m.group(1))
    if key == "display_url":
        for m in re.finditer(r'class="[^"]*EmbeddedMediaImage[^"]*"[^>]*?src="([^"]+)"', html_text):
            found.append(m.group(1))
    out, seen = [], set()
    for u in found:
        u = u.replace("\\\\u0026", "&").replace("\\u0026", "&").replace("\\\\/", "/").replace("\\/", "/")
        u = _html.unescape(u)
        if not u.startswith("http"):
            continue
        k = u.split("?")[0]
        if k not in seen:
            seen.add(k)
            out.append(u)
    return out[:10]


def _ig_generic(page: str) -> tuple[list[str], list[str]]:
    """Sahifadagi barcha CDN havolalarini topadi (format o'zgargan bo'lsa ham): (videolar, rasmlar)."""
    txt = page.replace("\\\\/", "/").replace("\\/", "/").replace("\\\\u0026", "&").replace("\\u0026", "&")
    txt = _html.unescape(txt)
    stop = r'[^"\'\s<>\\]'
    vids = re.findall(r'https://%s+?\.mp4%s*' % (stop, stop), txt)
    imgs = [u for u in re.findall(r'https://%s*cdninstagram\.com%s*?\.jpg%s*' % (stop, stop, stop), txt)
            if "t51.2885-19" not in u and "s150x150" not in u and "s320x320" not in u]
    def dedupe(lst):
        out, seen = [], set()
        for u in lst:
            k = u.split("?")[0]
            if k not in seen:
                seen.add(k)
                out.append(u)
        return out[:10]
    return dedupe(vids), dedupe(imgs)


def _ig_ytdlp_images(url: str) -> list[str]:
    """yt-dlp post ma'lumotini oladi; rasmli elementlarning havolasi 'thumbnail' ichida bo'ladi."""
    opts = _base(cookies=False) | {"skip_download": True, "ignore_no_formats_error": True, "noplaylist": False}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    items = (info or {}).get("entries") or [info]
    urls: list[str] = []
    for e in items:
        if not e or e.get("formats"):   # videoli element: rasm emas
            continue
        u = e.get("thumbnail")
        if not u:
            th = e.get("thumbnails") or []
            u = th[-1].get("url") if th else None
        if u and u.startswith("http") and u not in urls:
            urls.append(u)
    return urls[:10]


def _ig_save(urls: list[str], proxy: str | None = None, ext: str = ".jpg",
             limit: int = 3_000_000, min_size: int = 2000) -> Result | None:
    files = []
    for i, u in enumerate(urls):
        try:
            data = _http_get(u, proxy, 30, limit)
        except Exception as e:
            log.info("Instagram fayl yuklanmadi: %s", str(e)[:100])
            continue
        if len(data) < min_size or len(data) >= limit:
            continue
        f = config.TMP_DIR / f"igp_{uuid.uuid4().hex[:6]}_{i}{ext}"
        f.write_bytes(data)
        files.append(f)
    return Result(files, "") if files else None


def _ig_photos(url: str) -> Result | None:
    """yt-dlp o'tmasa zaxira: 1) rasmli post (yt-dlp ma'lumotidan), 2) embed sahifasidan video yoki rasm."""
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not ig_cooling():
        try:
            urls = _ig_ytdlp_images(url)
            log.info("Instagram rasm (yt-dlp): %d ta havola", len(urls))
            res = _ig_save(urls) if urls else None
            if res:
                return res
        except Exception as e:
            log.info("Instagram rasm (yt-dlp) xato: %s", str(e)[:150])
    m = _IG_CODE.search(url)
    if not m:
        return None
    code = m.group(1)
    for proxy in ([None, config.PROXY_URL] if config.PROXY_URL else [None]):
        try:
            page = _http_get(f"https://www.instagram.com/p/{code}/embed/captioned/", proxy).decode("utf-8", "ignore")
            vids = _ig_embed_urls(page, "video_url")
            imgs = _ig_embed_urls(page)
            log.info("Instagram embed (proksi=%s): %d bayt, video=%d, rasm=%d, login=%s",
                     bool(proxy), len(page), len(vids), len(imgs), "login" in page[:3000].lower())
            if not vids and not imgs:
                gv, gi = _ig_generic(page)
                vids, imgs = gv, gi
                i = page.find(".mp4")
                log.info("Instagram embed tahlil: mp4=%d video_url=%d display_url=%d contextJSON=%d shortcode_media=%d cdn=%d umumiy_video=%d umumiy_rasm=%d boshi=%r mp4_atrofi=%r",
                         page.count(".mp4"), page.count("video_url"), page.count("display_url"),
                         page.count("contextJSON"), page.count("shortcode_media"), page.count("cdninstagram"),
                         len(gv), len(gi), re.sub(r"\s+", " ", page[:150]),
                         re.sub(r"\s+", " ", page[max(0, i - 60):i + 60]) if i >= 0 else "")
            res = None
            if vids:
                res = _ig_save(vids, proxy, ".mp4", config.MAX_UPLOAD + 1, 20_000)
            if not res and imgs:
                res = _ig_save(imgs, proxy)
            if res:
                return res
        except Exception as e:
            log.info("Instagram embed o'tmadi (proksi=%s): %s", bool(proxy), str(e)[:120])
    return None


def _download(url: str, height: int = 720, max_items: int = 10) -> Result:
    if _ig_public(url):
        err = None
        if not ig_cooling():
            try:   # 1) cookies'siz, proksisiz (tez va ishonchli)
                return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, False, False))
            except Skipped:
                raise
            except Exception as e:
                err = e
                log.info("Instagram cookies'siz o'tmadi: %s", str(e)[:120])
                if _ig_limited(e):   # Instagram bu IP'ni vaqtincha cheklagan: 10 daqiqa bekorga so'ramaymiz
                    _ig_cool[0] = time.monotonic() + 600
                    log.warning("Instagram anonim so'rovni cheklagan: 10 daqiqa yt-dlp o'tkazib yuboriladi")
                    alerts.notify("ig_limit", "⚠️ Instagram serverni cheklagan (login talab qilyapti). Reels/postlar yuklanmasligi mumkin.\n"
                                              "Yechim: yashash proksisi (PROXY_URL).", 3 * 3600)
        if config.PROXY_URL:   # 2) proksi orqali
            try:
                return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, False, True))
            except Skipped:
                raise
            except Exception as e:
                err = e
                log.info("Instagram proksi bilan o'tmadi: %s", str(e)[:120])
        s = str(err or "")
        if err is None or _ig_limited(err) or "No video formats" in s or "empty media" in s:   # 3) zaxira
            res = _ig_photos(url)
            if res:
                return res
        if config.COOKIES_FILE and Path(config.COOKIES_FILE).exists():   # 4) cookies bilan
            try:
                return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, True, False))
            except Exception as e:
                err = err or e
        raise err or DownloadError("Instagram vaqtincha cheklangan (login talab qilinadi)")
    return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, True))


_FAILS: list[float] = []


def note_fail() -> None:
    """Yuklash xatosi: 10 daqiqada ALERT_FAILS tadan oshsa, adminga xabar."""
    now = time.monotonic()
    _FAILS.append(now)
    del _FAILS[:-100]
    recent = sum(1 for x in _FAILS if now - x < 600)
    if recent >= config.ALERT_FAILS:
        alerts.notify("fails", f"❗️ Yuklash xatolari ko'p: oxirgi 10 daqiqada {recent} ta. Manbalar (YouTube/Instagram) bloklangan bo'lishi mumkin.", 1800)


async def download(url: str, height: int = 720, max_items: int = 10) -> Result:
    async with JOBS:
        try:
            return await asyncio.to_thread(_download, url, height, max_items)
        except Skipped:
            raise
        except Exception:
            note_fail()
            raise


# ---------------- tezkor yo'l: to'g'ridan-to'g'ri havola ----------------
def _resolve(url: str) -> dict | None:
    """Faylni yuklamasdan, Telegram o'zi olishi mumkin bo'lgan havolani topadi."""
    if _ig_public(url) and ig_cooling():
        return None
    opts = _base(cookies=not _ig_public(url), proxy=needs_proxy(url)) | {"format": "b[ext=mp4][height<=720]/b[height<=720]/b", "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("entries") or info.get("is_live") or info.get("requested_formats"):
        return None
    d = info.get("duration")
    if d and d > config.MAX_VIDEO_SEC:
        raise Skipped("long")
    link = info.get("url")
    if not link or not link.startswith("http") or ".m3u8" in link:
        return None
    size = info.get("filesize") or info.get("filesize_approx")
    if size and size > config.MAX_DOWNLOAD_FROM_TG:
        return None
    ext = (info.get("ext") or "").lower()
    return {"url": link, "kind": "photo" if ext in IMG_EXT else "video", "title": info.get("title") or ""}


async def resolve(url: str) -> dict | None:
    async with RSEM:
        return await asyncio.to_thread(_resolve, url)


# ---------------- zaxira: sahifadagi og:video / og:image ----------------
async def scrape_media(url: str) -> dict | None:
    if any(h in url.lower() for h in NO_SCRAPE):
        return None
    import aiohttp
    hdr = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=hdr) as s:
        async with s.get(url, allow_redirects=True) as r:
            if r.status != 200:
                return None
            page = (await r.content.read(700_000)).decode("utf-8", "ignore")

    def meta(prop: str):
        pr = re.escape(prop)
        m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)' % pr, page, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']%s["\']' % pr, page, re.I))
        return _html.unescape(m.group(1)) if m else None

    v = meta("og:video:secure_url") or meta("og:video:url") or meta("og:video")
    if v and v.startswith("http"):
        return {"url": v, "kind": "video", "title": meta("og:title") or ""}
    i = meta("og:image")
    if i and i.startswith("http"):
        return {"url": i, "kind": "photo", "title": meta("og:title") or ""}
    return None


# ---------------- qo'shiq: yuklash va qidirish ----------------
def _song_opts(prefix: str, clients=None, proxy: bool = False) -> dict:
    def flt(info, *, incomplete):
        d = info.get("duration")
        return "long" if d and d > config.MAX_SONG_SEC else None

    return _base(clients, proxy=proxy) | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}.%(ext)s"),
        # m4a/mp3 bo'lsa o'zgartirmasdan yuboriladi (konvertatsiya 1-3 soniya oladi)
        "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
        "match_filter": flt,
    }


def _finish_song(prefix: str, entry: dict) -> Song | None:
    files = sorted(config.TMP_DIR.glob(f"{prefix}.*"))
    ok = [p for p in files if p.suffix.lower() in (".m4a", ".mp3")]
    if not ok and files:   # Telegram faqat mp3/m4a qabul qiladi: boshqa format bo'lsa o'giramiz
        out = config.TMP_DIR / f"{prefix}.mp3"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(files[0]), "-vn",
                            "-c:a", "libmp3lame", "-b:a", "192k", str(out)], capture_output=True, timeout=180)
        if r.returncode == 0 and out.exists():
            ok = [out]
    for p in files:
        if p not in ok:
            p.unlink(missing_ok=True)
    if not ok:
        return None
    return Song(ok[0], entry.get("track") or entry.get("title") or "Audio",
                entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")


def _song_once(target: str, clients) -> Song | None:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "song_" + uuid.uuid4().hex[:8]
    with YoutubeDL(_song_opts(prefix, clients, needs_proxy(target))) as ydl:
        info = ydl.extract_info(target, download=True)
    entries = (info or {}).get("entries")
    entry = (list(entries)[0] if entries else info) or {}
    return _finish_song(prefix, entry)


def _clean_query(q: str) -> str:
    """'Nom (Official Video) #tag' -> 'Nom': SoundCloud'da yaxshiroq topiladi."""
    q = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", q)
    q = re.sub(r"#\w+", " ", q)
    q = re.sub(r"(?i)\b(official|music|video|audio|lyrics|lyric|hd|4k|full|mv|clip|klip|premyera|yangi)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip(" -–|")


def _sc_download(q: str) -> Song | None:
    """SoundCloud: DRM bilan himoyalangan natijalarni o'tkazib, birinchi yuklanadiganini oladi."""
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "song_" + uuid.uuid4().hex[:8]
    got: dict = {}

    def hook(d):
        if d.get("status") == "finished" and not got:
            got.update(d.get("info_dict") or {})

    opts = _song_opts(prefix) | {"ignoreerrors": True, "max_downloads": 1, "playlist_items": "1-6",
                                 "progress_hooks": [hook]}
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([f"scsearch6:{q}"])
    except Exception as e:
        log.info("SoundCloud xato: %s", str(e)[:150])
    return _finish_song(prefix, got)


def _fold(s: str) -> str:
    """Diakritikalarni olib tashlaydi: 'Cumamız Mübarek' -> 'Cumamiz Mubarek' (SoundCloud'da shunday ko'p yoziladi)."""
    import unicodedata
    s = s.replace("ı", "i").replace("İ", "I").replace("ə", "a").replace("Ə", "A")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _sc_variants(q: str) -> list[str]:
    """SoundCloud uchun bir nechta qidiruv varianti (tartib bilan, takrorsiz, ko'pi bilan 4 ta)."""
    cleaned = _clean_query(q) or q
    out = [cleaned]
    title = ""
    if " - " in cleaned:
        artist, title = (x.strip() for x in cleaned.split(" - ", 1))
        out.append(f"{artist} {title}")
    folded = _fold(cleaned)
    out.append(folded)
    if len(title) >= 10:   # faqat nomi: ijrochi noto'g'ri aniqlangan bo'lishi mumkin (juda qisqa nomlar chalg'itadi)
        out.append(title)
    seen, res = set(), []
    for v in out:
        k = v.lower().strip()
        if k and k not in seen:
            seen.add(k)
            res.append(v.strip())
    return res[:4]


def _song(target: str, title: str | None) -> Song | None:
    try:
        song = _with_clients(target, lambda cl: _song_once(target, cl))
        if song:
            return song
    except Exception as e:
        log.info("Qo'shiq asosiy manbadan olinmadi (%s): %s", target, str(e)[:160])
    # zaxira: SoundCloud (datacenter IP'larni bloklamaydi)
    q = title or (target.split(":", 1)[1] if target.startswith("ytsearch") else None)
    if not q:
        return None
    for v in _sc_variants(q):
        song = _sc_download(v)
        if song:
            return song
    return None


async def download_song(target: str, title: str | None = None) -> Song | None:
    async with JOBS:
        t0 = time.monotonic()
        song = await asyncio.to_thread(_song, target, title)
        log.info("TIMING yuklash %.1fs ok=%s %r", time.monotonic() - t0, bool(song), (title or target)[:40])
        if not song:
            note_fail()
        return song


def _search_flat(prefix: str, q: str, n: int) -> list[dict]:
    opts = _base(proxy=(prefix == "ytsearch")) | {"extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{n}:{q}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        d = int(e.get("duration") or 0)
        if d > config.MAX_SONG_SEC:
            continue
        link = e.get("url") or e.get("webpage_url")
        if prefix == "ytsearch":
            if not e.get("id"):
                continue
            link = f"https://www.youtube.com/watch?v={e['id']}"
        if not link:
            continue
        out.append({"target": link, "title": e.get("title") or "?", "duration": d})
    return out


_SC: dict[str, tuple[float, list]] = {}


async def _one(prefix: str, q: str, n: int) -> list[dict]:
    async with SSEM:
        t0 = time.monotonic()
        try:
            res = await asyncio.to_thread(_search_flat, prefix, q, n)
            log.info("TIMING %s %.1fs natija=%d q=%r", prefix, time.monotonic() - t0, len(res), q[:40])
            return res
        except Exception as e:
            log.info("%s qidiruvi xato (%.1fs): %s", prefix, time.monotonic() - t0, str(e)[:160])
            return []


async def search(q: str, n: int = 20) -> list[dict]:
    key = q.lower().strip()
    hit = _SC.get(key)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    # YouTube bloklangan bo'lsa, yuklab bo'ladigan SoundCloud birinchi qidiriladi
    first, second = ("scsearch", "ytsearch") if yt_blocked() else ("ytsearch", "scsearch")
    t1 = asyncio.create_task(_one(first, q, n))
    done, _ = await asyncio.wait({t1}, timeout=config.SEARCH_HEDGE)
    res = t1.result() if t1 in done else []
    if not res:
        # birinchisi sekin yoki bo'sh: zaxirani parallel ishga tushiramiz, qaysi biri natija bersa shuni olamiz
        pending = {t1, asyncio.create_task(_one(second, q, n))} if t1 not in done else \
                  {asyncio.create_task(_one(second, q, n))}
        while pending and not res:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                res = res or d.result()
    if res:
        if len(_SC) > 3000:
            _SC.clear()
        _SC[key] = (time.monotonic(), res)
    return res


# ---------------- oldindan yuklash (qidiruvdan keyin fonda) ----------------
PRE: dict[str, Song] = {}                 # tayyor qo'shiqlar: target -> Song
_PRE_TASKS: dict[str, asyncio.Task] = {}   # yuklanayotganlar
PSEM = asyncio.Semaphore(2)


def prefetched_ready(target: str) -> bool:
    s = PRE.get(target)
    return bool(s and s.path.exists())


async def _prefetch_one(target: str, title: str) -> None:
    try:
        async with PSEM:
            if JOBS.locked():   # asosiy yuklashlar band bo'lsa, fon ishi xalaqit bermaydi
                return
            song = await asyncio.to_thread(_song, target, title)
        if song:
            while len(PRE) >= 30:   # eng eskisini o'chiramiz
                old = PRE.pop(next(iter(PRE)))
                old.path.unlink(missing_ok=True)
            PRE[target] = song
    except Exception as e:
        log.info("Oldindan yuklash xato (%s): %s", target, str(e)[:120])
    finally:
        _PRE_TASKS.pop(target, None)


def prefetch(items: list[dict]) -> None:
    """Qidiruv natijalarining birinchi bir nechtasini fonda yuklab qo'yadi."""
    for it in items[:config.PREFETCH]:
        tgt = it["target"]
        if tgt in PRE or tgt in _PRE_TASKS:
            continue
        _PRE_TASKS[tgt] = asyncio.create_task(_prefetch_one(tgt, it["title"]))


async def get_prefetched(target: str) -> Song | None:
    """Tayyor bo'lsa darrov, yuklanayotgan bo'lsa tugashini kutib qaytaradi (bir marta)."""
    task = _PRE_TASKS.get(target)
    if task:
        await asyncio.shield(task)
    s = PRE.pop(target, None)
    return s if s and s.path.exists() else None


def _merge(lists: list[list[dict]], limit: int) -> list[dict]:
    """Ro'yxatlarni navbat bilan aralashtiradi (takrorlarsiz)."""
    out, seen, i = [], set(), 0
    while len(out) < limit and any(i < len(x) for x in lists):
        for x in lists:
            if i < len(x) and x[i]["target"] not in seen:
                seen.add(x[i]["target"])
                out.append(x[i])
        i += 1
    return out


async def search_multi(queries: list[str], lang: str, limit: int = 20) -> list[dict]:
    """Bir nechta so'rov (masalan, ovozdan olingan bir nechta taxmin) natijalarini birlashtiradi.
    O'zbek tilida 'uzbek' qo'shilgan so'rov natijalari birinchi chiqadi."""
    queries = [q for q in queries if q]
    if config.TRANSLIT and lang == "uz":   # "Шохрух" va "Shoxrux" ikkalasi ham qidiriladi
        queries = translit.expand(queries)
    queries = queries[:3]
    bias = config.UZ_BIAS and lang == "uz"
    biased = [f"{q} uzbek" for q in queries] if bias else []
    res = await asyncio.gather(*(search(v) for v in biased + queries), return_exceptions=True)
    lists = [r if isinstance(r, list) else [] for r in res]
    first, rest = lists[:len(biased)], lists[len(biased):]
    out = _merge(first, limit) if first else []
    seen = {x["target"] for x in out}
    out += [x for x in _merge(rest, limit) if x["target"] not in seen]
    return out[:limit]


# ---------------- ovozni matnga o'girish ----------------
STT_PROMPT = {
    "uz": "Qo'shiq nomi yoki qo'shiqchi ismi.",
    "ru": "Название песни или имя исполнителя.",
    "en": "Song title or artist name.",
}
GOOGLE_LANG = {"uz": "uz-UZ", "ru": "ru-RU", "en": "en-US"}


async def _groq(src: Path, lang: str) -> str:
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("file", src.read_bytes(), filename="voice.ogg", content_type="audio/ogg")
    form.add_field("model", "whisper-large-v3")
    form.add_field("language", lang)
    if STT_PROMPT.get(lang):
        form.add_field("prompt", STT_PROMPT[lang])
    form.add_field("response_format", "json")
    form.add_field("temperature", "0")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post("https://api.groq.com/openai/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"}, data=form) as r:
            if r.status != 200:
                log.warning("Groq xato %s: %s", r.status, (await r.text())[:200])
                return ""
            return ((await r.json()).get("text") or "").strip()


def _google_stt(wav: Path, lang: str) -> str:
    try:
        import speech_recognition as sr
    except Exception:
        return ""
    rec = sr.Recognizer()
    with sr.AudioFile(str(wav)) as s:
        audio = rec.record(s)
    try:
        return rec.recognize_google(audio, language=GOOGLE_LANG.get(lang, "en-US")).strip()
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        log.warning("Google STT xato: %s", str(e)[:150])
        return ""


async def _google_job(src: Path, lang: str) -> str:
    wav = tmp(".wav", "stt")
    try:
        if not await ffmpeg("-i", str(src), "-ac", "1", "-ar", "16000", str(wav), timeout=60):
            return ""
        return await asyncio.to_thread(_google_stt, wav, lang)
    finally:
        wav.unlink(missing_ok=True)


def _clean(text: str) -> str:
    return text.strip(" .,!?;:…\"'«»-\n")


async def transcribe_all(src: Path, lang: str) -> list[str]:
    """Bir nechta usul bilan matnga o'giradi (Groq Whisper + Google). Bir-biriga yaqin variantlar qidiruvda birlashadi."""
    tasks = []
    if config.GROQ_API_KEY:
        tasks.append(_groq(src, lang))
    tasks.append(_google_job(src, lang))
    res = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[str] = []
    for r in res:
        if isinstance(r, Exception):
            log.warning("STT istisno: %s", str(r)[:150])
            continue
        c = _clean(r)
        if len(c) >= 2 and c.lower() not in [x.lower() for x in out]:
            out.append(c)
    return out


async def make_gif(src: Path) -> Path | None:
    """Videoning boshidan 6 soniya, ovozsiz (Telegram animatsiyasi)."""
    out = tmp(".mp4", "gif")
    ok = await ffmpeg("-i", str(src), "-t", "6", "-an", "-vf", "scale='min(480,iw)':-2,fps=20",
                      "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
                      "-movflags", "+faststart", str(out))
    return out if ok and out.exists() else None


async def make_cover(src: Path) -> Path | None:
    out = tmp(".jpg", "cover")
    for ss in ("1", "0"):
        if await ffmpeg("-ss", ss, "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)) and out.exists():
            return out
    return None


async def apply_fx(src: Path, af: str) -> Path | None:
    out = tmp(".mp3", "fx")
    ok = await ffmpeg("-i", str(src), "-vn", "-af", af, "-c:a", "libmp3lame", "-b:a", "192k", str(out), timeout=240)
    return out if ok and out.exists() else None
