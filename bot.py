import asyncio
import io
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import admins
import pdf_utils
from config import BASE_DIR, BOT_SUFFIX, BOT_TOKEN, OWNER_ID
from image_editor import render_translation
from translator import TranslationError, finish_page, read_page, translate_page

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_DIR / f"bot{BOT_SUFFIX}.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)

# httpx har bir so'rovni INFO darajasida yozadi va URL ichida BOT TOKENI bor —
# ya'ni token log fayliga tushib qolardi. Faqat xatolarni yozamiz.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Telegram Bot API oddiy tokenlar uchun 20 MB dan katta faylni getFile bilan
# yuklab bo'lmaydi — foydalanuvchiga tushunarli xabar berish uchun oldindan tekshiramiz.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Telegram sendPhoto cheklovi: (kenglik+balandlik) <= 10000 va fayl <= 10 MB,
# aks holda sendDocument (asl sifatda) ishlatiladi.
MAX_PHOTO_DIM_SUM = 10000
MAX_PHOTO_BYTES = 10 * 1024 * 1024

# PDF bir martada nechta sahifa. Tezkor OCR bilan sahifa ~5-15 sekund
# (avval VLM bilan ~2 daqiqa edi, shuning uchun chegara 10 edi).
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "60"))
# Natija PDF sahifalari uchun JPEG sifati (50 MB ga sig'maslik ehtimoli kamroq)
PDF_JPEG_QUALITY = 88
# Bitta PDF bob uchun sekin zaxira AI necha marta ishlashi mumkin (har biri ~90 sek)
VLM_BUDGET_PER_PDF = int(os.getenv("VLM_BUDGET_PER_PDF", "2"))

# Kiruvchi fayl va natijalar (sifatni tekshirish uchun), oxirgi 3 ta ish
ARCHIVE_DIR = BASE_DIR / f"archive{BOT_SUFFIX}"
ARCHIVE_KEEP = 3

# NAVBAT: barcha foydalanuvchilarning fayllari bitta navbatga tushadi va kelish
# tartibida birma-bir bajariladi (AI mahalliy CPU'da - parallel ishlatish umumiy
# vaqtni yutmaydi, faqat ikkalasini ham sekinlashtiradi).
_queue: asyncio.Queue = asyncio.Queue()
_waiting: list[dict] = []                 # navbatda kutayotganlar (o'rin ko'rsatish uchun)
_current: dict = {"job": None}           # hozir bajarilayotgan ish
MAX_QUEUE_PER_USER = 30
_job_counter = {"n": 0}
_ai_semaphore = asyncio.Semaphore(1)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admins.is_allowed(user_id):
        await update.message.reply_text(
            "Bu bot shaxsiy. Sizda hozircha foydalanish huquqi yo'q.\n"
            f"Sizning ID'ingiz: {user_id}\n\n"
            "So'rovingiz bot egasiga yuborildi — ruxsat berilsa, xabar keladi."
        )
        await _ask_owner_to_allow(context, update.effective_user)
        return
    text = (
        "Assalomu alaykum!\n\n"
        "Menga yuboring:\n"
        "• manhwa sahifasi RASMINI (yaxshisi 'fayl' sifatida — sifat yo'qolmaydi)\n"
        f"• yoki PDF bobni (bir martada {MAX_PDF_PAGES} sahifagacha) — BITTA PDF bo'lib qaytadi\n\n"
        "Bir nechta fayl yuborsangiz, navbat bilan birma-bir tarjima qilinadi.\n"
        "Odatda bitta bob 1.5-3 daqiqada tayyor bo'ladi.\n\n"
        "Buyruqlar:\n"
        "/holat — bot va AI tayyormi, tekshirish\n"
        "/navbat — navbatdagi ishlar (o'zingiznikini bekor qilish mumkin)\n"
        "/id — Telegram ID'ingizni ko'rish"
    )
    if admins.is_superadmin(user_id):
        text += (
            "\n\n👑 Siz SUPER ADMINsiz:\n"
            "/admins — adminlar ro'yxati, o'chirish tugmalari\n"
            "/addadmin <id> — admin qo'shish (yoki odamning xabarini menga forward qiling)\n"
            "/removeadmin <id> — adminni o'chirish\n"
            "/navbat — istalgan ishni bekor qila olasiz"
        )
    await update.message.reply_text(text)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Sizning Telegram ID'ingiz: {update.effective_user.id}")


async def _ask_owner_to_allow(context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """Notanish odam yozganda EGASIGA tugmali so'rov yuboradi.

    Shunda egasi ID raqamini qidirib yurmasdan, bitta tugma bilan ruxsat beradi.
    """
    if not OWNER_ID or user.id == OWNER_ID:
        return
    key = f"ruxsat_sorovi_{user.id}"
    if context.bot_data.get(key):      # bir odam uchun bir marta so'raladi
        return
    context.bot_data[key] = True

    name = user.full_name or user.username or str(user.id)
    handle = f" (@{user.username})" if user.username else ""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ruxsat berish", callback_data=f"allow:{user.id}"),
        InlineKeyboardButton("❌ Rad etish", callback_data=f"deny:{user.id}"),
    ]])
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(f"Yangi foydalanuvchi botdan foydalanmoqchi:\n\n"
                  f"Ism: {name}{handle}\nID: {user.id}\n\n"
                  f"Ruxsat berasizmi?"),
            reply_markup=keyboard,
        )
    except TelegramError as exc:
        logger.warning("Egasiga so'rov yuborilmadi: %s", exc)


async def on_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Egasi bosgan tugmani qayta ishlaydi."""
    query = update.callback_query
    await query.answer()
    if not admins.is_superadmin(query.from_user.id):
        await query.answer("Bu tugma faqat super admin uchun.", show_alert=True)
        return

    action, _, raw_id = query.data.partition(":")
    if not raw_id.isdigit():
        return
    target = int(raw_id)

    if action == "allow":
        added = admins.add_admin(target)
        await query.edit_message_text(
            f"{target} — ruxsat berildi ✅" if added else f"{target} allaqachon adminlar ro'yxatida."
        )
        try:
            await context.bot.send_message(
                target,
                "Sizga botdan foydalanishga ruxsat berildi ✅\n"
                "Manhwa sahifasi rasmini yoki PDF faylini yuboring.",
            )
        except TelegramError:
            pass
        logger.info("Admin qo'shildi: %s", target)
    elif action == "deny":
        await query.edit_message_text(f"{target} — rad etildi ❌")
    elif action == "remove":
        removed = admins.remove_admin(target)
        await query.edit_message_text(
            f"{target} — olib tashlandi ✅" if removed else f"{target} ni olib bo'lmadi."
        )
        logger.info("Admin olib tashlandi: %s", target)


async def add_by_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Egasi kimningdir xabarini bu yerga yo'naltirsa - o'shani admin qilishni taklif qiladi.

    Returns: True - xabar shu yerda qayta ishlandi.
    """
    msg = update.message
    origin = getattr(msg, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) if origin else None
    if not admins.is_superadmin(update.effective_user.id) or sender is None:
        return False

    if sender.id == OWNER_ID:
        await msg.reply_text("Bu sizning xabaringiz.")
        return True
    if admins.is_allowed(sender.id):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Adminlikdan olish", callback_data=f"remove:{sender.id}")
        ]])
        await msg.reply_text(
            f"{sender.full_name} (ID: {sender.id}) allaqachon admin.", reply_markup=keyboard
        )
        return True

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Admin qilish", callback_data=f"allow:{sender.id}"),
        InlineKeyboardButton("❌ Yo'q", callback_data=f"deny:{sender.id}"),
    ]])
    await msg.reply_text(
        f"Ism: {sender.full_name}\nID: {sender.id}\n\nBu odamni admin qilaymi?",
        reply_markup=keyboard,
    )
    return True


def _dir_size(path: Path) -> tuple[int, int]:
    """Papkadagi fayllar soni va umumiy hajmi (bayt)."""
    n = size = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                n += 1
                size += f.stat().st_size
    return n, size


def _server_line() -> str:
    cores = os.cpu_count() or 0
    if os.getenv("GITHUB_ACTIONS"):
        return f"Server: GitHub Actions, {cores} yadro (ish bo'lmasa uxlaydi)"
    return f"Server: {cores} yadro"


def _disk_report() -> str:
    """Botning o'zi diskka nima yozadi - /holat da ko'rsatiladi."""
    import shutil

    mb = lambda b: f"{b / 1024 / 1024:.1f} MB"
    _, _, free = shutil.disk_usage(str(BASE_DIR))
    jobs = [p for p in ARCHIVE_DIR.iterdir() if p.is_dir()] if ARCHIVE_DIR.exists() else []
    _, arch = _dir_size(ARCHIVE_DIR)
    _, logs = _dir_size(LOG_DIR)
    lines = [
        f"Disk: {free / 1e9:.1f} GB bo'sh. Bot yozadigan fayllar:",
        f"• arxiv: {len(jobs)} ta ish, {mb(arch)} (oxirgi {ARCHIVE_KEEP} ta: kirish + natija PDF)",
        f"• loglar: {mb(logs)} (ko'pi bilan ~20 MB)",
        "• adminlar ro'yxati (admins.json)",
        "Yakka rasmlar diskka yozilmaydi (xotirada ishlanadi), PDF boblar - faqat arxivga.",
    ]
    if os.getenv("GITHUB_ACTIONS"):
        lines.append("GitHub'da disk vaqtinchalik: bot uxlaganda hammasi o'chadi "
                     "(adminlar Cloudflare'da saqlanadi).")
    return "\n".join(lines)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bot va AI tayyormi - shu yerda ko'rinadi."""
    lines = ["Bot: ishlayapti ✅"]
    busy = "hozir 1 ta ish bajarilyapti" if _current["job"] else "bo'sh"
    lines.append(f"Navbat: {busy}, kutayotganlar: {len(_waiting)} ta")

    # Asosiy o'qish dvigateli (tezkor OCR)
    try:
        import fast_ocr

        ready = "korean" in fast_ocr._engines
        lines.append("O'qish (tezkor OCR): " + ("tayyor ✅" if ready else "yuklanmoqda ⏳"))
    except Exception as exc:
        lines.append(f"O'qish (tezkor OCR): xato ❌ ({exc})")

    # Zaxira AI (Ollama) faqat noutbukda bor. GitHub/telefonda OCR_ENGINE=fast -
    # u yerda Ollama umuman ishlatilmaydi, "ulanmadi ⚠️" deyish noto'g'ri signal edi.
    from translator import OCR_ENGINE

    if OCR_ENGINE == "fast":
        lines.append("Zaxira AI (Ollama): bu serverda kerak emas ✅ (faqat tezkor OCR)")
    else:
        try:
            import json as _json
            import urllib.request

            from config import OLLAMA_HOST
            from translator import MODEL

            with urllib.request.urlopen(OLLAMA_HOST.rstrip("/") + "/api/tags", timeout=5) as r:
                names = [m["name"] for m in _json.loads(r.read().decode("utf-8")).get("models", [])]
            ok = MODEL in names
            lines.append(f"Zaxira AI (Ollama): {'javob beryapti ✅' if ok else 'model topilmadi ⚠️'}")
        except Exception:
            lines.append("Zaxira AI (Ollama): ulanmadi ⚠️ (oddiy sahifalar baribir ishlaydi)")

    # Tarjima xizmati
    try:
        from uz_translate import translate_text

        probe = await asyncio.to_thread(translate_text, "hello")
        lines.append(f"Tarjima: ishlayapti ✅ (sinov: hello -> {probe})")
    except Exception as exc:
        lines.append(f"Tarjima: xato ❌ ({exc})")

    lines.append(_server_line())
    lines.append(_disk_report())
    lines.append("\nRasm yuboring — men tayyorman.")
    await update.message.reply_text("\n".join(lines))


async def list_admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admins.is_superadmin(user_id):
        await update.message.reply_text("Bu buyruq faqat super admin uchun.")
        return
    ids = admins.list_admins()
    lines, buttons = [], []
    for i in ids:
        if i == OWNER_ID:
            lines.append(f"• 👑 {i} — super admin (bot egasi)")
            continue
        label = str(i)
        try:
            chat = await context.bot.get_chat(i)
            label = chat.full_name or chat.username or str(i)
            lines.append(f"• {label} ({i})")
        except TelegramError:
            lines.append(f"• {i}")
        buttons.append([InlineKeyboardButton(f"🗑 {label} ni o'chirish",
                                             callback_data=f"remove:{i}")])

    await update.message.reply_text(
        "Adminlar ro'yxati:\n" + "\n".join(lines) +
        "\n\nYangi admin qo'shish: o'sha odamning xabarini menga yo'naltiring "
        "(forward), yoki u botga /start yozsa sizga tugmali so'rov keladi.",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admins.is_superadmin(user_id):
        await update.message.reply_text("Bu buyruq faqat super admin uchun.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Foydalanish: /addadmin <telegram_id>")
        return
    new_id = int(context.args[0])
    if admins.add_admin(new_id):
        await update.message.reply_text(f"{new_id} admin sifatida qo'shildi.")
    else:
        await update.message.reply_text(f"{new_id} allaqachon admin.")


async def remove_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admins.is_superadmin(user_id):
        await update.message.reply_text("Bu buyruq faqat super admin uchun.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Foydalanish: /removeadmin <telegram_id>")
        return
    del_id = int(context.args[0])
    if admins.remove_admin(del_id):
        await update.message.reply_text(f"{del_id} adminlikdan olib tashlandi.")
    else:
        await update.message.reply_text("Bu ID admin emas yoki bot egasini olib bo'lmaydi.")


async def _keep_typing(bot, chat_id: int) -> None:
    """Chat action ~5 s da o'chib qoladi — uzoq ishlov paytida uni yangilab turadi."""
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = update.message
    kind = "photo" if msg.photo else (
        f"document({msg.document.mime_type}, {msg.document.file_name})" if msg.document else "?"
    )
    logger.info("QABUL QILINDI: user=%s, tur=%s", user_id, kind)

    if not admins.is_allowed(user_id):
        await update.message.reply_text(
            f"Sizda ruxsat yo'q. ID'ingiz: {user_id}\n"
            "So'rovingiz bot egasiga yuborildi."
        )
        await _ask_owner_to_allow(context, update.effective_user)
        return

    # Fayl sifatida katta bo'lsa - navbatga qo'ymasdan darhol aytamiz
    photo = msg.photo[-1] if msg.photo else msg.document
    if getattr(photo, "file_size", None) and photo.file_size > MAX_DOWNLOAD_BYTES:
        await msg.reply_text(
            "Bu fayl juda katta (20 MB dan oshadi) — Telegram bot buni yuklab "
            "ololmaydi. Iltimos, kichikroq qilib (yoki bobni bo'lib) qayta yuboring."
        )
        return

    mine = sum(1 for j in _waiting if j["user"] == user_id)
    if mine >= MAX_QUEUE_PER_USER:
        await msg.reply_text(f"Navbatda sizning {mine} ta ishingiz bor — avval ular tugasin.")
        return

    # NAVBAT: avval ikkinchi fayl "kuting" deb rad etilardi va bitta bob ishlanayotganda
    # bot boshqa hech qanday xabarga javob bermasdi. Endi har bir fayl navbatga
    # qo'yiladi va ular kelish tartibida birma-bir bajariladi.
    ahead = len(_waiting) + (1 if _current["job"] else 0)
    text = ("Qabul qilindi, boshlanmoqda..." if ahead == 0 else
            f"Navbatga qo'yildi ⏳ Oldingizda {ahead} ta ish bor — navbat kelganda o'zim boshlayman.")
    _job_counter["n"] += 1
    fname = (msg.document.file_name if msg.document else None) or "rasm"
    job = {"update": update, "context": context, "user": user_id, "id": _job_counter["n"],
           "name": fname, "who": update.effective_user.full_name or str(user_id),
           "cancelled": False, "status": await msg.reply_text(text)}
    _waiting.append(job)
    await _queue.put(job)
    logger.info("Navbatga qo'yildi: user=%s, oldinda=%d", user_id, ahead)


async def _queue_worker() -> None:
    """Navbatdagi ishlarni kelish tartibida birma-bir bajaradi."""
    while True:
        job = await _queue.get()
        if job in _waiting:
            _waiting.remove(job)
        if job.get("cancelled"):               # /navbat orqali bekor qilingan
            _queue.task_done()
            continue
        _current["job"] = job
        await _refresh_positions()
        try:
            await _process_photo(job["update"], job["context"], job["status"])
        except Exception:
            logger.exception("Navbatdagi ish xatosi (user=%s)", job["user"])
            await _edit_status(job["status"], "Kechirasiz, kutilmagan xatolik yuz berdi.")
        finally:
            _current["job"] = None
            _queue.task_done()


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/navbat - navbatdagi ishlar. Super admin istalganini, admin o'zinikini bekor qiladi."""
    user_id = update.effective_user.id
    if not admins.is_allowed(user_id):
        return
    sup = admins.is_superadmin(user_id)
    lines, buttons = [], []
    cur = _current["job"]
    if cur:
        lines.append(f"▶️ Hozir: {cur['name']} ({cur['who']})")
    for pos, job in enumerate(_waiting, start=1):
        lines.append(f"{pos}. {job['name']} ({job['who']})")
        if sup or job["user"] == user_id:
            buttons.append([InlineKeyboardButton(f"❌ {pos}-o'rinni bekor qilish",
                                                 callback_data=f"qcancel:{job['id']}")])
    if not lines:
        await update.message.reply_text("Navbat bo'sh ✅")
        return
    await update.message.reply_text("Navbat:\n" + "\n".join(lines),
                                    reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def on_queue_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    job_id = int(query.data.split(":", 1)[1])
    job = next((j for j in _waiting if j["id"] == job_id), None)
    if job is None:
        await query.answer("Bu ish allaqachon boshlangan yoki tugagan.", show_alert=True)
        return
    if not (admins.is_superadmin(uid) or job["user"] == uid):
        await query.answer("Faqat o'z ishingizni bekor qila olasiz.", show_alert=True)
        return
    job["cancelled"] = True
    _waiting.remove(job)
    await query.answer("Bekor qilindi")
    await query.edit_message_text(f"❌ Bekor qilindi: {job['name']}")
    await _edit_status(job["status"], "❌ Bu ish navbatdan olib tashlandi.")
    await _refresh_positions()
    logger.info("Navbatdan bekor qilindi: %s (kim: %s)", job["name"], uid)


async def _refresh_positions() -> None:
    """Kutayotganlarga yangi o'rnini ko'rsatadi."""
    for pos, job in enumerate(list(_waiting), start=1):
        await _edit_status(job["status"],
                           f"Navbatda ⏳ Oldingizda {pos} ta ish bor — navbat kelganda o'zim boshlayman.")


async def _translate_one(image_bytes: bytes, status_msg, prefix: str = "") -> tuple[bytes, list] | None:
    """Bitta rasmni tarjima qilib, chizilgan natijani qaytaradi.

    None - matn topilmadi.
    """
    await status_msg.edit_text(f"{prefix}1/3 — Rasmdagi matnlar o'qilmoqda (AI)...")
    t0 = asyncio.get_running_loop().time()
    async with _ai_semaphore:
        translations = await asyncio.to_thread(translate_page, image_bytes)
    logger.info(
        "AI tugadi: %d ta matn, %.0f sekund",
        len(translations),
        asyncio.get_running_loop().time() - t0,
    )
    if not translations:
        return None

    await status_msg.edit_text(f"{prefix}2/3 — {len(translations)} ta matn topildi, chizilmoqda...")
    result_bytes = await asyncio.to_thread(render_translation, image_bytes, translations)
    await status_msg.edit_text(f"{prefix}3/3 — Yuborilmoqda...")
    return result_bytes, translations


def _caption(translations: list, header: str = "Tarjima tayyor:") -> str:
    lines = [f"• {t.get('uzbek', '')}" for t in translations if t.get("uzbek")]
    return (header + "\n\n" + "\n".join(lines))[:1000]


async def _edit_status(status_msg, text: str) -> None:
    """Holat xabarini yangilaydi; Telegram xatosi (masalan matn o'zgarmagan) ishni to'xtatmasin."""
    try:
        await status_msg.edit_text(text)
    except TelegramError:
        pass


def _archive(job: str, name: str, data: bytes) -> None:
    """Kiruvchi fayl va natijani saqlaydi - sifatni keyin tekshirish uchun.

    Faqat oxirgi ARCHIVE_KEEP ta ish saqlanadi (disk to'lmasin).
    """
    try:
        folder = ARCHIVE_DIR / job
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        jobs = sorted((p for p in ARCHIVE_DIR.iterdir() if p.is_dir()), key=lambda p: p.name)
        for old in jobs[:-ARCHIVE_KEEP]:
            for f in old.iterdir():
                f.unlink(missing_ok=True)
            old.rmdir()
    except OSError as exc:
        logger.warning("Arxivga saqlanmadi: %s", exc)


async def _process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       pdf_bytes: bytes, status_msg) -> None:
    """PDF bobni to'liq tarjima qilib, BITTA PDF qilib qaytaradi (<= 50 MB).

    Avval har sahifa alohida rasm bo'lib yuborilardi - foydalanuvchi bitta
    fayl so'radi.
    """
    chat_id = update.effective_chat.id
    doc = update.message.document
    src_name = (getattr(doc, "file_name", None) or "bob.pdf").rsplit(".", 1)[0]
    job = time.strftime("%Y%m%d-%H%M%S")
    _archive(job, "kirish.pdf", pdf_bytes)

    try:
        total = await asyncio.to_thread(pdf_utils.page_count, pdf_bytes)
    except pdf_utils.PdfError as exc:
        await status_msg.edit_text(str(exc))
        return

    limit = min(total, MAX_PDF_PAGES)
    logger.info("PDF: %d sahifa, %d tasi ishlanadi", total, limit)
    note = "" if total <= MAX_PDF_PAGES else (
        f"\n(Bir martada eng ko'pi {MAX_PDF_PAGES} sahifa — qolganini alohida yuboring.)"
    )
    await _edit_status(status_msg, f"PDF qabul qilindi: {total} sahifa.\n"
                                   f"Hammasi tarjima qilinib, BITTA PDF bo'lib qaytadi.{note}")

    pages = await asyncio.to_thread(lambda: list(pdf_utils.render_pages(pdf_bytes, limit)))
    if not pages:
        await status_msg.edit_text("PDF sahifalarini rasmga aylantirib bo'lmadi.")
        return

    budget = {"vlm": VLM_BUDGET_PER_PDF}
    out_pages: list[bytes] = []
    failed: list[int] = []
    texts = 0
    t0 = time.time()
    # KONVEYER: sahifa N o'qilayotganda (OCR - barcha yadrolar) N-1 sahifaning
    # tarjimasi (Google - tarmoqni kutish) va chizilishi fonda ketadi. Avval hammasi
    # ketma-ket edi: 21 sahifalik bobda tarjima 34 s, chizish 24 s OCR'ga qo'shilardi.
    async def finish(jpeg: bytes, read: list[dict]) -> tuple[bytes, int]:
        items = await asyncio.to_thread(finish_page, read) if read else []
        if not items:
            return jpeg, 0                    # matnsiz sahifa - aslicha (PDF to'liq bo'lsin)
        return await asyncio.to_thread(render_translation, jpeg, items, PDF_JPEG_QUALITY), len(items)

    pending: list[asyncio.Task] = []
    for num, _total, jpeg in pages:
        elapsed = int(time.time() - t0)
        await _edit_status(status_msg, f"Tarjima qilinmoqda: {num}/{limit}-sahifa "
                                       f"({elapsed // 60}:{elapsed % 60:02d} o'tdi)")
        try:
            async with _ai_semaphore:
                read = await asyncio.to_thread(read_page, jpeg, "image/jpeg", budget)
        except TranslationError as exc:
            logger.warning("PDF %d-sahifa tarjima bo'lmadi: %s", num, exc)
            read, failed = [], failed + [num]
        pending.append(asyncio.create_task(finish(jpeg, read)))
        # Xotira to'lmasin: fonda ko'pi bilan 3 ta sahifa
        while sum(not t.done() for t in pending) > 3:
            await asyncio.wait([t for t in pending if not t.done()], return_when=asyncio.FIRST_COMPLETED)
    for page_bytes, n in await asyncio.gather(*pending):
        out_pages.append(page_bytes)
        texts += n

    await _edit_status(status_msg, "PDF yig'ilmoqda...")
    fitted, size_note = await asyncio.to_thread(pdf_utils.fit_size, out_pages)
    result_pdf = await asyncio.to_thread(pdf_utils.build_pdf, fitted)
    _archive(job, "natija.pdf", result_pdf)
    elapsed = int(time.time() - t0)

    caption = (f"Tarjima tayyor: {limit} sahifa, {texts} ta matn.\n"
               f"Hajm: {len(result_pdf) / 1024 / 1024:.1f} MB ({size_note}), "
               f"vaqt: {elapsed // 60}:{elapsed % 60:02d}.")
    if failed:
        caption += f"\nTarjima qilinmagan sahifalar (aslicha qoldi): {', '.join(map(str, failed))}"
    if budget["vlm"] <= 0 and VLM_BUDGET_PER_PDF:
        caption += "\nBa'zi qiyin joylar tezlik uchun o'tkazib yuborilgan bo'lishi mumkin."

    await _edit_status(status_msg, "Yuborilmoqda...")
    for attempt in range(4):
        try:
            await context.bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(result_pdf),
                filename=f"{src_name} (o'zbekcha).pdf",
                caption=caption,
                # katta faylni sekin internetda yuklash uchun uzoqroq kutish
                write_timeout=900, read_timeout=300,
            )
            break
        except RetryAfter as exc:
            raw = getattr(exc, "retry_after", 5)
            await asyncio.sleep(float(getattr(raw, "total_seconds", lambda: raw)()) + 1)
    await _edit_status(status_msg, "Tayyor ✅")
    logger.info("PDF yuborildi: %d sahifa, %.1f MB, %d sek",
                limit, len(result_pdf) / 1024 / 1024, elapsed)


async def _process_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> None:
    chat_id = update.effective_chat.id
    await _edit_status(status_msg, "Navbatingiz keldi — yuklab olinmoqda...")
    typing_task = asyncio.create_task(_keep_typing(context.bot, chat_id))

    try:
        photo = update.message.photo[-1] if update.message.photo else None
        if photo is None and update.message.document:
            photo = update.message.document

        if getattr(photo, "file_size", None) and photo.file_size > MAX_DOWNLOAD_BYTES:
            await status_msg.edit_text(
                "Bu fayl juda katta (20 MB dan oshadi) — Telegram bot buni yuklab "
                "ololmaydi. Iltimos, rasmni siqib yoki kichikroq holda qayta yuboring."
            )
            return

        tg_file = await photo.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        file_bytes = buf.getvalue()
        logger.info("Fayl yuklandi: %.0f KB", len(file_bytes) / 1024)

        # PDF bo'lsa - har bir sahifa alohida tarjima qilinadi
        doc = update.message.document
        if pdf_utils.is_pdf(file_bytes, getattr(doc, "file_name", None),
                            getattr(doc, "mime_type", None)):
            await _process_pdf(update, context, file_bytes, status_msg)
            return

        result = await _translate_one(file_bytes, status_msg)
        if result is None:
            await status_msg.edit_text("Ushbu rasmda tarjima qilinadigan matn topilmadi.")
            return
        result_bytes, translations = result
        logger.info("Chizildi: %.0f KB", len(result_bytes) / 1024)

        await _send_result(context, chat_id, result_bytes, _caption(translations))
        await status_msg.delete()
    except TranslationError as exc:
        logger.warning("Tarjima xatosi (user=%s): %s", update.effective_user.id, exc)
        await status_msg.edit_text(str(exc))
    except TelegramError as exc:
        logger.exception("Telegram xatosi (user=%s)", update.effective_user.id)
        await status_msg.edit_text(
            f"Telegramga yuborishda xatolik yuz berdi: {exc}. Qaytadan urinib ko'ring."
        )
    except Exception:
        logger.exception("Kutilmagan xatolik (user=%s)", update.effective_user.id)
        await status_msg.edit_text(
            "Kechirasiz, kutilmagan xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring."
        )
    finally:
        typing_task.cancel()


async def _send_result(context: ContextTypes.DEFAULT_TYPE, chat_id: int, image_bytes: bytes, caption: str) -> None:
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(image_bytes)) as im:
        dim_sum = im.width + im.height
    as_document = len(image_bytes) > MAX_PHOTO_BYTES or dim_sum > MAX_PHOTO_DIM_SUM

    # Ko'p sahifali PDF'da ketma-ket ko'p rasm yuboriladi - Telegram buni
    # cheklaydi (RetryAfter). Shunda aytilgan vaqt kutib, qayta yuboramiz.
    for attempt in range(4):
        try:
            if as_document:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(image_bytes),
                    filename="tarjima.jpg",
                    caption=caption,
                )
            else:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=io.BytesIO(image_bytes), caption=caption
                )
            return
        except RetryAfter as exc:
            raw = getattr(exc, "retry_after", 5)
            if hasattr(raw, "total_seconds"):          # yangi versiyalarda timedelta
                raw = raw.total_seconds()
            wait = float(raw or 5)
            logger.info("Telegram cheklovi: %.0f sek kutilmoqda", wait)
            await asyncio.sleep(wait + 1)
    raise TelegramError("Telegram rasmni qabul qilmadi (ko'p urinishdan keyin)")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rasm bo'lmagan xabarlar. Avval bot bunday xabarlarga JIM qolardi —
    foydalanuvchi bot ishlayaptimi yoki yo'qmi bilolmasdi."""
    msg = update.message
    if msg is None:
        return
    user_id = update.effective_user.id
    if msg.document:
        what = f"hujjat ({msg.document.mime_type or 'nomaʼlum tur'})"
    elif msg.video or msg.animation:
        what = "video"
    elif msg.sticker:
        what = "stiker"
    elif msg.text:
        what = "matn"
    else:
        what = "bu xabar"
    logger.info("QABUL QILINDI (mos emas): user=%s, tur=%s", user_id, what)

    # Egasi kimningdir xabarini yo'naltirsa - uni admin qilish taklif qilinadi
    if await add_by_forward(update, context):
        return

    if not admins.is_allowed(user_id):
        await msg.reply_text(
            f"Sizda ruxsat yo'q. ID'ingiz: {user_id}\n"
            "So'rovingiz bot egasiga yuborildi."
        )
        await _ask_owner_to_allow(context, update.effective_user)
        return
    await msg.reply_text(
        f"Men {what}ni tarjima qila olmayman.\n\n"
        "Menga manhwa sahifasining RASMINI yuboring — oddiy rasm sifatida yoki "
        "'fayl' sifatida (sifat yo'qolmasligi uchun fayl yaxshiroq).\n\n"
        "Bot ishlayapti va sizni kutmoqda."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Vaqtinchalik tarmoq uzilishlari (internet bir lahza uzilsa) - polling o'zi
    # qayta ulanadi. Ular uchun egasiga xabar yuborilmaydi: avval tungi 01:03 dagi
    # oddiy "httpx.ReadError" uchun ham "Botda xatolik" xabari ketgan.
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Vaqtinchalik tarmoq xatosi (o'zi tiklanadi): %s", context.error)
        return
    logger.exception("Global handlerda tutilmagan xato", exc_info=context.error)
    try:
        if OWNER_ID:
            await context.bot.send_message(
                chat_id=OWNER_ID, text=f"Botda xatolik yuz berdi:\n{context.error}"
            )
    except TelegramError:
        pass


# Telegram API manzili: odatda api.telegram.org. Hugging Face Space'da u
# yopiq bo'lishi mumkin - o'shanda Cloudflare'dagi `manhwa-gate` orqali
# (TG_API_BASE=https://.../tg) ishlaydi.
TG_API_BASE = os.getenv("TG_API_BASE", "").rstrip("/")


def _build_app(token: str) -> Application:
    builder = Application.builder()
    if TG_API_BASE:
        builder = builder.base_url(f"{TG_API_BASE}/bot").base_file_url(f"{TG_API_BASE}/file/bot")
    app = (
        builder
        .token(token)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("holat", status_cmd))
    app.add_handler(CommandHandler("navbat", queue_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("admins", list_admins_cmd))
    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("removeadmin", remove_admin_cmd))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF, handle_photo))
    # Qolgan hamma narsa (buyruqlardan tashqari) - jim qolmaslik uchun
    app.add_handler(MessageHandler(~filters.COMMAND, handle_other))
    app.add_handler(CallbackQueryHandler(on_queue_button, pattern=r"^qcancel:"))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^(allow|deny|remove):"))
    app.add_error_handler(on_error)
    return app


# GIBRID REJIM: telefon - asosiy (HEARTBEAT_ROLE=primary), kompyuter - zaxira
# (HEARTBEAT_ROLE=reserve). Telegram bitta botni faqat BITTA joyda so'rashga
# ruxsat beradi, shuning uchun asosiy har BEAT_EVERY s da Cloudflare'dagi
# `manhwa-heartbeat` ga "tirikman" yuboradi; zaxira signal STALE_AFTER s kelmasa
# botni o'zi yoqadi, signal qaytsa qo'lidagi ishlarni TUGATIB o'chadi.
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "").rstrip("/")
HEARTBEAT_KEY = os.getenv("HEARTBEAT_KEY", "")
HEARTBEAT_ROLE = os.getenv("HEARTBEAT_ROLE", "").lower()
BEAT_EVERY = 30
STALE_AFTER = 90     # asosiydan shuncha soniya signal bo'lmasa - zaxira yoqiladi
FRESH_WITHIN = 45    # signal shundan yangi bo'lsa - asosiy qaytdi, zaxira o'chadi
SLOT = os.getenv("HEARTBEAT_SLOT") or BOT_SUFFIX or "1"  # HEARTBEAT_SLOT - faqat sinov uchun


async def _hb_request(method: str, path: str):
    import httpx

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.request(method, HEARTBEAT_URL + path, headers={"x-key": HEARTBEAT_KEY})
        r.raise_for_status()
        return r.json()


async def _beat_loop() -> None:
    while True:
        try:
            await _hb_request("POST", f"/beat/{SLOT}")
        except Exception as exc:
            logger.warning("Signal yuborilmadi: %s", exc)
        await asyncio.sleep(BEAT_EVERY)


async def _primary_age() -> int | None:
    """Asosiy (telefon) signali necha soniya oldin kelgan; bilib bo'lmasa None."""
    try:
        data = await _hb_request("GET", "/status")
    except Exception as exc:
        logger.warning("Signal holati olinmadi: %s", exc)
        return None
    age = data.get(SLOT)
    return 10**9 if age is None else int(age)


async def _start_app():
    app = _build_app(BOT_TOKEN)
    await app.initialize()
    await app.start()
    # drop_pending_updates=False: bot qayta ishga tushayotganda yuborilgan
    # xabarlar YO'QOLMAYDI (avval yo'qolardi - foydalanuvchi fayl yuborib,
    # hech qanday javob olmasdi).
    await app.updater.start_polling(drop_pending_updates=False)
    logger.info("Bot ishga tushdi: @%s", app.bot.username)
    return app


async def _stop_app(app) -> None:
    for step in (app.updater.stop, app.stop, app.shutdown):
        try:
            await step()
        except Exception:
            logger.exception("To'xtatishda xato")


async def _tell_owner(app, text: str) -> None:
    try:
        await app.bot.send_message(chat_id=OWNER_ID, text=text)
    except TelegramError:
        pass


async def _run_reserve() -> None:
    """Kompyuter: telefon o'chsa botni yoqadi, qaytsa ishlarni tugatib o'chadi."""
    logger.info("Zaxira rejim (bot %s): telefon signali kuzatilmoqda", SLOT)
    while True:
        age = await _primary_age()
        if age is None or age <= STALE_AFTER:
            await asyncio.sleep(BEAT_EVERY)
            continue
        logger.info("Telefon signali %s s yo'q - kompyuter botni yoqmoqda", age)
        app = await _start_app()
        await _tell_owner(app, "📱 Telefon javob bermayapti — bot vaqtincha kompyuterda ishlayapti.")
        while True:
            await asyncio.sleep(BEAT_EVERY)
            age = await _primary_age()
            if age is not None and age <= FRESH_WITHIN:
                break
        logger.info("Telefon qaytdi - yangi xabar olish to'xtatildi, navbat tugatilmoqda")
        await app.updater.stop()            # yangi xabarni endi telefon oladi
        while _waiting or _current["job"]:  # qabul qilingan ishlar yarimda qolmasin
            await asyncio.sleep(5)
        await _tell_owner(app, "📱 Telefon qaytdi — bot yana telefonda ishlayapti.")
        await _stop_app(app)


async def _run() -> None:
    """Bitta botni ishga tushiradi (qaysi biri - `config.BOT_SLOT`)."""
    worker = asyncio.create_task(_queue_worker())
    tasks = [worker]
    try:
        if HEARTBEAT_ROLE == "reserve" and HEARTBEAT_URL:
            await _run_reserve()
            return
        app = await _start_app()
        if HEARTBEAT_ROLE == "primary" and HEARTBEAT_URL:
            tasks.append(asyncio.create_task(_beat_loop()))
        try:
            await asyncio.Event().wait()
        finally:
            await _stop_app(app)
    finally:
        for t in tasks:
            t.cancel()


def main() -> None:
    # Tezkor OCR modellarini fonda oldindan yuklaymiz - birinchi rasm ham tez bo'lsin
    import threading

    import fast_ocr

    threading.Thread(target=fast_ocr.warm_up, daemon=True).start()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
