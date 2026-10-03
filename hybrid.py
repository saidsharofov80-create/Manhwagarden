"""@gardenhwa_bot UCH BOSQICHLI rejim: telefon > noutbuk > GitHub Actions.

Cloudflare'ning OG'IR darvozalari (`manhwa-gate*`, D1'ga ko'p yozadi) endi faqat @manhwatarjima_bot
uchun (foydalanuvchi qarori, 2026-10-02). Shuning uchun bu bot ularga umuman tegmaydi - buning o'rniga
ALLAQACHON BOR, juda yengil `manhwa-heartbeat` worker ishlatiladi (bitta jadval, har 30 s da bitta
yozuv - kuniga ~2 880 so'rov/tomon, hisobning 100 000/kun chegarasining ~3 foizi).

Qoida juda oddiy: har tomon o'z nomi bilan "men tirikman" deb belgi qoldiradi (`/beat/<prefix>-<rol>`).
Pastroq ustuvorlikdagi tomon `/status` orqali O'ZIDAN YUQORI barcha tomonlarning yoshini tekshiradi;
ulardan BIRI ham STALE_AFTER soniyadan yangi bo'lsa - jim turadi. Hammasi jim bo'lib qolsa - ishga
tushadi va o'zi ham belgi qoldira boshlaydi (shunda undan pastroq tomon ham "menga ham kerak emas"
deb biladi). Yuqori tomon qaytsa - joriy ishlarni TUGATIB, jim turishga qaytadi (xabar yo'qolmaydi).
"""
import asyncio
import logging
import os

import httpx

log = logging.getLogger("hybrid")

HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "").rstrip("/")
HEARTBEAT_KEY = os.getenv("HEARTBEAT_KEY", "")
PREFIX = os.getenv("HEARTBEAT_PREFIX", "garden")
ROLE = os.getenv("HEARTBEAT_ROLE", "").lower()          # phone | laptop | github
TIERS = ["phone", "laptop", "github"]                     # ustuvorlik tartibida (eng tirigi birinchi)
BEAT_EVERY = 30
STALE_AFTER = 90     # shuncha soniya signal kelmasa - o'sha tomon o'lik
FRESH_WITHIN = 45    # signal shundan yangi bo'lsa - o'sha tomon qaytdi


def enabled() -> bool:
    return bool(HEARTBEAT_URL and HEARTBEAT_KEY and ROLE in TIERS)


def _slot(role: str) -> str:
    return f"{PREFIX}-{role}"


async def _req(c: httpx.AsyncClient, method: str, path: str) -> dict:
    r = await c.request(method, HEARTBEAT_URL + path, headers={"x-key": HEARTBEAT_KEY}, timeout=15)
    r.raise_for_status()
    return r.json()


async def beat_loop(c: httpx.AsyncClient) -> None:
    """O'z borligini bildirib turadi (faol bo'lsin-bo'lmasin - pastroq tomon ko'rishi uchun)."""
    slot = _slot(ROLE)
    while True:
        try:
            await _req(c, "POST", f"/beat/{slot}")
        except Exception as exc:
            log.warning("Signal yuborilmadi: %s", exc)
        await asyncio.sleep(BEAT_EVERY)


_fail_streak = 0
STATUS_FAIL_GRACE = 2   # shuncha ketma-ket urinish muvaffaqiyatsiz bo'lsa - "bilmayman" emas, "stale"


async def _higher_age(c: httpx.AsyncClient) -> int | None:
    """Mendan ustunroq tomonlardan ENG YANGI signal yoshi (soniya); hech biri bo'lmasa None."""
    global _fail_streak
    try:
        status = await _req(c, "GET", "/status")
        _fail_streak = 0
    except Exception as exc:
        _fail_streak += 1
        log.warning("Signal holati olinmadi (%d-marta ketma-ket): %s", _fail_streak, exc)
        if _fail_streak <= STATUS_FAIL_GRACE:
            return 0   # qisqa uzilish: xavfsiz tomondan - ustunroq hali tirik deb hisoblanadi
        # Cloudflare uzoq vaqt javob bermayapti (masalan, hisobning kunlik limiti): muvofiqlashtirib
        # bo'lmaydi, lekin foydalanuvchilarni soatlab javobsiz qoldirishdan ko'ra shu tomon xabar olgani
        # afzal - signal tiklanishi bilan navbatdagi tekshiruvda yana odatdagi tartibga qaytiladi.
        return None
    my_i = TIERS.index(ROLE)
    best = None
    for tier in TIERS[:my_i]:
        age = status.get(_slot(tier))
        if age is not None and (best is None or age < best):
            best = age
    return best


# GitHub Actions: pulsni kuzatish uchun soatlab ish vaqtini band qilib o'tirmaydi - bitta tekshiruv
# qilib, kerak bo'lmasa darrov chiqadi (ishga tushirish `schedule:` cron bilan tez-tez takrorlanadi).
# Serving boshlagandan keyin esa, albatta, yuqori tomon qaytguncha yoki ish vaqti tugaguncha ishlaydi.
WATCH_ONCE = os.getenv("GITHUB_ACTIONS", "") == "true" and ROLE == "github"


async def run(start_app, stop_app, tell_owner) -> None:
    """ROLE eng yuqori ustuvorlik (`phone`) bo'lsa - doim faol. Aks holda navbat bilan kuzatadi."""
    async with httpx.AsyncClient(timeout=15) as c:
        beat_task = asyncio.create_task(beat_loop(c))
        try:
            if ROLE == TIERS[0]:
                app = await start_app()
                await asyncio.Event().wait()
                return
            log.info("Zaxira rejim (%s): ustunroq tomon kuzatilmoqda", ROLE)
            while True:
                age = await _higher_age(c)
                if age is not None and age <= STALE_AFTER:
                    if WATCH_ONCE:
                        log.info("Ustunroq tomon tirik (%s s) - hozircha kerak emas, chiqilmoqda", age)
                        return
                    await asyncio.sleep(BEAT_EVERY)
                    continue
                log.info("Ustunroq tomon(lar)dan signal %s - %s botni yoqmoqda", age, ROLE)
                app = await start_app()
                await tell_owner(app, f"⚠️ Yuqori daraja javob bermayapti - bot vaqtincha {ROLE}da ishlayapti.")
                while True:
                    await asyncio.sleep(BEAT_EVERY)
                    age = await _higher_age(c)
                    if age is not None and age <= FRESH_WITHIN:
                        break
                log.info("Ustunroq tomon qaytdi - yangi xabar olish to'xtatildi, navbat tugatilmoqda")
                if app.updater.running:   # stop_app() ham shu qadamni bosadi - ikki marta chaqirilsa xato beradi
                    await app.updater.stop()
                from bot import _active, _waiting   # aylanma import'dan qochish uchun shu yerda
                while _waiting or _active:
                    await asyncio.sleep(5)
                await tell_owner(app, f"✅ Yuqori daraja qaytdi - bot yana o'sha yerda ishlayapti.")
                await stop_app(app)
        finally:
            beat_task.cancel()
