"""Bazani zaxiralash (SQLite backup API + gzip) va tekshirib tiklash."""
import asyncio
import gzip
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)


def _backup_sync(dst_gz: Path) -> int:
    raw = dst_gz.with_suffix("")   # .db.gz -> .db
    src = sqlite3.connect(str(config.DB_PATH))
    try:
        dst = sqlite3.connect(str(raw))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    with open(raw, "rb") as f_in, gzip.open(dst_gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw.unlink(missing_ok=True)
    return dst_gz.stat().st_size


async def make_backup() -> Path | None:
    await db.flush()
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("savedgo-%Y%m%d-%H%M.db.gz")
    out = config.TMP_DIR / name
    try:
        await asyncio.to_thread(_backup_sync, out)
        return out
    except Exception:
        log.exception("Zaxira yaratilmadi")
        return None


async def send_backup(bot, to: set[int] | list[int], note: str = "") -> int:
    """Zaxirani egalarga yuboradi. Nechta odamga yetganini qaytaradi."""
    from aiogram.types import FSInputFile
    path = await make_backup()
    if not path:
        return 0
    sent = 0
    try:
        if path.stat().st_size > config.MAX_UPLOAD:
            for aid in to:
                try:
                    await bot.send_message(aid, "⚠️ Zaxira fayli 49 MB dan katta, yuborib bo'lmadi.")
                except Exception:
                    pass
            return 0
        users = (await db.summary()).get("total", "?")
        cap = f"💾 Baza zaxirasi\n👥 Foydalanuvchilar: {users}\n{note}\n\nTiklash: faylni botga yuboring, izohga /restore yozing."
        for aid in to:
            try:
                await bot.send_document(aid, FSInputFile(path), caption=cap.strip())
                sent += 1
            except Exception as e:
                log.warning("Zaxira yuborilmadi (%s): %s", aid, str(e)[:100])
        if sent:
            await db.set_setting("last_backup", str(int(time.time())))
    finally:
        path.unlink(missing_ok=True)
    return sent


async def backup_loop(bot) -> None:
    if config.BACKUP_HOURS <= 0:
        return
    await asyncio.sleep(300)
    while True:
        try:
            last = int(db.setting("last_backup", "0") or 0)
            if time.time() - last >= config.BACKUP_HOURS * 3600:
                await send_backup(bot, config.OWNER_IDS, "Avtomatik zaxira.")
        except Exception:
            log.exception("backup_loop xatosi")
        await asyncio.sleep(1800)


def validate_sync(src: Path, out: Path) -> str | None:
    """Fayl (.db yoki .db.gz) yaroqli SQLite bazasimi? Yaroqli bo'lsa out ga yozadi, aks holda xato matni."""
    try:
        with open(src, "rb") as f:
            magic = f.read(2)
        if magic == b"\x1f\x8b":
            with gzip.open(src, "rb") as f_in, open(out, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        else:
            shutil.copyfile(src, out)
        con = sqlite3.connect(str(out))
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "Fayl shikastlangan (integrity_check)."
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "users" not in tables:
                return "Bu bot bazasi emas (users jadvali yo'q)."
        finally:
            con.close()
        return None
    except Exception as e:
        return f"Fayl o'qilmadi: {str(e)[:100]}"
