# -*- coding: utf-8 -*-
"""Matnni o'zbekchaga tarjima qilish.

NEGA ALOHIDA MODUL: qwen2.5vl rasmdagi matnni **a'lo o'qiydi**, lekin o'zbekcha
tarjimasi ma'nosiz chiqadi (sinovda: "It's been ten years..." -> "O'zg'iroq",
"aflat"; ba'zan bitta so'zni cheksiz takrorlaydi). Shuning uchun ish ikkiga
bo'lindi:
    1) OCR + bbox   -> mahalliy qwen2.5vl (bepul, internetsiz, ishonchli)
    2) tarjima      -> shu modul

Dvigatellar (avtomat tanlanadi):
    google — bepul, kalitsiz endpoint; o'zbekchani yaxshi biladi, internet kerak
    mymemory — zaxira (kalitsiz, kuniga ~5000 so'z)
    local  — mahalliy model; internetsiz ishlaydi, lekin sifati past

TRANSLATE_ENGINE muhit o'zgaruvchisi: auto (standart) | google | local

TEZLIK: sahifadagi barcha pufakchalar BITTA so'rovda tarjima qilinadi
(bir nechta `q=` parametr - Google javobni aynan shu tartibda qaytaradi).
Avval har bir pufakcha uchun 2 tadan so'rov ketardi (6 pufakcha = 12 so'rov,
~5 sekund); endi sahifaga 2 ta so'rov (~1 sekund).
"""
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

ENGINE = os.getenv("TRANSLATE_ENGINE", "auto").lower()
_TIMEOUT = 15
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_GOOGLE = "https://clients5.google.com/translate_a/t"
# GET manzil uzunligi chegarasi (Google ~16 KB dan uzunini rad etadi)
_MAX_URL = 7000

_cache: dict[str, str] = {}

# Harfsiz matn ("...", "?!", "!!!", "—") tarjima qilinmaydi - aslicha qoladi
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------- tozalash

def normalize_source(text: str) -> str:
    """Tarjimadan OLDIN matnni tozalaydi.

    - ko'p qatorli pufakcha -> bitta qator (qator uzilishi tarjimonni chalg'itadi)
    - KATTA HARFLI inglizcha (skanlatsiyalarda odatiy) -> oddiy yozuv:
      "I CAN'T BELIEVE IT" ni Google ba'zan qisqartma deb o'ylab buzadi
    - OCR ning takroriy tinish belgilari ("!!!!!!") -> qisqartiriladi
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    letters = [c for c in t if c.isalpha()]
    latin_upper = [c for c in letters if "A" <= c <= "Z"]
    if len(letters) >= 4 and len(latin_upper) / len(letters) > 0.8:
        t = t.lower()
        t = re.sub(r"(^|[.!?…]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
        t = re.sub(r"\bi\b", "I", t)
        t = re.sub(r"\bi'", "I'", t)
    t = re.sub(r"([!?.])\1{3,}", r"\1\1\1", t)
    # OCR ko'p nuqtaning bittasini yo'qotadi: "CHECK.." -> "CHECK..."
    t = re.sub(r"(?<!\.)\.\.(?!\.)", "...", t)
    return t


_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힣]")
_END_PUNCT = {"！": "!", "？": "?", "。": ".", "…": "...", "~": "~", "～": "~"}


def restore_punctuation(source: str, uzbek: str) -> str:
    """Tarjimada yo'qolgan oxirgi tinish belgisini qaytaradi.

    Sinovda: "你在做什么？" -> "Nima qilyapsiz" (so'roq belgisi yo'qolgan).
    Pufakchada "?" yoki "!" ohangni beradi - yo'qolmasligi kerak.
    """
    if not uzbek or re.search(r"[.!?…~]$", uzbek):
        return uzbek
    m = re.search(r"([!?.…~！？。～]+)\s*$", source or "")
    if not m:
        return uzbek
    tail = "".join(_END_PUNCT.get(c, c) for c in m.group(1))
    return uzbek + tail


def capitalize_first(source: str, uzbek: str) -> str:
    """Gap bosh harf bilan boshlansin (sinovda: "xayrli tong!", "uch yildan keyin").

    Manba kichik harf bilan boshlangan lotin matn bo'lsa (davom etayotgan gap,
    masalan "...and then") - tegilmaydi.
    """
    if not uzbek:
        return uzbek
    src_letter = next((c for c in (source or "") if c.isalpha()), "")
    if src_letter and not _CJK.match(src_letter) and src_letter.islower():
        return uzbek
    for i, c in enumerate(uzbek):
        if c.isalpha():
            uzbek = uzbek[:i] + c.upper() + uzbek[i + 1:]
            break
    # Gap ichidagi keyingi gaplar ham: "Xayrli tong! yaxshimi?" -> "... Yaxshimi?"
    # ("..." dan keyin tegilmaydi - u yerda gap odatda davom etadi)
    return re.sub(r"(?<!\.)([!?.])(?!\.)(\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2) + m.group(3).upper(), uzbek)


# Tovush effektlari: Google ularni ot sifatida tarjima qiladi ("BOOM" -> "portlash",
# "쾅" -> "portlash"). O'zbek komikslarida tovush taqlidi ishlatiladi. Inglizcha
# oraliq tarjima bo'yicha tanlanadi - shuning uchun koreys, yapon, ingliz
# effektlari uchun bir xil ishlaydi.
_SFX = {
    "bang": "Gurs!", "boom": "Gumm!", "kaboom": "Gumm!", "blam": "Gurs!", "bam": "Gup!",
    "thud": "Gup!", "thump": "Dup!", "ba-dump": "Duk-duk", "badump": "Duk-duk",
    "thump thump": "Duk-duk", "heartbeat": "Duk-duk", "pound": "Duk-duk",
    "whoosh": "Shuv!", "swoosh": "Shuv!", "swish": "Shuv!", "woosh": "Shuv!",
    "crash": "Qars!", "smash": "Qars!", "crack": "Qirs!", "snap": "Qirs!",
    "knock": "Taq-taq", "knock knock": "Taq-taq", "tap": "Tiq", "click": "Chiq",
    "slap": "Shap!", "smack": "Shap!", "punch": "Gup!", "whack": "Gup!",
    "gasp": "Hah!", "huff": "Puf", "sigh": "Uhh...", "sniff": "Hid-hid",
    "splash": "Shalop!", "drip": "Tomp", "ring": "Jiring!", "ding": "Jiring!",
    "buzz": "Viz-z", "rumble": "Gurr...", "creak": "G'ijir", "rustle": "Shit-shit",
    "bang bang": "Gurs-gurs!", "pow": "Gup!", "zoom": "Viz!", "zap": "Chirs!",
    "explosion": "Gumm!", "boom boom": "Gumm-gumm!", "stomp": "Tup!", "step": "Tup",
    "footsteps": "Tup-tup", "tremble": "Dir-dir", "shiver": "Dir-dir", "nod": "Bosh irg'adi",
}


# Undov so'zlari: Google "HUH?" ni "HU?" qilib qo'yardi (haqiqiy bob).
# Kalit - takroriy harflarsiz ("hmmm" -> "hm", "wheeew" -> "whew").
_INTERJ = {
    "huh": "A", "hm": "Hm", "mm": "Mm", "hmph": "Hmf", "whew": "Uf", "phew": "Uf",
    "ugh": "Ux", "tsk": "Tss", "eh": "E", "oh": "O", "ah": "A", "uh": "E", "um": "Mm",
    "wow": "Voy", "whoa": "Voy", "ouch": "Voy", "oops": "Voy", "hey": "Hoy", "ha": "Ha",
    "haha": "Haha", "hahaha": "Hahaha", "heh": "He", "hehe": "Hehe", "argh": "Aaa",
    "aargh": "Aaa", "gah": "Aah", "huhu": "Huhu",
}


def _interjection(source: str) -> str | None:
    t = re.sub(r"\b([^\W\d_]{1,2})-(?=\1)", "", source or "", flags=re.I)
    m = re.fullmatch(r"\W*([A-Za-z]+)(\W*)", t)
    if not m:
        return None
    word = re.sub(r"(.)\1+", r"\1", m.group(1).lower())
    word2 = re.sub(r"(.)\1{2,}", r"\1\1", m.group(1).lower())
    base = _INTERJ.get(m.group(1).lower()) or _INTERJ.get(word2) or _INTERJ.get(word)
    if not base:
        return None
    return base + m.group(2).strip()


def _sfx_uzbek(english: str | None) -> str | None:
    if not english:
        return None
    key = re.sub(r"[^a-z\- ]", "", english.lower()).strip().replace("-", " ")
    key = re.sub(r"\s+", " ", key)
    if key in _SFX:
        return _SFX[key]
    words = key.split()
    # "boom boom boom" kabi takror
    if words and len(set(words)) == 1 and words[0] in _SFX:
        base = _SFX[words[0]].rstrip("!.")
        return "-".join([base] * min(len(words), 3)) + "!"
    return None


def clean_uzbek(text: str) -> str:
    """Tarjimadan KEYIN: shrift va o'qilishi uchun tozalash.

    Montserrat shriftida U+02BB (ʻ) va U+02BC (ʼ) belgilari bo'lmasligi mumkin -
    rasmda "kvadratcha" bo'lib chiqadi. Hammasi oddiy apostrofga keltiriladi,
    chunki botning barcha tarjimalari shu ko'rinishda.
    """
    t = (text or "").strip()
    t = t.replace("ʻ", "'").replace("ʼ", "'").replace("‘", "'").replace("’", "'")
    t = t.replace("`", "'")
    t = re.sub(r"\s+", " ", t)
    # Google ba'zan tinish belgisidan oldin bo'sh joy qo'yadi: "kerak ..." -> "kerak..."
    t = re.sub(r"\s+([.,!?…:;])", r"\1", t)
    return t


# Duduqlanish: "C-CLEAR THE WAY!" -> Google "C - yo'lni bo'shating!" qilardi.
# Tarjimadan oldin olib tashlanadi, keyin o'zbekcha so'zning bosh harfi bilan
# qaytariladi: "Y-yo'lni bo'shating!"
_STUTTER = re.compile(r"\b([^\W\d_]{1,2})-(?=\1)", re.IGNORECASE | re.UNICODE)


def split_stutter(text: str) -> tuple[str, bool]:
    t = (text or "").strip()
    first = _STUTTER.match(t) is not None
    return _STUTTER.sub("", t), first


def add_stutter(uzbek: str) -> str:
    for i, c in enumerate(uzbek):
        if c.isalpha():
            return uzbek[:i] + c.upper() + "-" + c.lower() + uzbek[i + 1:]
    return uzbek


def _same(a: str, b: str) -> bool:
    """Tarjima asl matnning o'zimi (Google tarjima qilmay qaytargan)."""
    k = lambda x: re.sub(r"[^\w]", "", (x or "").lower())
    return bool(k(a)) and k(a) == k(b)


# ---------------------------------------------------------------- Google

def _google_many(texts: list[str], source: str, target: str) -> list[tuple[str, str]] | None:
    """Bir nechta matnni bitta so'rovda tarjima qiladi.

    Returns: [(tarjima, aniqlangan_til), ...] - kirish bilan AYNAN bir xil tartibda.
    Uzun ro'yxat bo'laklarga bo'linadi (URL uzunligi chegarasi).
    """
    if not texts:
        return []
    results: list[tuple[str, str]] = []
    chunk: list[str] = []

    def flush(part: list[str]) -> bool:
        params = [("client", "dict-chrome-ex"), ("sl", source), ("tl", target)]
        params += [("q", s) for s in part]
        try:
            data = json.loads(_fetch(_GOOGLE + "?" + urllib.parse.urlencode(params)))
        except Exception as exc:
            logger.info("Google tarjima ishlamadi (%s)", exc)
            return False
        parsed = _parse_google(data, len(part))
        if parsed is None:
            logger.info("Google javobi kutilmagan shaklda: %r", str(data)[:200])
            return False
        results.extend(parsed)
        return True

    size = 0
    for s in texts:
        enc = len(urllib.parse.quote(s)) + 3
        if chunk and size + enc > _MAX_URL:
            if not flush(chunk):
                return None
            chunk, size = [], 0
        chunk.append(s)
        size += enc
    if chunk and not flush(chunk):
        return None
    return results if len(results) == len(texts) else None


def _parse_google(data, n: int) -> list[tuple[str, str]] | None:
    """Google javobining turli shakllarini bir xil ko'rinishga keltiradi."""
    out: list[tuple[str, str]] = []
    if n == 1 and isinstance(data, list) and data and isinstance(data[0], str):
        return [(data[0], "")]
    if not isinstance(data, list):
        if isinstance(data, dict) and n == 1:
            text = "".join(s.get("trans", "") for s in data.get("sentences", []))
            return [(text, data.get("src", ""))]
        return None
    for item in data:
        if isinstance(item, list) and item:
            out.append((str(item[0]), str(item[1]) if len(item) > 1 else ""))
        elif isinstance(item, str):
            out.append((item, ""))
        else:
            return None
    return out if len(out) == n else None


def _google_pivot(texts: list[str]) -> tuple[list[str] | None, list[str] | None]:
    """Ingliz tili orqali tarjima (koreys/yapon/xitoy -> ingliz -> o'zbek).

    NEGA IKKI BOSQICH: to'g'ridan-to'g'ri koreyschadan o'zbekchaga o'girilganda
    sifat pasayadi (sinovda: "안에서 기다리고 있어" -> "waiting inside" — inglizcha
    qolib ketgan; "이 마을은 많이 변했구나" -> "Bu shaharda juda ko'p narsa bor" —
    ma'no buzilgan). Ingliz orqali ikkalasi ham to'g'ri chiqadi.

    Manba allaqachon inglizcha bo'lsa, birinchi bosqich natijasi emas, asl matn
    olinadi (Google "ingliz->ingliz" da ba'zan so'zni o'zgartirib yuboradi).

    Returns: (o'zbekcha ro'yxat | None, inglizcha ro'yxat | None)
    """
    first = _google_many(texts, "auto", "en")
    if first is None:
        return None, None
    english = [src if lang == "en" else (tr or src) for src, (tr, lang) in zip(texts, first)]
    second = _google_many(english, "en", "uz")
    if second is None:
        return None, english
    return [tr for tr, _ in second], english


# ---------------------------------------------------------------- zaxiralar

def _mymemory(text: str, source: str = "en") -> str | None:
    """Zaxira: MyMemory (kalitsiz, kuniga ~5000 so'z)."""
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
        {"q": text, "langpair": f"{source}|uz"}
    )
    try:
        data = json.loads(_fetch(url))
        out = html.unescape(data["responseData"]["translatedText"]).strip()
        if not out or "NO QUERY SPECIFIED" in out.upper() or "INVALID" in out.upper() \
                or "MYMEMORY WARNING" in out.upper():
            return None
        return out
    except Exception as exc:
        logger.info("MyMemory ishlamadi (%s)", exc)
        return None


_LOCAL_PROMPT = (
    "Translate this manga speech line into natural spoken Uzbek (latin alphabet, "
    "use o' and g'). Output ONLY the Uzbek sentence, nothing else. Keep it short.\n\nLine: "
)


def _local(text: str) -> str | None:
    """Mahalliy model bilan tarjima (internetsiz zaxira)."""
    try:
        import ollama

        from config import OLLAMA_HOST
        from translator import MODEL

        client = ollama.Client(host=OLLAMA_HOST, timeout=120)
        r = client.chat(
            model=MODEL,
            messages=[{"role": "user", "content": _LOCAL_PROMPT + text}],
            options={"num_predict": 80, "temperature": 0.2},
            keep_alive="30m",
        )
        return _clean_model(r.message.content or "") or None
    except Exception as exc:
        logger.warning("Mahalliy tarjima ham ishlamadi: %s", exc)
        return None


def _clean_model(text: str) -> str:
    """Model javobidagi ortiqchani olib tashlaydi va buzuq takrorni kesadi."""
    t = (text or "").strip()
    t = re.sub(r"^```\w*|```$", "", t).strip()
    t = t.split("\n")[0].strip()
    t = t.strip('"“”«» ')
    t = re.sub(r"^(uzbek|o'zbek(cha)?|tarjima)\s*[:\-]\s*", "", t, flags=re.I)
    words = t.split()
    if len(words) > 6:
        out, run, prev = [], 0, None
        for w in words:
            run = run + 1 if w == prev else 0
            if run >= 2:
                break
            out.append(w)
            prev = w
        t = " ".join(out)
    return t[:300].strip()


# ---------------------------------------------------------------- asosiy API

def translate_many(texts: list[str]) -> list[str]:
    """Bir nechta matnni o'zbekchaga tarjima qiladi (tartib saqlanadi).

    Tarjima qilib bo'lmagan matn aslicha qaytadi - bot hech qachon bo'sh
    pufakcha chizmaydi.
    """
    stut = [split_stutter(t) for t in texts]
    norm = [normalize_source(t) for t, _ in stut]
    result: list[str | None] = [None] * len(norm)

    todo: list[int] = []
    for i, t in enumerate(norm):
        if not t:
            result[i] = ""
        elif not _HAS_LETTER.search(t):
            result[i] = t                       # "...", "?!" - o'zgarmaydi
        elif _interjection(t):
            result[i] = _interjection(t)       # "HUH?" -> "A?" (lug'atdan)
        elif t in _cache:
            result[i] = _cache[t]
        else:
            todo.append(i)

    english: list[str | None] = [None] * len(norm)
    if todo and ENGINE in ("auto", "google"):
        uz, en = _google_pivot([norm[i] for i in todo])
        if en:
            for k, i in enumerate(todo):
                english[i] = en[k]
        if uz:
            for k, i in enumerate(todo):
                if uz[k] and uz[k].strip():
                    result[i] = uz[k]
        # Tarjima qilinmay qaytgan inglizcha ("Messenger!" - Google uni ilova nomi
        # deb o'ylaydi, "Greenhorn") - kichik harf bilan qayta so'raymiz: "xabarchi!"
        again = [i for i in todo if result[i] and english[i] and _same(result[i], english[i])]
        if again:
            retry = _google_many([english[i].lower() for i in again], "en", "uz")
            for i, got in zip(again, retry or []):
                if got and got[0].strip() and not _same(got[0], english[i]):
                    result[i] = got[0]
        # Tovush effektlari - lug'atdan (ot sifatidagi tarjima o'rniga)
        for i in todo:
            sfx = _sfx_uzbek(english[i])
            if sfx:
                result[i] = sfx

    for i in todo:
        if result[i] is not None:
            continue
        out = None
        if ENGINE in ("auto", "google"):
            src_en = english[i]
            if src_en:
                out = _mymemory(src_en, "en")
            elif norm[i].isascii():
                out = _mymemory(norm[i], "en")
        if out is None and ENGINE in ("auto", "local"):
            out = _local(english[i] or norm[i])
        result[i] = out

    final = []
    for i, t in enumerate(norm):
        r = clean_uzbek(result[i] or "") or t
        if i in todo and result[i]:
            r = capitalize_first(t, restore_punctuation(t, r))
            _cache[t] = r
        if stut[i][1] and r:
            r = add_stutter(r)
        final.append(r)
    return final


def translate_text(text: str) -> str:
    """Bitta matnni o'zbekchaga tarjima qiladi."""
    return translate_many([text])[0]


def translate_items(items: list[dict]) -> list[dict]:
    """OCR natijasidagi har bir element uchun 'uzbek' maydonini to'ldiradi."""
    live = []
    for item in items:
        original = (item.get("original") or item.get("text") or "").strip()
        if original:
            item["original"] = original
            live.append(item)
    if live:
        for item, uz in zip(live, translate_many([it["original"] for it in live])):
            item["uzbek"] = uz
    return items
