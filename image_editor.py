"""Tarjimani rasmga qayta chizish.

Ikki nozik joyi bor (sinovda ko'rilgan nuqsonlar shu yerda tuzatilgan):

1) TO'LDIRISH SHAKLI. Avval bbox butunligicha to'rtburchak qilib bo'yalardi —
   dumaloq pufakchada oq to'rtburchak burchaklari tashqariga chiqib turardi.
   Endi har bir qator bo'yicha pufakcha rangiga YAQIN piksellar orasi
   to'ldiriladi, ya'ni pufakchaning haqiqiy shakli saqlanadi.

2) SATRGA BO'LISH. Avval `textwrap` belgilar sonini taxmin qilib bo'lardi va
   so'z o'rtasidan kesardi ("Qaytganing / izga"). Endi har bir satr eni
   HAQIQIY o'lchanadi va so'z hech qachon bo'linmaydi.
"""

import logging
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from image_utils import encode_jpeg, load_image

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Komiks shrifti (Shantell Sans Bold Italic, OFL): skanlatsiyalardagi qo'lda
# yozilgandek qiya harflarga o'xshaydi. Foydalanuvchi fikri (2026-09-30):
# "shriftni to'g'rilash kerak" - Montserrat oddiy sayt shrifti edi.
# Boshqasini FONT_FILE muhit o'zgaruvchisi bilan berish mumkin.
# 2026-09-30 (foydalanuvchi namuna rasm yubordi - klassik skanlatsiya shrifti, to'g'ri,
# qalin, KATTA harf): Digital Strip (Blambot) + FONT_BOLD qalinlik namunaga eng yaqin.
# Litsenziyasi notijorat komiksga bepul, lekin QAYTA TARQATISH MUMKIN EMAS - ochiq
# repoga qo'yilmaydi: GitHub workflow uni blambot/dafont'dan o'zi yuklaydi (.gitignore).
FONT_CANDIDATES = [
    os.getenv("FONT_FILE", ""),
    BASE_DIR / "assets" / "fonts" / "digistrip.ttf",
    BASE_DIR / "assets" / "fonts" / "ShantellSans-BoldItalic.ttf",
    BASE_DIR / "assets" / "fonts" / "Montserrat-Bold.ttf",
    r"C:\Windows\Fonts\DejaVuSans-Bold.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_COLOR_TOLERANCE = 70   # pufakcha rangidan shuncha farq qilsa ham "pufakcha ichi" deb olinadi
_MIN_SPAN = 4           # qatordagi eng kichik to'ldiriladigan kenglik


@lru_cache(maxsize=64)
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if not path:
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


# Qalinlik (sun'iy bold): harf o'z rangida shuncha chiziq bilan qalinlashadi.
# Digital Strip o'zi ingichka - namunadagidek bo'lishi uchun ~3.5% o'lchamdan.
FONT_BOLD = float(os.getenv("FONT_BOLD", "0.028"))
FONT_UPPER = os.getenv("FONT_UPPER", "1") == "1"

# RENDER_V2 (2026-09-30, faqat @gardenhwa_bot - foydalanuvchi: "cleaning yaxshi emas"):
# pufakcha BUTUNLAY bitta rang bilan bo'yalmaydi - faqat harflar topilib, atrofidan
# to'ldiriladi (inpaint). Sabab: gradientli / ichki soyali pufakcha (haqiqiy bobda
# oq->binafsha) tekis kulrang "yamoq" bo'lib qolardi, soyasi yo'qolardi. Qator oralig'i ham.
RENDER_V2 = os.getenv("RENDER_V2", "") == "1"


def _text_ink(sub: np.ndarray, bg: tuple[int, int, int], thr: int = 35) -> np.ndarray:
    """Harf piksellari - gradient fonda ham. Fon morfologik "yopish" bilan topiladi
    (harf chizig'idan keng yadro harfni yo'qotadi, gradient esa qoladi); fon yorug'
    bo'lsa to'q harf, qorong'i bo'lsa och harf qidiriladi."""
    import cv2

    gray = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY).astype(np.int16)
    kernel = np.ones((21, 21), np.uint8)
    if sum(bg) / 3 > 110:
        back = cv2.morphologyEx(gray.astype(np.uint8), cv2.MORPH_CLOSE, kernel).astype(np.int16)
        return (back - gray) > thr
    back = cv2.morphologyEx(gray.astype(np.uint8), cv2.MORPH_OPEN, kernel).astype(np.int16)
    return (gray - back) > thr


def _flood_gradient(sub: np.ndarray, tbox: tuple[int, int, int, int],
                    bg: tuple[int, int, int]) -> np.ndarray | None:
    """Pufakcha ichini topadi - gradientli pufakchada ham.

    Eski usul bitta rangga yaqin piksellarni olardi: oq->binafsha gradientli pufakchaning
    faqat YARMI topilib, yarmi tekis bo'yalar, yarmida asl yozuv qolardi (haqiqiy bob,
    "HE DIED PROTECTING..."). Endi: harflar morfologik yopish bilan olib tashlangan
    fonda floodFill QO'SHNI piksellar farqi bo'yicha tarqaladi - silliq gradient bo'ylab
    yuradi, keskin kontur chizig'ida to'xtaydi."""
    import cv2

    gray = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY)
    kernel = np.ones((21, 21), np.uint8)
    op = cv2.MORPH_CLOSE if sum(bg) / 3 > 110 else cv2.MORPH_OPEN
    closed = cv2.morphologyEx(gray, op, kernel)
    tx1, ty1, tx2, ty2 = tbox
    h, w = gray.shape
    tx1, ty1 = max(0, tx1), max(0, ty1)
    tx2, ty2 = min(w, tx2), min(h, ty2)
    if tx2 - tx1 < 2 or ty2 - ty1 < 2:
        return None
    # Harflar FAQAT matn qutisi atrofida olib tashlanadi: yopish amali pufakchaning
    # ingichka konturini ham o'chirardi va tarqalish pufakchadan chiqib ketardi
    # (haqiqiy bobda kontur yo'qoldi). Tashqarida asl rasm - kontur to'siq bo'lib qoladi.
    back = gray.copy()
    zy1, zy2, zx1, zx2 = max(0, ty1 - 8), min(h, ty2 + 8), max(0, tx1 - 10), min(w, tx2 + 10)
    back[zy1:zy2, zx1:zx2] = closed[zy1:zy2, zx1:zx2]
    back = cv2.GaussianBlur(back, (5, 5), 0)          # tekstura/JPEG shovqini
    # urug': matn qutisida fonning eng odatiy (median) qiymatiga yaqin nuqta
    patch = back[ty1:ty2, tx1:tx2].astype(np.int16)
    med = np.median(patch)
    iy, ix = np.unravel_index(np.argmin(np.abs(patch - med)), patch.shape)
    mask = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(back.copy(), mask, (int(tx1 + ix), int(ty1 + iy)), 0, 4, 4,
                  4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
    region = mask[1:-1, 1:-1] > 0
    if not region.any():
        return None
    # Himoya: pufakcha matndan ~15 barobardan katta bo'lmaydi - kattaroq bo'lsa kontur
    # uzuq va tarqalish rasmga chiqib ketgan. Unda eski (rang bo'yicha) usul.
    if region.sum() > 15 * (tx2 - tx1) * (ty2 - ty1):
        close = np.abs(sub.astype(np.int16) - np.array(bg, np.int16)).max(axis=2) < _COLOR_TOLERANCE
        return _surrounding_region(close, (tx1, ty1, tx2, ty2))
    return region


def _is_busy(sub: np.ndarray, region: np.ndarray, bg: tuple[int, int, int]) -> bool:
    """Topilgan "pufakcha" aslida rasmmi: harflarsiz piksellari juda rang-barang.
    Titul sahifasida logotip ustidagi yozuv pufakcha deb olinib, ko'k to'rtburchak
    bilan bo'yalgan edi. Oddiy/gradientli pufakcha: 5-95% yorug'lik oralig'i < ~45."""
    import cv2

    ink = _text_ink(sub, bg, thr=18) | _text_ink(sub, (0, 0, 0) if sum(bg) / 3 > 110 else (255, 255, 255), thr=25)
    ink = cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    pix = sub[region & ~ink]
    if len(pix) < 200:
        return False
    lum = pix.astype(np.float32).mean(axis=1)
    return float(np.percentile(lum, 95) - np.percentile(lum, 5)) > 60


def _is_gradient(sub: np.ndarray, fill: np.ndarray, bg: tuple[int, int, int]) -> bool:
    """Pufakcha foni bir tekis emasmi (gradient, ichki soya). Harfsiz fon rangi
    tarqoqligi bo'yicha: oq pufakchada ~0-10, oq->binafsha gradientda 30+."""
    import cv2

    ink = _text_ink(sub, bg, thr=18)
    ink = cv2.dilate(ink.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    known = (fill & ~ink).astype(np.float32)
    if known.sum() < 200:
        return False
    # Faqat KENG ko'lamli o'zgarish: skanlatsiyadan qolgan ingichka xira chiziq yoki
    # JPEG shovqini oq pufakchani "gradient" qilib qo'ymasin (haqiqiy bobda shunday bo'ldi,
    # tekis bo'yash o'rniga iz qolgan edi) - harfsiz fon keng blur bilan o'rtachalanadi.
    lum = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_RGB2GRAY).astype(np.float32)
    d = cv2.GaussianBlur(known, (41, 41), 0)
    smooth = cv2.GaussianBlur(lum * known, (41, 41), 0) / np.maximum(d, 1e-3)
    vals = smooth[(fill & ~ink) & (d > 0.5)]
    if len(vals) < 200:
        return False
    return float(np.percentile(vals, 97) - np.percentile(vals, 3)) > 18


def _erase_text_smooth(sub: np.ndarray, fill: np.ndarray, tbox: tuple[int, int, int, int],
                       bg: tuple[int, int, int]) -> None:
    """Pufakcha ichidagi (fill) va matn qutisi atrofidagi harflarni inpaint bilan o'chiradi."""
    import cv2

    h, w = fill.shape
    tx1, ty1, tx2, ty2 = tbox
    px, py = max(10, int((tx2 - tx1) * 0.06)), 10     # qiya harf / tor OCR qutisi uchun zaxira
    zone = np.zeros_like(fill)
    zone[max(0, ty1 - py):min(h, ty2 + py), max(0, tx1 - px):min(w, tx2 + px)] = True
    zone &= fill
    # past chegara (18): harf atrofidagi och JPEG "halo" ham olinsin - aks holda dog' qolardi.
    # Qarama-qarshi yo'nalish ham: harfning oq "nur" konturi (binafsha pufakchada oq iz qolardi)
    light_bg = sum(bg) / 3 > 110
    ink = _text_ink(sub, bg, thr=18) | _text_ink(sub, (0, 0, 0) if light_bg else (255, 255, 255), thr=25)
    mask = (ink & zone).astype(np.uint8)
    if not mask.any():
        return
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=3).astype(bool) & fill
    # To'ldirish: harfsiz pufakcha piksellaridan NORMALLASHTIRILGAN blur - gradient silliq
    # davom etadi. (cv2.inpaint TELEA harf chetidagi kulrangni tortib, dog' qoldirardi.)
    known = (fill & ~mask).astype(np.float32)
    img = sub.astype(np.float32)
    est = np.zeros_like(img)
    den = np.zeros(known.shape, np.float32)
    for k in (31, 81):                   # keng harf bloki uchun kattaroq yadro zaxira
        d = cv2.GaussianBlur(known, (k, k), 0)
        n = cv2.GaussianBlur(img * known[..., None], (k, k), 0)
        ok = (den < 1e-3) & (d > 1e-3)
        est[ok] = n[ok] / d[ok][:, None]
        den[ok] = d[ok]
    done = mask & (den > 1e-3)
    sub[done] = np.clip(est[done], 0, 255).astype(np.uint8)
    rest = mask & ~done
    if rest.any():
        sub[rest] = bg


def _bold(font) -> int:
    if "digistrip" not in str(getattr(font, "path", "")).lower():
        return 0
    return max(1, round(font.size * FONT_BOLD)) if FONT_BOLD > 0 else 0


def _dominant_color(region: Image.Image) -> tuple[int, int, int]:
    small = region.resize((16, 16)).convert("RGB")
    colors = Counter(small.getdata())
    return colors.most_common(1)[0][0]


def _bg_around(arr: np.ndarray, box: tuple[int, int, int, int],
               line_h: float | None) -> tuple[int, int, int] | None:
    """Fon rangi matn qutisi ATROFIDAGI halqadan (qutining o'zidan emas).

    Katta yozuvda qutini harflar to'ldiradi: "DIANA DE VERECCIA." (jigarrang harf,
    oq ramka) da quti ichidagi eng ko'p rang harf rangi chiqib, ramka ichi
    jigarrangga bo'yalgan edi. Halqa esa asosan fondan iborat.
    """
    H, W = arr.shape[:2]
    x1, y1, x2, y2 = box
    p = int(max(6, min(24, (line_h or (y2 - y1)) * 0.25)))
    X1, Y1, X2, Y2 = max(0, x1 - p), max(0, y1 - p), min(W, x2 + p), min(H, y2 + p)
    sub = arr[Y1:Y2, X1:X2]
    mask = np.ones(sub.shape[:2], bool)
    mask[y1 - Y1:y2 - Y1, x1 - X1:x2 - X1] = False
    ring = sub[mask]
    if ring.shape[0] < 20:
        return None
    q = (ring // 16).astype(np.int32)
    keys = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    best = vals[counts.argmax()]
    if counts.max() < 0.35 * ring.shape[0]:
        return None                    # halqa bir xil rangda emas (rasm) - eski usul
    pick = ring[keys == best]
    return tuple(int(v) for v in np.median(pick, axis=0))


def _ink_color(arr: np.ndarray, box: tuple[int, int, int, int],
               bg: tuple[int, int, int]) -> tuple[int, int, int] | None:
    """Asl yozuv RANGLI bo'lsa (jigarrang, qizil, ko'k) - o'sha rang; qora/oq bo'lsa None.

    Foydalanuvchi: tozalash "yaxshi emas" - rangli ramka yozuvi tarjimada qora chiqardi.
    """
    x1, y1, x2, y2 = box
    sub = arr[y1:y2, x1:x2].reshape(-1, 3).astype(np.int16)
    if sub.shape[0] == 0:
        return None
    far = np.abs(sub - np.array(bg, np.int16)).max(axis=1) > 90
    if far.sum() < 30:
        return None
    ink = np.median(sub[far], axis=0)
    if int(ink.max() - ink.min()) < 50:
        return None                     # kulrang/qora/oq - odatiy rang qoladi
    return tuple(int(v) for v in ink)


def _text_color_for(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    brightness = (bg[0] * 299 + bg[1] * 587 + bg[2] * 114) / 1000
    return (20, 20, 20) if brightness > 140 else (245, 245, 245)


def _touches(region: np.ndarray, clipped: tuple[bool, bool, bool, bool]) -> int:
    """Hudud oynaning nechta tomoniga tegib turibdi?

    Rasm chetiga to'g'ri kelgan tomonlar hisoblanmaydi (u yerda tegish tabiiy).
    clipped = (chap, yuqori, o'ng, past) - shu tomon rasm chegarasimi.
    """
    sides = [region[:, 0].any(), region[0, :].any(), region[:, -1].any(), region[-1, :].any()]
    return sum(1 for touch, edge in zip(sides, clipped) if touch and not edge)


def _flood(close: np.ndarray, seed_box: tuple[int, int, int, int] | None = None) -> np.ndarray | None:
    """Matn qutisi ichidan boshlab bog'langan hududni topadi (skanchiziqli flood fill).

    `close` - "pufakcha rangiga yaqin" piksellar maskasi. Boshlang'ich nuqta
    harf ustiga tushib qolmasligi uchun matn qutisi ichida bir nechta nuqta
    sinaladi (harflar orasidagi fon).
    """
    h, w = close.shape
    if h == 0 or w == 0:
        return None

    sx1, sy1, sx2, sy2 = seed_box if seed_box else (0, 0, w, h)
    sx1, sy1 = max(0, sx1), max(0, sy1)
    sx2, sy2 = min(w, sx2), min(h, sy2)
    seeds = []
    for fy in (0.5, 0.15, 0.85, 0.3, 0.7, 0.02, 0.98):
        for fx in (0.5, 0.2, 0.8, 0.05, 0.95):
            y, x = sy1 + int((sy2 - sy1 - 1) * fy), sx1 + int((sx2 - sx1 - 1) * fx)
            if 0 <= y < h and 0 <= x < w and close[y, x]:
                seeds.append((y, x))
    if not seeds:
        return None
    sy, sx = seeds[0]

    # Tez yo'l: OpenCV floodFill (C++). Python siklidan ~50-100 marta tez.
    try:
        import cv2

        img = close.astype(np.uint8) * 255
        mask = np.zeros((h + 2, w + 2), np.uint8)
        cv2.floodFill(img, mask, (int(sx), int(sy)), 128, 0, 0,
                      4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        out = mask[1:-1, 1:-1] > 0
        return out if out.any() else None
    except ImportError:
        pass

    # Zaxira: skanchiziqli flood fill. Qo'shni qatorga har bir PIKSEL emas,
    # har bir uzluksiz BO'LAK uchun bitta urug' qo'yiladi.
    out = np.zeros_like(close)
    stack = [(sy, sx)]
    while stack:
        y, x = stack.pop()
        if out[y, x] or not close[y, x]:
            continue
        left = x
        while left > 0 and close[y, left - 1] and not out[y, left - 1]:
            left -= 1
        right = x
        while right + 1 < w and close[y, right + 1] and not out[y, right + 1]:
            right += 1
        out[y, left : right + 1] = True
        for ny in (y - 1, y + 1):
            if 0 <= ny < h:
                idx = np.flatnonzero(close[ny, left : right + 1] & ~out[ny, left : right + 1])
                if idx.size:
                    starts = idx[np.concatenate(([True], np.diff(idx) > 1))]
                    stack.extend((ny, left + int(o)) for o in starts)

    return out if out.any() else None


def _surrounding_region(close: np.ndarray, text_box: tuple[int, int, int, int]) -> np.ndarray | None:
    """Matnni ATROFDAN o'rab turgan fon hududini topadi (pufakcha ichi).

    Avval matn qutisi ichidagi nuqtadan tarqalish boshlanardi. Haqiqiy bobda
    qalin komiks yozuvida bu nuqta ko'pincha harf ichidagi bo'shliqqa ("O",
    "A", "R" ichi) tushardi: o'sha kichik teshik "pufakcha" deb olinib, asl
    matn o'chmay qolar va tarjima uning ichiga mikroskopik bo'lib chizilardi
    (bobda ~20 joy). Endi matn qutisi ATROFIDAGI halqada eng ko'p joy olgan
    bog'langan hudud tanlanadi - harf teshiklari matnni o'ramaydi, pufakcha
    ichi esa har tomondan o'raydi.
    """
    try:
        import cv2
    except ImportError:
        return _flood(close, text_box)

    h, w = close.shape
    n, labels = cv2.connectedComponents(close.astype(np.uint8), connectivity=4)
    if n <= 1:
        return None
    tx1, ty1, tx2, ty2 = text_box
    pad = max(4, int(min(tx2 - tx1, ty2 - ty1) * 0.15))
    ox1, oy1 = max(0, tx1 - pad), max(0, ty1 - pad)
    ox2, oy2 = min(w, tx2 + pad), min(h, ty2 + pad)
    ring = labels[oy1:oy2, ox1:ox2].copy()
    ring[max(0, ty1 - oy1 + 2):max(0, ty2 - oy1 - 2), max(0, tx1 - ox1 + 2):max(0, tx2 - ox1 - 2)] = 0
    counts = np.bincount(ring.ravel(), minlength=n)
    counts[0] = 0
    best = int(counts.argmax())
    if counts[best] == 0:
        return _flood(close, text_box)
    return labels == best


def _fill_bubble(arr: np.ndarray, box: tuple[int, int, int, int],
                 bg: tuple[int, int, int]):
    """Pufakcha ichini fon rangi bilan to'ldiradi (shaklini saqlab).

    Returns: (ichki_quti, tegilgan_tomonlar) - ichki_quti matn yozish uchun
             xavfsiz to'rtburchak; tegilgan_tomonlar > 0 bo'lsa hudud qidiruv
             oynasidan chiqib ketgan (masalan webtoon'ning oq oralig'i).
             Yoki None - pufakcha topilmadi (matn rasm/fon ustida).
    """
    H, W = arr.shape[:2]
    bx1, by1, bx2, by2 = box
    bw, bh = bx2 - bx1, by2 - by1
    if bw <= 0 or bh <= 0:
        return box, 0, None

    # Qidiruv oynasi matn qutisidan KENGROQ: tezkor OCR faqat matnni o'raydi,
    # pufakcha esa undan ancha katta. Pufakcha to'liq topilsa, tarjima uchun
    # joy ko'payadi va shrift kattaroq chiqadi.
    mx, my = max(40, int(bw * 0.5)), max(40, int(bh * 1.2))
    x1, y1 = max(0, bx1 - mx), max(0, by1 - my)
    x2, y2 = min(W, bx2 + mx), min(H, by2 + my)
    sub = arr[y1:y2, x1:x2]

    bg_arr = np.array(bg, dtype=np.int16)
    close = np.abs(sub.astype(np.int16) - bg_arr).max(axis=2) < _COLOR_TOLERANCE

    # Pufakcha hududini MATN ICHIDAN tarqalib (flood fill) topamiz.
    # Nega: avval har qatordagi birinchi va oxirgi "oqimtir" piksel orasi
    # to'ldirilardi — shovqinli/och rangli fonda pufakchadan TASHQARIDAGI
    # piksellar ham oqimtir bo'lgani uchun to'ldirish butun to'rtburchakka
    # yoyilib ketardi. Tarqalish esa qorong'i kontur (pufakcha chizig'i)
    # bilan to'siladi, shuning uchun faqat ichkarida qoladi.
    if RENDER_V2:
        region = _flood_gradient(sub, (bx1 - x1, by1 - y1, bx2 - x1, by2 - y1), bg)
    else:
        region = _surrounding_region(close, (bx1 - x1, by1 - y1, bx2 - x1, by2 - y1))

    # Tarqalish oynaning 3+ tomoniga yetib borsa - pufakcha chegarasi yo'q
    # (matn rasm yoki bir xil fon ustida). Bu holatni chaqiruvchi inpaint
    # bilan hal qiladi - shuning uchun bu yerda hech narsa bo'yalmaydi.
    if region is None:
        return None
    # Hudud matn qutisidan ham kichik bo'lsa - bu pufakcha emas (harf teshigi yoki
    # rasm bo'lagi). Unda inpaint ishlaydi, asl yozuv qolib ketmaydi.
    if int(region.sum()) < 0.5 * bw * bh:
        return None
    touched = _touches(region, clipped=(x1 == 0, y1 == 0, x2 == W, y2 == H))
    if touched >= 3:
        return None
    if RENDER_V2 and _is_busy(sub, region, bg):
        return None     # bu rasm (logotip, fon tasviri) - pufakcha emas: faqat harf o'chiriladi

    # Faqat pufakcha ichi + uning ICHIDA qolgan harflar bo'yaladi. Avval har qator
    # chetdan-chetgacha bo'yalardi - haqiqiy bobda pufakcha konturi va dumi
    # kesilib, tutash pufakchalarda oq to'rtburchak paydo bo'lardi.
    fill = _with_holes(region, clipped=(x1 == 0, y1 == 0, x2 == W, y2 == H))
    if RENDER_V2 and _is_gradient(sub, fill, bg):
        _erase_text_smooth(sub, fill, (bx1 - x1, by1 - y1, bx2 - x1, by2 - y1), bg)
    else:
        sub[fill] = bg      # tekis pufakcha: bitta rang - eng toza natija (iz qolmaydi)
    shape = (x1, y1, fill)          # pufakcha shakli - matnni shu shaklga moslab yozish uchun

    # Matn uchun joy: faqat SHU matn atrofidagi (vertikal tasma) va uning ostidagi
    # uzluksiz bo'lak. Butun hudud olinsa, tutash ikki pufakchada ikkala tarjima
    # bir joyga tushardi.
    tx1, tx2 = bx1 - x1, bx2 - x1
    band = max(bh * 0.8, 30)
    r_lo, r_hi = max(0, int(by1 - y1 - band)), min(sub.shape[0], int(by2 - y1 + band))
    spans: list[tuple[int, int] | None] = [None] * sub.shape[0]
    for r in range(r_lo, r_hi):
        run = _run_under(fill[r], tx1, tx2)
        if run and run[1] - run[0] >= _MIN_SPAN:
            spans[r] = run

    widths = [s[1] - s[0] for s in spans if s]
    if not widths:
        return box, touched, shape
    limit = max(widths) * 0.78
    rows = [i for i, s in enumerate(spans) if s and (s[1] - s[0]) >= limit]
    if not rows:
        return box, touched, shape
    inner_x1 = max(spans[i][0] for i in rows)
    inner_x2 = min(spans[i][1] for i in rows)
    if inner_x2 - inner_x1 < _MIN_SPAN:
        return box, touched, shape
    # Matn pufakcha chetiga tegib qolmasligi uchun kichik zaxira
    inset = int((inner_x2 - inner_x1) * 0.05)
    inner = (x1 + inner_x1 + inset, y1 + rows[0], x1 + inner_x2 - inset, y1 + rows[-1] + 1)
    # Asl matn sig'gan joy tarjimaga ham albatta sig'adi. Tikanli/notekis
    # pufakchada yuqoridagi hisob asl matndan TORROQ chiqardi va tarjima tor
    # ustunga siqilardi ("Har bir / oxirgi / ritsarni / chaqiring!").
    # Faqat ENI bo'yicha: balandlik bo'yicha qo'shilganda notekis chetlarda matn
    # pufakcha chizig'iga tegib qolardi ("yashil shox!").
    inner = (min(inner[0], bx1), inner[1], max(inner[2], bx2), inner[3])
    return inner, touched, shape


def _run_under(row: np.ndarray, tx1: int, tx2: int) -> tuple[int, int] | None:
    """Qatordagi uzluksiz bo'laklardan matn qutisi ostidagisini (eng ko'p ustma-ust) oladi."""
    idx = np.flatnonzero(row)
    if idx.size == 0:
        return None
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]]))
    best, best_ov = None, 0
    for a, b in zip(starts, ends):
        ov = min(b, tx2) - max(a, tx1)
        if ov > best_ov:
            best, best_ov = (int(a), int(b)), ov
    return best


def _with_holes(region: np.ndarray,
                clipped: tuple[bool, bool, bool, bool] = (False, False, False, False)) -> np.ndarray:
    """Hudud + uning ichidagi "teshiklar" (harflar). Kontur esa tashqariga ulangan -
    teshik emas, shuning uchun tegilmaydi.

    clipped = (chap, yuqori, o'ng, past) - oynaning shu tomoni RASM CHETI. U yer
    "tashqari" hisoblanmaydi: haqiqiy bobda pufakcha PDF sahifalari chegarasida
    kesilgan edi va chetga tegib turgan oxirgi qator ("IN NASSAU?!") o'chmay qolgan.
    """
    try:
        import cv2

        h, w = region.shape
        inv = np.pad((~region).astype(np.uint8) * 255, 1, constant_values=255)
        left, top, right, bottom = clipped
        if left:
            inv[:, 0] = 0
        if top:
            inv[0, :] = 0
        if right:
            inv[:, -1] = 0
        if bottom:
            inv[-1, :] = 0
        seed = next(((x, y) for y, x in
                     [(0, i) for i in range(w + 2)] + [(h + 1, i) for i in range(w + 2)] +
                     [(i, 0) for i in range(h + 2)] + [(i, w + 1) for i in range(h + 2)]
                     if inv[y, x]), None)
        if seed is None:
            return region
        mask = np.zeros((h + 4, w + 4), np.uint8)
        # 8-bog'lanish: qiya (zinapoyali) kontur piksellari ham "tashqari"ga ulansin,
        # aks holda ular teshik deb o'chib ketadi (hudud 4-bog'lanish bilan topilgan)
        cv2.floodFill(inv, mask, seed, 128, 0, 0, 8 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        outside = mask[2:-2, 2:-2] > 0
        return region | (~region & ~outside)
    except ImportError:
        # OpenCV'siz: qator bo'yicha qamrov (eski usul)
        out = region.copy()
        for r in range(region.shape[0]):
            idx = np.flatnonzero(region[r])
            if idx.size:
                out[r, idx[0]: idx[-1] + 1] = True
        return out


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: float) -> list[str]:
    """So'zni BO'LMASDAN satrlarga ajratadi (enini haqiqiy o'lchab).

    Faqat defisdan keyin bo'lish mumkin ("To'g'ridan-" / "to'g'ri?"): haqiqiy
    bobda bitta uzun defisli so'z sig'magani uchun butun tarjima mayda shriftda
    chiqqan edi.
    """
    tokens: list[tuple[str, bool]] = []          # (bo'lak, oldingisiga yopishadimi)
    for word in text.split():
        for i, part in enumerate(re.split(r"(?<=-)(?=\w)", word)):
            tokens.append((part, i > 0))

    lines: list[str] = []
    current = ""
    for piece, glued in tokens:
        trial = current + piece if (glued and current) else (current + " " + piece).strip()
        if not current or draw.textlength(trial, font=font) <= max_w:
            current = trial
        else:
            lines.append(current)
            current = piece
    if current:
        lines.append(current)
    return lines or [text]


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    box = font.getbbox("Agqy")
    h = (box[3] - box[1]) + 4
    if RENDER_V2:
        # KATTA harf + qalinlik + "O‘" ustidagi belgi: qatorlar bir-biriga tegib qolardi
        h += 2 * _bold(font) + int(font.size * 0.12)
    return h


def _fit_text(draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int,
              max_size: int | None = None) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Qutiga sig'adigan eng katta shriftni tanlaydi.

    max_size - asl harf o'lchamidan kelib chiqqan chegara. Busiz katta
    pufakchadagi qisqa "Oh." yoki oq fondagi matn ulkan harflar bilan
    chizilardi (sinovda: asl ~34 px matn o'rniga ~80 px chiqqan).
    """
    size = max(9, min(box_h // 2, int(box_w / 3)))
    if max_size:
        size = max(9, min(size, max_size))
    while size >= 9:
        font = _load_font(size)
        lines = _wrap(draw, text, font, box_w)
        widest = max(draw.textlength(ln, font=font) for ln in lines)
        if widest <= box_w and _line_height(font) * len(lines) <= box_h:
            return font, lines
        size -= 1
    font = _load_font(9)
    return font, _wrap(draw, text, font, box_w)


def _draw_block(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
                color: tuple[int, int, int], max_size: int | None = None,
                stroke: tuple[int, int, int] | None = None, pad: int = 4) -> None:
    """Matnni quti o'rtasiga (satrlarga bo'lib) yozadi."""
    ix1, iy1, ix2, iy2 = box
    box_w = max(1, (ix2 - ix1) - pad * 2)
    box_h = max(1, (iy2 - iy1) - pad * 2)
    font, lines = _fit_text(draw, text, box_w, box_h, max_size)
    sw = max(2, font.size // 12) if stroke else 0
    lh = _line_height(font)
    cur_y = iy1 + ((iy2 - iy1) - lh * len(lines)) // 2
    for line in lines:
        line_w = draw.textlength(line, font=font)
        x = ix1 + ((ix2 - ix1) - line_w) // 2
        if stroke:
            # kontur tagida, qalinlik (o'z rangida) ustida
            draw.text((x, cur_y), line, font=font, fill=color, stroke_width=sw + _bold(font),
                      stroke_fill=stroke)
        draw.text((x, cur_y), line, font=font, fill=color,
                  stroke_width=_bold(font), stroke_fill=color)
        cur_y += lh


def mostly_upper(text: str) -> bool:
    """Asl yozuv KATTA harflardami (ingliz skanlatsiyalarida odatiy)."""
    letters = [c for c in text or "" if c.isalpha() and c.isascii()]
    return len(letters) >= 2 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def display_text(text: str, upper: bool) -> str:
    """Rasmga yoziladigan ko'rinish: to'g'ri o'zbek belgilari va asl yozuv uslubi.

    o'/g' -> o‘/g‘ (U+2018, o'zbek imlosi), tutuq belgisi -> U+2019.
    Asl yozuv katta harfda bo'lsa, tarjima ham katta harfda (skanlatsiya uslubi).
    """
    t = re.sub(r"([OoGg])'", r"\1‘", text)
    t = t.replace("'", "’").replace("—", "-")   # Digital Strip'da "—" yo'q
    # Namuna (2026-09-30): skanlatsiya uslubi - doim KATTA harf. FONT_UPPER=0 - asl uslub.
    return t.upper() if upper or FONT_UPPER else t


def _shape_depth(shape) -> np.ndarray:
    """Pufakcha ichidagi har nuqtaning chetgacha masofasi (px). Bir marta hisoblanadi,
    keyin istalgan zaxira (margin) uchun `depth > margin` - arzon."""
    fill = shape[2].astype(np.uint8)
    try:
        import cv2

        return cv2.distanceTransform(fill, cv2.DIST_L2, 3)
    except ImportError:
        return fill.astype(np.float32) * 1e6


def _span_at(mask: np.ndarray, r0: int, r1: int, cx: int) -> tuple[int, int] | None:
    """[r0, r1) qatorlarining HAMMASIDA ichkarida bo'lgan, cx atrofidagi uzluksiz oraliq."""
    h, w = mask.shape
    if r0 < 0 or r1 > h or r1 <= r0:
        return None
    rows = mask[r0:r1].all(axis=0)
    if not rows.any():
        return None
    cx = min(max(cx, 0), w - 1)
    if not rows[cx]:
        idx = np.flatnonzero(rows)
        cx = int(idx[np.abs(idx - cx).argmin()])
    a = cx
    while a > 0 and rows[a - 1]:
        a -= 1
    b = cx
    while b + 1 < w and rows[b + 1]:
        b += 1
    return a, b + 1


def _layout_in_shape(draw, text: str, mask: np.ndarray, cx: int, cy: int, size: int):
    """Berilgan o'lchamda matnni pufakcha shakliga moslab qatorlarga bo'ladi.

    Har qatorning eni - pufakchaning AYNAN o'sha balandlikdagi eni (dumaloq
    pufakchada o'rtadagi qatorlar keng, yuqori/pastki qatorlar tor). Blok asl
    matn markazida turadi. Sig'masa - None.
    """
    font = _load_font(size)
    lh = _line_height(font)
    words = text.split()
    if not words:
        return None
    h = mask.shape[0]
    for n in range(1, len(words) + 1):
        total = lh * n
        if total > h:
            return None
        top = int(min(max(cy - total / 2, 0), h - total))
        lines, spans, k = [], [], 0
        for i in range(n):
            span = _span_at(mask, top + i * lh, top + (i + 1) * lh, cx)
            if span is None:
                break
            limit = span[1] - span[0]
            cur = ""
            while k < len(words):
                trial = (cur + " " + words[k]).strip()
                if draw.textlength(trial, font=font) <= limit:
                    cur, k = trial, k + 1
                else:
                    break
            if not cur:
                break
            lines.append(cur)
            spans.append(span)
        if k == len(words) and len(lines) == n:
            return font, lines, spans, top, lh
        # Bu n da sig'madi - ko'proq qator bilan urinib ko'ramiz
    return None


def _draw_in_shape(draw, shape, text_box, text: str, color, max_size: int | None) -> bool:
    """Matnni pufakcha SHAKLIGA moslab, asl matn joyiga yozadi. Bo'lmasa False."""
    ox, oy, _ = shape
    tx1, ty1, tx2, ty2 = text_box
    depth = _shape_depth(shape)
    start = max_size or 60
    for size in range(max(10, start), 9, -1):
        mask = depth > max(3, size // 3)       # matn pufakcha chizig'iga tegmasin
        if not mask.any():
            continue
        cx, cy = (tx1 + tx2) // 2 - ox, (ty1 + ty2) // 2 - oy
        got = _layout_in_shape(draw, text, mask, cx, cy, size)
        if not got:
            continue
        font, lines, spans, top, lh = got
        for i, (line, (a, b)) in enumerate(zip(lines, spans)):
            w = draw.textlength(line, font=font)
            x = min(max(cx - w / 2, a), b - w)
            draw.text((ox + x, oy + top + i * lh), line, font=font, fill=color,
                      stroke_width=_bold(font), stroke_fill=color)
        return True
    return False


def _inpaint_text(arr: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Rasm/fon ustidagi harflarni "bo'yab" o'chiradi (OpenCV inpaint).

    Bir rangli to'rtburchak bilan yopish rasmda dog' qoldirardi. Inpaint esa
    harf o'rnini atrofdagi rasmdan to'ldiradi. OpenCV bo'lmasa - oddiy yopish.
    Returns: yozish uchun quti.
    """
    H, W = arr.shape[:2]
    x1, y1, x2, y2 = box
    pad = 6
    x1, y1, x2, y2 = max(0, x1 - pad), max(0, y1 - pad), min(W, x2 + pad), min(H, y2 + pad)
    region = arr[y1:y2, x1:x2]
    if region.size == 0:
        return box

    # Fon: qutining chetidagi piksellar (matn odatda o'rtada)
    ring = np.concatenate([region[0], region[-1], region[:, 0], region[:, -1]])
    bg = np.median(ring, axis=0)
    if RENDER_V2:
        # Orqa fonni saqlash: "fondan farq qiladigan hamma narsa" emas, faqat harf
        # shaklidagi (ingichka, fonga nisbatan keskin) piksellar - to'q harf ham, uning
        # oq konturi ham. Rasmning keng qismlari (soch, kiyim) tegilmaydi.
        dark = _text_ink(region, (255, 255, 255), thr=45)
        light = _text_ink(region, (0, 0, 0), thr=45)
        mask = (dark | light).astype(np.uint8) * 255
    else:
        diff = np.abs(region.astype(np.int16) - bg.astype(np.int16)).max(axis=2)
        mask = (diff > 60).astype(np.uint8) * 255
    try:
        import cv2

        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
        fixed = cv2.inpaint(np.ascontiguousarray(region[:, :, ::-1]), mask, 4, cv2.INPAINT_TELEA)
        arr[y1:y2, x1:x2] = fixed[:, :, ::-1]
    except Exception:
        arr[y1:y2, x1:x2][mask > 0] = bg.astype(np.uint8)
    return (x1, y1, x2, y2)


def _is_sfx(item: dict, page_line_h: float | None, image_h: int) -> bool:
    """Katta, qisqa, bezakli matn - tovush effekti (BOOM!, 쾅!).

    Effektni o'chirib ustiga yozish rasmni buzadi (sinovda "BOOM!" harfi ichida
    mayda oq dog' paydo bo'lgan). Shuning uchun effekt joyida qoladi, tarjimasi
    yoniga kichik izoh sifatida yoziladi.
    """
    lh = item.get("line_h")
    if not lh:
        return False
    letters = sum(1 for c in (item.get("original") or "") if c.isalpha())
    if letters == 0 or letters > 10:
        return False
    if page_line_h and lh >= page_line_h * 2.0:
        return True
    return lh >= max(90, image_h * 0.06) and letters <= 8


def _draw_sfx_label(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
                    img_w: int, img_h: int, line_h: float) -> None:
    """Effekt ostiga (joy bo'lmasa ustiga) kichik konturli izoh."""
    x1, y1, x2, y2 = box
    size = int(max(16, min(40, line_h * 0.32)))
    font = _load_font(size)
    tw = draw.textlength(text, font=font)
    th = _line_height(font)
    cx = (x1 + x2) / 2
    tx = int(min(max(4, cx - tw / 2), img_w - tw - 4))
    ty = y2 + 4 if y2 + 4 + th < img_h else max(4, y1 - th - 4)
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255),
              stroke_width=max(2, size // 8), stroke_fill=(0, 0, 0))


def render_translation(image_bytes: bytes, translations: list[dict], quality: int = 95) -> bytes:
    image = load_image(image_bytes)
    arr = np.array(image)
    orig = arr.copy() if RENDER_V2 else None
    W, H = image.width, image.height

    heights = sorted(it["line_h"] for it in translations if it.get("line_h"))
    page_line_h = heights[len(heights) // 2] if len(heights) >= 2 else None

    # 1-bosqich: tozalash (numpy/OpenCV). Har element uchun rejim tanlanadi.
    # O'qish tartibida (yuqoridan pastga) - quyidagi birlashtirish tartibni saqlasin.
    ordered = sorted(
        (it for it in translations if it.get("bbox") and len(it["bbox"]) == 4),
        key=lambda it: (float(it["bbox"][1]), float(it["bbox"][0])),
    )
    jobs = []
    for item in ordered:
        bbox = item.get("bbox")
        uzbek_text = (item.get("uzbek") or "").strip()
        if not bbox or not uzbek_text or len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(round(float(v))) for v in bbox)
        except (TypeError, ValueError):
            continue
        x1, x2 = sorted((max(0, x1), min(W, x2)))
        y1, y2 = sorted((max(0, y1), min(H, y2)))
        if x2 - x1 <= 2 or y2 - y1 <= 2:
            continue
        box = (x1, y1, x2, y2)
        upper = mostly_upper(item.get("original") or "")
        uzbek_text = display_text(uzbek_text, upper)
        line_h = item.get("line_h")
        # Shrift asl harf o'lchamidan oshmasin. OCR qutisi harfdan ~25% baland
        # (bo'shliqlar bilan), shuning uchun 0.8: 0.95 da haqiqiy bobda tarjima
        # asl yozuvdan sezilarli katta chiqdi.
        max_size = int(line_h * 0.8) if line_h else None

        if _is_sfx(item, page_line_h, H):
            jobs.append(("sfx", box, None, uzbek_text, line_h))
            continue

        bg_color = _dominant_color(image.crop(box))
        ink_color = None
        if RENDER_V2:
            bg_color = _bg_around(arr, box, line_h) or bg_color
            ink_color = _ink_color(arr, box, bg_color)
        before = _ink(arr, box, bg_color)
        filled = _fill_bubble(arr, box, bg_color)
        if filled is not None:
            inner, touched, shape = filled
            if touched:
                inner = _limit_area(inner, box)
            # O'Z-O'ZINI TEKSHIRISH: asl harflar haqiqatan o'childimi? Qaysi sabab
            # bilan bo'lmasin (harf teshigi, sahifa cheti, g'ayrioddiy pufakcha)
            # o'chmay qolgan bo'lsa - qolgan siyoh inpaint bilan o'chiriladi.
            # Haqiqiy bobda aynan shu xato ~20 joyda asl yozuvni qoldirgan edi.
            if RENDER_V2:
                # gradient fonda _ink soyani ham "siyoh" deb sanardi - harf detektori bilan
                bx1, by1, bx2, by2 = box
                before = int(_text_ink(orig[by1:by2, bx1:bx2], bg_color).sum())
                after = int(_text_ink(arr[by1:by2, bx1:bx2], bg_color).sum())
            else:
                after = _ink(arr, box, bg_color)
            if before and after / before > 0.15:
                logger.info("Asl yozuv to'liq o'chmadi (%.0f%% qoldi) - qayta o'chirilmoqda",
                            100 * after / before)
                _erase_ink(arr, box, bg_color)
            jobs.append(("bubble", inner, bg_color, uzbek_text, max_size, shape, box, upper,
                         ink_color))
        else:
            area = _inpaint_text(arr, box)
            jobs.append(("art", area, None, uzbek_text, max_size))

    jobs = _merge_same_bubble(jobs)

    # 2-bosqich: yozish
    out = Image.fromarray(arr)
    draw = ImageDraw.Draw(out)
    for job in jobs:
        mode, box, bg_color, text, extra = job[:5]
        if mode == "bubble":
            color = job[8] or _text_color_for(bg_color)
            shape, tbox = job[5], job[6]
            if shape is None or not _draw_in_shape(draw, shape, tbox, text, color, extra):
                _draw_block(draw, box, text, color, max_size=extra)
        elif mode == "art":
            # Rasm ustida o'qilishi uchun: fon yorug' bo'lsa qora matn oq kontur bilan,
            # qorong'i bo'lsa aksincha
            region = np.asarray(out.crop(box).convert("L"))
            bright = region.mean() > 140 if region.size else True
            fg, st = ((20, 20, 20), (255, 255, 255)) if bright else ((255, 255, 255), (0, 0, 0))
            _draw_block(draw, box, text, fg, max_size=extra, stroke=st, pad=2)
        else:
            _draw_sfx_label(draw, box, text, W, H, extra or 40)

    return encode_jpeg(out, quality=quality)


def _ink(arr: np.ndarray, box: tuple[int, int, int, int], bg: tuple[int, int, int]) -> int:
    """Matn qutisi ichida fondan keskin farq qiladigan (siyoh) piksellar soni."""
    x1, y1, x2, y2 = box
    sub = arr[y1:y2, x1:x2]
    if sub.size == 0:
        return 0
    return int((np.abs(sub.astype(np.int16) - np.array(bg, np.int16)).max(axis=2) > 90).sum())


def _erase_ink(arr: np.ndarray, box: tuple[int, int, int, int], bg: tuple[int, int, int]) -> None:
    """Matn qutisida qolgan siyohni o'chiradi: avval inpaint, bo'lmasa fon rangi bilan."""
    x1, y1, x2, y2 = box
    sub = arr[y1:y2, x1:x2]
    if RENDER_V2:
        ink = _text_ink(sub, bg)          # gradient/soya o'chirilmasin - faqat harf
    else:
        ink = (np.abs(sub.astype(np.int16) - np.array(bg, np.int16)).max(axis=2) > 60)
    if not ink.any():
        return
    try:
        import cv2

        mask = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((3, 3), np.uint8), iterations=1)
        fixed = cv2.inpaint(np.ascontiguousarray(sub[:, :, ::-1]), mask, 3, cv2.INPAINT_TELEA)
        arr[y1:y2, x1:x2] = fixed[:, :, ::-1]
    except Exception:
        sub[ink] = bg


def _box_iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if not inter:
        return 0.0
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _merge_same_bubble(jobs: list) -> list:
    """Bitta pufakchaga tushib qolgan bir nechta tarjimani bittaga qo'shadi.

    Agar OCR bitta pufakchaning qatorlarini alohida bersa (va birlashtirish
    ularni qo'shmasa), birinchisini to'ldirish ikkinchisining matnini ham
    o'chiradi va ikkala tarjima BIR JOYGA ustma-ust yozilardi. Endi ular
    o'qish tartibida bitta matn qilinadi.
    """
    out: list = []
    for job in jobs:
        if job[0] == "bubble":
            for k, prev in enumerate(out):
                if prev[0] == "bubble" and _box_iou(prev[1], job[1]) > 0.5:
                    sizes = [s for s in (prev[4], job[4]) if s]
                    tb = (min(prev[6][0], job[6][0]), min(prev[6][1], job[6][1]),
                          max(prev[6][2], job[6][2]), max(prev[6][3], job[6][3]))
                    out[k] = ("bubble", prev[1], prev[2], prev[3] + " " + job[3],
                              min(sizes) if sizes else None, prev[5], tb, prev[7], prev[8])
                    break
            else:
                out.append(job)
        else:
            out.append(job)
    return out


def _limit_area(inner: tuple[int, int, int, int], text_box: tuple[int, int, int, int]):
    """Pufakcha "topilgan" hudud haddan tashqari katta bo'lsa (masalan webtoon'ning
    oq oralig'i) - matnni asl joyi atrofida ushlab qoladi."""
    ix1, iy1, ix2, iy2 = inner
    tx1, ty1, tx2, ty2 = text_box
    tw, th = tx2 - tx1, ty2 - ty1
    if (ix2 - ix1) * (iy2 - iy1) <= 6 * max(1, tw * th):
        return inner
    cx, cy = (tx1 + tx2) / 2, (ty1 + ty2) / 2
    hw, hh = tw * 0.75, th * 0.9
    return (int(max(ix1, cx - hw)), int(max(iy1, cy - hh)),
            int(min(ix2, cx + hw)), int(min(iy2, cy + hh)))
