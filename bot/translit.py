"""Lotin <-> kirill (o'zbek) o'tkazish: qidiruvda ikkala yozuvdagi natijalarni topish uchun."""
import re

_APOS = re.compile("[\u02bb\u02bc\u2018\u2019\u0060\u00b4\u2032]")

_C2L = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "'", "ы": "i", "ь": "",
    "э": "e", "ю": "yu", "я": "ya", "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
}

# uzun (ikki harfli) birikmalar avval tekshiriladi
_L2C_DI = {"o'": "ў", "g'": "ғ", "sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю", "ya": "я", "ye": "е", "ts": "ц"}
_L2C = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ", "i": "и", "j": "ж", "k": "к",
    "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "x": "х", "y": "й", "z": "з", "c": "к", "w": "в", "'": "",
}


def norm_apos(s: str) -> str:
    return _APOS.sub("'", s)


def _count(s: str) -> tuple[int, int]:
    cyr = sum(1 for ch in s if "\u0400" <= ch <= "\u04ff")
    lat = sum(1 for ch in s if ("a" <= ch.lower() <= "z"))
    return cyr, lat


def to_latin(s: str) -> str:
    return "".join(_C2L.get(ch, ch) for ch in s.lower())


def to_cyrillic(s: str) -> str:
    s = norm_apos(s.lower())
    out, i = [], 0
    while i < len(s):
        two = s[i:i + 2]
        if two in _L2C_DI:
            out.append(_L2C_DI[two])
            i += 2
            continue
        ch = s[i]
        out.append(_L2C.get(ch, ch))
        i += 1
    return "".join(out)


def alt(q: str) -> str | None:
    """So'rovning boshqa yozuvdagi varianti (agar farq qilsa). Aralash yozuvda None."""
    cyr, lat = _count(q)
    if cyr and not lat:
        res = to_latin(q)
    elif lat and not cyr:
        res = to_cyrillic(q)
    else:
        return None
    res = re.sub(r"\s+", " ", res).strip()
    return res if res and res != q.lower().strip() else None


def expand(queries: list[str]) -> list[str]:
    """[q, boshqa yozuvdagi q, ...qolganlari]: birinchi so'rov uchun variant qo'shadi."""
    if not queries:
        return queries
    a = alt(queries[0])
    if not a or a.lower() in [x.lower() for x in queries]:
        return queries
    return [queries[0], a] + queries[1:]
