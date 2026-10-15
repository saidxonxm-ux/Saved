import os
import tempfile
from pathlib import Path


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()}


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_IDS = _ids(os.getenv("ADMIN_IDS", ""))   # egalar (o'chirib bo'lmaydi)
ADMIN_IDS = set(OWNER_IDS)                      # egalar + panel orqali qo'shilgan adminlar (ish vaqtida o'zgaradi)
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "bot.db"
TMP_DIR = Path(tempfile.gettempdir()) / "tgbot"
COOKIES_FILE = os.getenv("COOKIES_FILE") or None
MAX_JOBS = int(os.getenv("MAX_JOBS", "8"))

MAX_UPLOAD = 49 * 1024 * 1024            # botdan yuborish limiti (50 MB dan biroz past)
MAX_DOWNLOAD_FROM_TG = 20 * 1024 * 1024  # botga yuborilgan fayl limiti
FILE_TTL = 3600                          # vaqtinchalik fayllar 1 soat
MAX_VIDEO_SEC = int(os.getenv("MAX_VIDEO_MIN", "30")) * 60
MAX_SONG_SEC = 15 * 60
SUB_CACHE_TTL = 300                      # obuna tekshiruvi natijasi necha soniya eslab qolinadi

REF_BONUS = 3                                       # har bir taklif uchun kunlik limitga qo'shiladi
PREMIUM_STARS = int(os.getenv("PREMIUM_STARS", "100"))
PREMIUM_DAYS = int(os.getenv("PREMIUM_DAYS", "30"))

PROXY_URL = os.getenv("PROXY_URL") or None          # ixtiyoriy: yashash proksisi (YouTube uchun)
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or None    # ixtiyoriy: ovozni matnga o'girish (Whisper)
COOKIES_B64 = os.getenv("COOKIES_B64") or None      # ixtiyoriy: cookies.txt base64 ko'rinishida
FFMPEG_JOBS = int(os.getenv("FFMPEG_JOBS", "3"))
RESOLVE_JOBS = int(os.getenv("RESOLVE_JOBS", "16"))
UZ_BIAS = os.getenv("UZ_BIAS", "1") == "1"   # o'zbek tilidagi foydalanuvchiga o'zbekcha qo'shiqlar birinchi chiqadi
PREFETCH = int(os.getenv("PREFETCH", "2"))   # qidiruvdan keyin nechta qo'shiq fonda oldindan yuklanadi (0 = o'chirilgan)
SEARCH_HEDGE = float(os.getenv("SEARCH_HEDGE", "3"))   # birinchi qidiruv shuncha soniyada javob bermasa, zaxira parallel ishga tushadi
