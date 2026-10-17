import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import aiosqlite

log = logging.getLogger(__name__)
_db: aiosqlite.Connection | None = None
_S: dict[str, str] = {}

# ---- xotira keshlari va yozuv buferi (har xabarda bazaga tegmaslik uchun) ----
USER_TTL = 60
_U: dict[int, tuple[float, object]] = {}
_CH: list | None = None
_p_stats: dict[tuple[str, str], int] = {}
_p_dl: dict[int, int] = {}
_p_hist: list[tuple[int, str, str, int]] = []
_p_cache: dict[str, tuple[str, str, int]] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
  lang TEXT DEFAULT 'uz', lang_set INTEGER DEFAULT 0,
  joined_at INTEGER, last_seen INTEGER,
  banned INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  auto_circle INTEGER DEFAULT 1, auto_music INTEGER DEFAULT 1,
  downloads INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, added_by INTEGER, ts INTEGER);
CREATE INDEX IF NOT EXISTS u_joined ON users(joined_at);
CREATE INDEX IF NOT EXISTS u_seen ON users(last_seen);
CREATE TABLE IF NOT EXISTS channels(chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS stats(day TEXT, key TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS media_cache(key TEXT PRIMARY KEY, payload TEXT, title TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, url TEXT, title TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS h_uid ON history(uid, id);
CREATE TABLE IF NOT EXISTS songs(id INTEGER PRIMARY KEY AUTOINCREMENT, ckey TEXT UNIQUE, target TEXT, title TEXT, n INTEGER DEFAULT 0, last INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS s_n ON songs(n);
CREATE TABLE IF NOT EXISTS favs(uid INTEGER, sid INTEGER, ts INTEGER, PRIMARY KEY(uid, sid));
CREATE INDEX IF NOT EXISTS f_uid ON favs(uid, ts);
"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _midnight() -> int:
    n = datetime.now(timezone.utc)
    return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


_path = None


async def init(path) -> None:
    global _db, _path
    _path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    await _db.commit()
    async with _db.execute("SELECT key, value FROM settings") as cur:
        for r in await cur.fetchall():
            _S[r["key"]] = r["value"]


async def flush() -> None:
    """Buferdagi yozuvlarni bitta tranzaksiyada bazaga yozadi."""
    if not (_p_stats or _p_dl or _p_hist or _p_cache):
        return
    stats, dl, hist, cache = dict(_p_stats), dict(_p_dl), list(_p_hist), dict(_p_cache)
    _p_stats.clear(); _p_dl.clear(); _p_hist.clear(); _p_cache.clear()
    try:
        if stats:
            await _db.executemany("INSERT INTO stats(day,key,n) VALUES(?,?,?) "
                                  "ON CONFLICT(day,key) DO UPDATE SET n = n + excluded.n",
                                  [(d, k, n) for (d, k), n in stats.items()])
        if dl:
            await _db.executemany("UPDATE users SET downloads = downloads + ? WHERE id=?", [(n, u) for u, n in dl.items()])
        if hist:
            await _db.executemany("INSERT INTO history(uid,url,title,ts) VALUES(?,?,?,?)", hist)
            for uid in {h[0] for h in hist}:
                await _db.execute("DELETE FROM history WHERE uid=? AND id NOT IN "
                                  "(SELECT id FROM history WHERE uid=? ORDER BY id DESC LIMIT 20)", (uid, uid))
        if cache:
            await _db.executemany("INSERT OR REPLACE INTO media_cache(key,payload,title,ts) VALUES(?,?,?,?)",
                                  [(k, v[0], v[1], v[2]) for k, v in cache.items()])
        await _db.commit()
    except Exception:
        log.exception("flush xatosi")


async def reopen(new_file) -> None:
    """Bazani zaxira faylidan almashtiradi (qayta ishga tushirmasdan)."""
    import shutil
    from pathlib import Path
    global _db, _CH
    await flush()
    if _db:
        await _db.close()
    path = Path(str(_path))
    for ext in ("-wal", "-shm"):
        Path(str(path) + ext).unlink(missing_ok=True)
    await asyncio.to_thread(shutil.copyfile, str(new_file), str(path))
    _U.clear()
    _S.clear()
    _CH = None
    await init(path)


async def flush_loop() -> None:
    while True:
        await asyncio.sleep(5)
        await flush()


async def close() -> None:
    if _db:
        await flush()
        await _db.close()


async def _one(sql, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


async def _all(sql, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def _run(sql, *args):
    await _db.execute(sql, args)
    await _db.commit()


# ---------- settings ----------
def setting(key: str, default: str = "") -> str:
    return _S.get(key, default)


async def set_setting(key: str, value: str) -> None:
    _S[key] = value
    await _run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


# ---------- users ----------
def _lang_from(code) -> str:
    code = (code or "")[:2].lower()
    return code if code in ("ru", "en") else "uz"


def _inv(uid: int) -> None:
    _U.pop(uid, None)


async def get_user(uid: int):
    hit = _U.get(uid)
    if hit and time.monotonic() - hit[0] < USER_TTL:
        return hit[1]
    row = await _one("SELECT * FROM users WHERE id=?", uid)
    if row is not None:
        _U[uid] = (time.monotonic(), row)
    return row


async def upsert_user(u):
    now = int(time.time())
    row = await get_user(u.id)
    if row is None:
        await _run("INSERT OR IGNORE INTO users(id,username,first_name,lang,joined_at,last_seen) VALUES(?,?,?,?,?,?)",
                   u.id, u.username, u.first_name, _lang_from(u.language_code), now, now)
    elif (now - (row["last_seen"] or 0) > 60 or row["username"] != u.username
          or row["first_name"] != u.first_name or not row["active"]):
        await _run("UPDATE users SET username=?, first_name=?, last_seen=?, active=1 WHERE id=?",
                   u.username, u.first_name, now, u.id)
    else:
        return row
    _inv(u.id)
    return await get_user(u.id)


async def set_lang(uid: int, lang: str) -> None:
    await _run("UPDATE users SET lang=?, lang_set=1 WHERE id=?", lang, uid)
    _inv(uid)


async def toggle_flag(uid: int, col: str) -> None:
    assert col in ("auto_circle", "auto_music")
    await _run(f"UPDATE users SET {col} = 1 - {col} WHERE id=?", uid)
    _inv(uid)


async def set_banned(uid: int, banned: bool) -> None:
    await _run("UPDATE users SET banned=? WHERE id=?", int(banned), uid)
    _inv(uid)


async def set_inactive(uid: int) -> None:
    await _run("UPDATE users SET active=0 WHERE id=?", uid)
    _inv(uid)


async def bump_downloads(uid: int) -> None:
    _p_dl[uid] = _p_dl.get(uid, 0) + 1


async def audience(lang: str | None = None) -> list[int]:
    if lang:
        rows = await _all("SELECT id FROM users WHERE active=1 AND banned=0 AND lang=?", lang)
    else:
        rows = await _all("SELECT id FROM users WHERE active=1 AND banned=0")
    return [r["id"] for r in rows]


async def all_users():
    return await _all("SELECT * FROM users ORDER BY joined_at")


# ---------- stats ----------
async def incr(key: str, n: int = 1) -> None:
    k = (_today(), key)
    _p_stats[k] = _p_stats.get(k, 0) + n


async def summary() -> dict:
    await flush()
    mid = _midnight()
    now = int(time.time())
    r = {}
    r["total"] = (await _one("SELECT COUNT(*) c FROM users"))["c"]
    r["new_today"] = (await _one("SELECT COUNT(*) c FROM users WHERE joined_at>=?", mid))["c"]
    r["online_24h"] = (await _one("SELECT COUNT(*) c FROM users WHERE last_seen>=?", now - 86400))["c"]
    r["today"] = {x["key"]: x["n"] for x in await _all("SELECT key, n FROM stats WHERE day=?", _today())}
    r["all"] = {x["key"]: x["n"] for x in await _all("SELECT key, SUM(n) n FROM stats GROUP BY key")}
    return r


# ---------- channels ----------
async def list_channels():
    global _CH
    if _CH is None:
        _CH = list(await _all("SELECT * FROM channels ORDER BY rowid"))
    return _CH


async def add_channel(chat_id: int, title: str, link: str) -> None:
    global _CH
    await _run("INSERT OR REPLACE INTO channels(chat_id,title,link) VALUES(?,?,?)", chat_id, title, link)
    _CH = None


async def del_channel(chat_id: int) -> None:
    global _CH
    await _run("DELETE FROM channels WHERE chat_id=?", chat_id)
    _CH = None


# ---------- file_id cache ----------
async def cache_get(key: str):
    if key in _p_cache:
        payload, title, ts = _p_cache[key]
        return {"key": key, "payload": payload, "title": title, "ts": ts}
    return await _one("SELECT * FROM media_cache WHERE key=?", key)


async def cache_put(key: str, items: list, title: str) -> None:
    _p_cache[key] = (json.dumps(items), title or "", int(time.time()))


async def cache_del(key: str) -> None:
    _p_cache.pop(key, None)
    await _run("DELETE FROM media_cache WHERE key=?", key)


# ---------- qo'shiqlar: /top, sevimlilar, inline qidiruv ----------
async def song_seen(ckey: str, target: str, title: str) -> int:
    """Qo'shiqni jadvalga yozadi (sanamaydi). Qaytaradi: qo'shiq id si."""
    await _run("INSERT INTO songs(ckey,target,title,n,last) VALUES(?,?,?,0,?) "
               "ON CONFLICT(ckey) DO UPDATE SET target=excluded.target, "
               "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE songs.title END",
               ckey, target, title or "", int(time.time()))
    return (await _one("SELECT id FROM songs WHERE ckey=?", ckey))["id"]


async def song_bump(sid: int) -> None:
    """Qo'shiq muvaffaqiyatli yuborilganda: yuklashlar sonini oshiradi."""
    await _run("UPDATE songs SET n=n+1, last=? WHERE id=?", int(time.time()), sid)


async def song_get(sid: int):
    return await _one("SELECT * FROM songs WHERE id=?", sid)


async def top_songs(n: int = 10):
    return await _all("SELECT id, ckey, title, n FROM songs WHERE title<>'' AND n>0 ORDER BY n DESC, last DESC LIMIT ?", n)


async def fav_has(uid: int, sid: int) -> bool:
    return await _one("SELECT 1 FROM favs WHERE uid=? AND sid=?", uid, sid) is not None


async def fav_toggle(uid: int, sid: int) -> bool:
    """Sevimlilarga qo'shadi yoki olib tashlaydi. True = qo'shildi."""
    if await fav_has(uid, sid):
        await _run("DELETE FROM favs WHERE uid=? AND sid=?", uid, sid)
        return False
    await _run("INSERT OR IGNORE INTO favs(uid,sid,ts) VALUES(?,?,?)", uid, sid, int(time.time()))
    return True


async def fav_count(uid: int) -> int:
    return (await _one("SELECT COUNT(*) c FROM favs WHERE uid=?", uid))["c"]


async def fav_list(uid: int, offset: int, n: int):
    return await _all("SELECT s.id, s.title FROM favs f JOIN songs s ON s.id=f.sid "
                      "WHERE f.uid=? ORDER BY f.ts DESC LIMIT ? OFFSET ?", uid, n, offset)


async def audio_cache_titles(limit: int = 3000):
    """Inline qidiruv uchun: keshdagi tayyor qo'shiqlar (key, title, file_id), yangilari birinchi."""
    await flush()
    out = []
    for r in await _all("SELECT key, payload, title FROM media_cache WHERE title<>'' ORDER BY ts DESC LIMIT ?", limit):
        try:
            items = json.loads(r["payload"])
            if items and items[0][0] == "audio":
                out.append((r["key"], r["title"], items[0][1]))
        except Exception:
            continue
    return out


# ---------- history ----------
async def history_add(uid: int, url: str, title: str) -> None:
    _p_hist.append((uid, url, title or "", int(time.time())))


async def history_list(uid: int, n: int = 10):
    await flush()
    return await _all("SELECT url, title FROM history WHERE uid=? ORDER BY id DESC LIMIT ?", uid, n)


async def daily_series(key: str, days: int = 7) -> list[tuple[str, int]]:
    from datetime import timedelta
    await flush()
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    rows = {r["day"]: r["n"] for r in await _all("SELECT day, n FROM stats WHERE key=? AND day>=?", key, start)}
    out = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        out.append((d.strftime("%m-%d"), rows.get(d.isoformat(), 0)))
    return out


# ---------- adminlar ----------
async def list_admins():
    return await _all("SELECT * FROM admins ORDER BY ts")


async def add_admin(uid: int, by: int) -> None:
    await _run("INSERT OR IGNORE INTO admins(id,added_by,ts) VALUES(?,?,?)", uid, by, int(time.time()))


async def del_admin(uid: int) -> None:
    await _run("DELETE FROM admins WHERE id=?", uid)
