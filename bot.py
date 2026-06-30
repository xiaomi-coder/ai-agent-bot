"""
Shaxsiy AI Yordamchi — Telegram Bot (v2)
=========================================
Imkoniyatlar:
- Matn, GOLOS va RASM xabarlarni tushunadi (Gemini)
- Internetdan yangi ma'lumot qidiradi (Google Search)
- Shaxsiy buxgalter: kirim-chiqim + hisobot (chek rasmidan ham o'qiydi!)
- Eslatmalar: "Ertaga 9 da dorini eslatib qo'y" — vaqtida xabar keladi
- Qaydlar: "Eslab qol: ..." — keyin so'rasangiz topib beradi

Ishga tushirish:
  1. .env faylga BOT_TOKEN va GEMINI_API_KEY yozing
  2. pip install -r requirements.txt
  3. python bot.py
"""

import asyncio
import io
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GOOGLE_CSE_KEY = os.getenv("GOOGLE_CSE_KEY", "")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "")
TZ = ZoneInfo(os.getenv("TZ", "Asia/Tashkent"))

bot_enabled = True  # admin o'chirishi mumkin

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise SystemExit("Xato: .env faylda BOT_TOKEN va GEMINI_API_KEY bo'lishi shart!")
if not DATABASE_URL:
    raise SystemExit("Xato: DATABASE_URL bo'lishi shart!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-assistant")

client = genai.Client(api_key=GEMINI_API_KEY)
scheduler = AsyncIOScheduler(timezone=TZ)
BOT: Bot | None = None  # main() da to'ldiriladi


def now_local() -> datetime:
    return datetime.now(TZ)


# ============================================================
# BAZA (PostgreSQL)
# ============================================================

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def db_init():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('kirim', 'chiqim')),
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    note TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    text TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    first_seen TIMESTAMP NOT NULL DEFAULT NOW(),
                    last_seen TIMESTAMP NOT NULL DEFAULT NOW(),
                    message_count INTEGER NOT NULL DEFAULT 0,
                    approved BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id BIGINT PRIMARY KEY,
                    name TEXT,
                    profession TEXT,
                    interests TEXT,
                    language TEXT DEFAULT 'uz',
                    goals TEXT,
                    onboarded BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS long_memory (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)


# --- Buxgalteriya ---

def db_add_transaction(user_id: int, tx_type: str, amount: float, category: str, note: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO transactions (user_id, type, amount, category, note) VALUES (%s, %s, %s, %s, %s)",
                (user_id, tx_type, amount, category, note),
            )
    return f"Yozildi: {tx_type} {amount:,.0f} so'm, kategoriya: {category}" + (f" ({note})" if note else "")


def db_get_report(user_id: int, period: str = "oy", start_date: str = "", end_date: str = "") -> str:
    now = now_local().replace(tzinfo=None)
    label = period

    # Aniq sana oralig'i berilgan bo'lsa — o'shani ishlatamiz
    if start_date:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return "Boshlanish sanasi noto'g'ri (YYYY-MM-DD bo'lishi kerak)."
        if end_date:
            try:
                end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except ValueError:
                return "Tugash sanasi noto'g'ri (YYYY-MM-DD bo'lishi kerak)."
        else:
            end = start.replace(hour=23, minute=59, second=59)
        label = f"{start_date}" + (f" — {end_date}" if end_date and end_date != start_date else "")
    else:
        end = now
        if period == "bugun":
            start = now.replace(hour=0, minute=0, second=0)
        elif period == "kecha":
            y = now - timedelta(days=1)
            start = y.replace(hour=0, minute=0, second=0)
            end = y.replace(hour=23, minute=59, second=59)
            label = "kecha"
        elif period == "hafta":
            start = now - timedelta(days=7)
        else:
            start = now - timedelta(days=30)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT type, category, SUM(amount), COUNT(*)
                   FROM transactions
                   WHERE user_id = %s AND created_at >= %s AND created_at <= %s
                   GROUP BY type, category
                   ORDER BY type, SUM(amount) DESC""",
                (user_id, start, end),
            )
            rows = cur.fetchall()

    if not rows:
        return f"Bu davr ({label}) uchun yozuvlar topilmadi."

    kirim_total, chiqim_total = 0.0, 0.0
    lines = [f"Hisobot ({label}):"]
    for row in rows:
        tx_type, category, total, count = row["type"], row["category"], row["sum"], row["count"]
        lines.append(f"- {tx_type} | {category}: {total:,.0f} so'm ({count} ta)")
        if tx_type == "kirim":
            kirim_total += total
        else:
            chiqim_total += total
    lines.append(f"Jami kirim: {kirim_total:,.0f} so'm")
    lines.append(f"Jami chiqim: {chiqim_total:,.0f} so'm")
    lines.append(f"Balans: {kirim_total - chiqim_total:,.0f} so'm")
    return "\n".join(lines)


def db_delete_last(user_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, type, amount, category FROM transactions WHERE user_id = %s ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return "O'chiradigan yozuv yo'q."
            cur.execute("DELETE FROM transactions WHERE id = %s", (row["id"],))
    return f"O'chirildi: {row['type']} {row['amount']:,.0f} so'm ({row['category']})"


# --- Eslatmalar ---

async def fire_reminder(reminder_id: int, user_id: int, text: str):
    """Vaqti kelganda foydalanuvchiga xabar yuboradi."""
    try:
        if BOT:
            await BOT.send_message(user_id, f"⏰ Eslatma: {text}")
        with sqlite3.connect(DB_PATH) as db:
            db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
    except Exception:
        logger.exception("Eslatma yuborishda xato")


def schedule_reminder(reminder_id: int, user_id: int, text: str, remind_at: datetime):
    scheduler.add_job(
        fire_reminder, "date", run_date=remind_at,
        args=[reminder_id, user_id, text],
        id=f"rem_{reminder_id}", replace_existing=True,
    )


def db_set_reminder(user_id: int, text: str, remind_at_str: str) -> str:
    try:
        remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return "Vaqt formati noto'g'ri. 'YYYY-MM-DD HH:MM' formatida bo'lishi kerak."

    if remind_at <= now_local():
        return "Bu vaqt o'tib ketgan. Kelajakdagi vaqtni ayting."

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (user_id, text, remind_at) VALUES (%s, %s, %s) RETURNING id",
                (user_id, text, remind_at.strftime("%Y-%m-%d %H:%M")),
            )
            reminder_id = cur.fetchone()["id"]

    schedule_reminder(reminder_id, user_id, text, remind_at)
    return f"Eslatma o'rnatildi: \"{text}\" — {remind_at.strftime('%d.%m.%Y soat %H:%M')} (№{reminder_id})"


def db_list_reminders(user_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, text, remind_at FROM reminders WHERE user_id = %s AND sent = 0 ORDER BY remind_at",
                (user_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return "Faol eslatmalar yo'q."
    return "Faol eslatmalar:\n" + "\n".join(f"№{r['id']}: {r['text']} — {r['remind_at']}" for r in rows)


def db_delete_reminder(user_id: int, reminder_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reminders WHERE id = %s AND user_id = %s AND sent = 0",
                (reminder_id, user_id),
            )
            deleted = cur.rowcount
    if deleted == 0:
        return f"№{reminder_id} eslatma topilmadi."
    try:
        scheduler.remove_job(f"rem_{reminder_id}")
    except Exception:
        pass
    return f"№{reminder_id} eslatma o'chirildi."


def restore_reminders():
    """Server qayta yonsa — bazadagi eslatmalarni qayta yuklaymiz."""
    now = now_local()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, text, remind_at FROM reminders WHERE sent = 0")
            rows = cur.fetchall()
    restored = 0
    for row in rows:
        remind_at = datetime.strptime(row["remind_at"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if remind_at <= now:
            remind_at = now + timedelta(seconds=10)
        schedule_reminder(row["id"], row["user_id"], row["text"], remind_at)
        restored += 1
    if restored:
        logger.info("%d ta eslatma qayta yuklandi", restored)


# --- Qaydlar ---

def db_add_note(user_id: int, text: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notes (user_id, text) VALUES (%s, %s) RETURNING id",
                (user_id, text),
            )
            note_id = cur.fetchone()["id"]
    return f"Eslab qoldim (№{note_id}): {text}"


def db_find_notes(user_id: int, query: str) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if query:
                cur.execute(
                    "SELECT id, text, created_at FROM notes WHERE user_id = %s AND text ILIKE %s ORDER BY id DESC LIMIT 10",
                    (user_id, f"%{query}%"),
                )
            else:
                cur.execute(
                    "SELECT id, text, created_at FROM notes WHERE user_id = %s ORDER BY id DESC LIMIT 10",
                    (user_id,),
                )
            rows = cur.fetchall()
    if not rows:
        return "Qaydlar topilmadi." if query else "Hali qaydlar yo'q."
    return "Topilgan qaydlar:\n" + "\n".join(f"№{r['id']} ({str(r['created_at'])[:10]}): {r['text']}" for r in rows)


def db_delete_note(user_id: int, note_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notes WHERE id = %s AND user_id = %s", (note_id, user_id))
            deleted = cur.rowcount
    return f"№{note_id} qayd o'chirildi." if deleted else f"№{note_id} qayd topilmadi."


# --- Admin funksiyalar ---

def db_track_user(user_id: int, username: str | None, full_name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, full_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    last_seen = NOW(),
                    message_count = users.message_count + 1
            """, (user_id, username, full_name))


def db_admin_stats() -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as cnt FROM users")
            total_users = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM users WHERE last_seen >= NOW() - INTERVAL '24 hours'")
            active_today = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) as cnt FROM users WHERE last_seen >= NOW() - INTERVAL '7 days'")
            active_week = cur.fetchone()["cnt"]
            cur.execute("SELECT SUM(message_count) as cnt FROM users")
            total_msgs = cur.fetchone()["cnt"] or 0
            cur.execute("SELECT COUNT(*) as cnt FROM reminders WHERE sent = 0")
            active_reminders = cur.fetchone()["cnt"]
    return (
        f"📊 Bot statistikasi:\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"🟢 Bugun faol: {active_today}\n"
        f"📅 Hafta faol: {active_week}\n"
        f"💬 Jami xabarlar: {total_msgs}\n"
        f"⏰ Faol eslatmalar: {active_reminders}"
    )


def db_admin_users(limit: int = 10) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, username, full_name, message_count, last_seen
                FROM users ORDER BY last_seen DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    if not rows:
        return "Foydalanuvchilar yo'q."
    lines = ["👥 Oxirgi foydalanuvchilar:\n"]
    for r in rows:
        name = f"@{r['username']}" if r['username'] else r['full_name']
        last = str(r['last_seen'])[:16]
        lines.append(f"• {name} — {r['message_count']} xabar ({last})")
    return "\n".join(lines)


def db_get_all_user_ids() -> list[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE approved = TRUE")
            return [r["user_id"] for r in cur.fetchall()]


def db_is_approved(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT approved FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return bool(row and row["approved"])


def db_approve_user(user_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET approved = TRUE WHERE user_id = %s", (user_id,))
            return cur.rowcount > 0


def db_revoke_user(user_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET approved = FALSE WHERE user_id = %s", (user_id,))
            return cur.rowcount > 0


def db_pending_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, username, full_name, first_seen
                FROM users WHERE approved = FALSE ORDER BY first_seen DESC LIMIT 20
            """)
            return cur.fetchall()


# --- Profil ---

def db_get_profile(user_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM profiles WHERE user_id = %s", (user_id,))
            return cur.fetchone()


def db_save_profile(user_id: int, name: str, profession: str, interests: str, goals: str, language: str = "uz"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO profiles (user_id, name, profession, interests, goals, language, onboarded)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    profession = EXCLUDED.profession,
                    interests = EXCLUDED.interests,
                    goals = EXCLUDED.goals,
                    language = EXCLUDED.language,
                    onboarded = TRUE
            """, (user_id, name, profession, interests, goals, language))


def db_is_onboarded(user_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT onboarded FROM profiles WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return bool(row and row["onboarded"])


def db_add_memory(user_id: int, summary: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO long_memory (user_id, summary) VALUES (%s, %s)",
                (user_id, summary)
            )
            # Faqat oxirgi 10 ta xotirani saqlaymiz
            cur.execute("""
                DELETE FROM long_memory WHERE user_id = %s AND id NOT IN (
                    SELECT id FROM long_memory WHERE user_id = %s ORDER BY id DESC LIMIT 10
                )
            """, (user_id, user_id))


def db_get_memory(user_id: int) -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT summary FROM long_memory WHERE user_id = %s ORDER BY id DESC LIMIT 10",
                (user_id,)
            )
            rows = cur.fetchall()
    return "\n".join(r["summary"] for r in reversed(rows)) if rows else ""


def db_clear_memory(user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM long_memory WHERE user_id = %s", (user_id,))
            return cur.rowcount


# ============================================================
# YOUTUBE VIDEO TOPISH (to'g'ridan-to'g'ri ijro etish uchun)
# ============================================================

def resolve_youtube_video(query: str) -> str:
    """Google CSE orqali so'ralgan qo'shiq/video uchun aniq YouTube havolasini topadi."""
    if not (GOOGLE_CSE_KEY and GOOGLE_CSE_ID):
        return ""
    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": GOOGLE_CSE_KEY, "cx": GOOGLE_CSE_ID,
                "q": f"{query} youtube",
                "num": 5,
            },
            timeout=10,
        )
        items = resp.json().get("items", [])
        for it in items:
            link = it.get("link", "")
            if "youtube.com/watch" in link or "youtu.be/" in link:
                return link
    except Exception:
        logger.exception("YouTube video qidirishda xato")
    return ""


# ============================================================
# INTERNET QIDIRUV
# ============================================================

def do_web_search(query: str) -> str:
    # 1-urinish: Google Custom Search API
    if GOOGLE_CSE_KEY and GOOGLE_CSE_ID:
        try:
            resp = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": GOOGLE_CSE_KEY,
                    "cx": GOOGLE_CSE_ID,
                    "q": query,
                    "num": 6,
                },
                timeout=12,
            )
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return _gemini_search(query)

            snippets = "\n\n".join(
                f"[{i+1}] {it.get('title','')}\n{it.get('snippet','')}\nManba: {it.get('link','')}"
                for i, it in enumerate(items)
            )
            now = now_local().strftime("%Y-%m-%d")
            prompt = (
                f"Bugungi sana: {now}. Quyidagi internet qidiruv natijalari asosida "
                f"'{query}' savoliga aniq, qisqa javob ber (o'zbek tilida).\n\n"
                f"{snippets}\n\n"
                f"MUHIM: Faqat natijalardagi ma'lumotga tayan. Agar natijalarda javob bo'lmasa, "
                f"'Bu haqda aniq ma'lumot topilmadi' deb ayt. O'zingdan to'qib chiqarma."
            )
            resp2 = client.models.generate_content(model=MODEL, contents=prompt)
            answer = (resp2.text or "").strip()
            return answer or snippets
        except Exception:
            logger.exception("Google CSE xato")
            return _gemini_search(query)

    return _gemini_search(query)


def _gemini_search(query: str) -> str:
    """Fallback: Gemini grounding bilan qidirish."""
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=f"Quyidagi savolga internetdagi eng yangi ma'lumotlar asosida qisqa va aniq javob ber: {query}",
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        if not response.candidates:
            return "Qidiruv natijasi topilmadi."
        try:
            return (response.text or "").strip() or "Ma'lumot topilmadi."
        except Exception:
            parts = response.candidates[0].content.parts if response.candidates[0].content else []
            return " ".join(p.text for p in parts if p.text).strip() or "Ma'lumot topilmadi."
    except Exception as e:
        logger.exception("Gemini search xato")
        return f"Qidiruvda xatolik: {e}"


# ============================================================
# HAVOLA O'QISH (URL kontentini olish)
# ============================================================

import re as _re

_SOCIAL_DOMAINS = ("instagram.com", "tiktok.com", "t.me", "facebook.com", "fb.com", "twitter.com", "x.com")

def do_fetch_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Ijtimoiy tarmoqlar login talab qiladi — qidiruvga yo'naltiramiz
    if any(d in url.lower() for d in _SOCIAL_DOMAINS):
        return (
            "Bu ijtimoiy tarmoq sahifasi (Instagram/TikTok/Telegram va h.k.) — "
            "ular login talab qilgani uchun to'g'ridan-to'g'ri o'qib bo'lmaydi. "
            "Iltimos profil/akkaunt nomini (username) yoki mavzuni ayting, men web_search bilan qidiraman."
        )
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "uz,ru;q=0.9,en;q=0.8",
            },
            timeout=12,
            allow_redirects=True,
        )
        if resp.status_code == 403 or resp.status_code == 401:
            return "Bu sayt avtomatik o'qishni bloklagan (403). Mazmunini o'zingiz qisqacha aytsangiz tahlil qilaman."
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return f"Bu havola matn sahifa emas ({ctype}). Tahlil qila olmadim."

        html = resp.text
        html = _re.sub(r"<script[\s\S]*?</script>", " ", html, flags=_re.I)
        html = _re.sub(r"<style[\s\S]*?</style>", " ", html, flags=_re.I)
        title_m = _re.search(r"<title[^>]*>(.*?)</title>", html, flags=_re.I | _re.S)
        title = title_m.group(1).strip() if title_m else ""
        text = _re.sub(r"<[^>]+>", " ", html)
        text = _re.sub(r"\s+", " ", text).strip()
        if not text:
            return "Sahifadan matn topilmadi (ehtimol JavaScript bilan yuklanadi). Mazmunini o'zingiz ayting."
        text = text[:6000]
        return f"SAHIFA: {title}\nURL: {url}\n\nMAZMUN:\n{text}"
    except Exception as e:
        logger.exception("URL o'qishda xato")
        return f"Havolani ocholmadim: {e}. Mazmunini o'zingiz qisqacha aytsangiz tahlil qilaman."


# ============================================================
# HUJJAT O'QISH (Word, Excel, matn)
# ============================================================

def extract_docx(data: bytes) -> str:
    import docx
    doc = docx.Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    # jadvallarni ham olamiz
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()


def extract_xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"=== Varaq: {ws.title} ===")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                out.append(" | ".join(cells))
            if len(out) > 500:  # juda katta fayllarni cheklash
                out.append("... (qisqartirildi)")
                break
    return "\n".join(out).strip()


# ============================================================
# RASM YARATISH (Imagen)
# ============================================================

def do_generate_image(prompt: str) -> bytes | None:
    try:
        result = client.models.generate_images(
            model="imagen-3.0-generate-002",
            prompt=prompt,
            config=types.GenerateImagesConfig(number_of_images=1),
        )
        if result.generated_images:
            return result.generated_images[0].image.image_bytes
    except Exception:
        logger.exception("Rasm yaratishda xato")
    return None


# ============================================================
# OVOZ YARATISH (Gemini TTS — tabiiy ovoz, o'zbek)
# ============================================================

import struct

TTS_MODEL = os.getenv("TTS_MODEL", "gemini-2.5-flash-preview-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "Kore")  # tabiiy ayol ovozi


def _pcm_to_wav(pcm: bytes, rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = (
        b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE" +
        b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, bits) +
        b"data" + struct.pack("<I", data_size)
    )
    return header + pcm


def do_tts(text: str) -> bytes | None:
    """Matnni tabiiy ovozga aylantiradi (WAV bytes)."""
    try:
        resp = client.models.generate_content(
            model=TTS_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE)
                    )
                ),
            ),
        )
        if not resp.candidates:
            return None
        for part in resp.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                return _pcm_to_wav(part.inline_data.data)
    except Exception:
        logger.exception("TTS xato")
    return None


# ============================================================
# OB-HAVO (Open-Meteo — bepul, kalitsiz, aniq)
# ============================================================

_WEATHER_CODES = {
    0: "ochiq, quyoshli", 1: "asosan ochiq", 2: "qisman bulutli", 3: "bulutli",
    45: "tumanli", 48: "qirovli tuman", 51: "yengil shivalama", 53: "shivalama",
    55: "kuchli shivalama", 56: "muzli shivalama", 57: "kuchli muzli shivalama",
    61: "yengil yomg'ir", 63: "yomg'ir", 65: "kuchli yomg'ir",
    66: "muzli yomg'ir", 67: "kuchli muzli yomg'ir",
    71: "yengil qor", 73: "qor", 75: "kuchli qor", 77: "qor donalari",
    80: "yengil jala", 81: "jala", 82: "kuchli jala",
    85: "yengil qor jalasi", 86: "kuchli qor jalasi",
    95: "momaqaldiroq", 96: "do'lli momaqaldiroq", 99: "kuchli do'lli momaqaldiroq",
}


def do_get_weather(location: str, when: str = "bugun") -> str:
    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "ru"},
            timeout=10,
        ).json()
        results = geo.get("results")
        if not results:
            return f"'{location}' joyi topilmadi. Shahar nomini aniqroq yozing."
        place = results[0]
        lat, lon = place["latitude"], place["longitude"]
        name = place.get("name", location)
        country = place.get("country", "")

        wx = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                "timezone": "auto", "forecast_days": 3,
            },
            timeout=10,
        ).json()

        cur = wx.get("current", {})
        daily = wx.get("daily", {})
        code = cur.get("weather_code", 0)
        desc = _WEATHER_CODES.get(code, "noma'lum")

        loc_label = f"{name}" + (f", {country}" if country else "")

        if when in ("ertaga", "tomorrow"):
            idx = 1
            day_label = "Ertaga"
        elif when in ("indinga", "after_tomorrow"):
            idx = 2
            day_label = "Indinga"
        else:
            # bugun — joriy ob-havo
            tmax = daily.get("temperature_2m_max", [None])[0]
            tmin = daily.get("temperature_2m_min", [None])[0]
            return (
                f"🌤 {loc_label} — bugun:\n"
                f"Hozir: {cur.get('temperature_2m')}°C (his: {cur.get('apparent_temperature')}°C), {desc}\n"
                f"Kun davomida: {tmin}…{tmax}°C\n"
                f"Namlik: {cur.get('relative_humidity_2m')}%, shamol: {cur.get('wind_speed_10m')} km/soat"
            )

        codes = daily.get("weather_code", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        if len(codes) > idx:
            d_desc = _WEATHER_CODES.get(codes[idx], "noma'lum")
            return (
                f"🌤 {loc_label} — {day_label.lower()}:\n"
                f"Harorat: {tmin[idx]}…{tmax[idx]}°C, {d_desc}"
            )
        return f"{loc_label} uchun {day_label.lower()} prognozi topilmadi."
    except Exception as e:
        logger.exception("Ob-havo xato")
        return f"Ob-havo ma'lumotini olishda xatolik: {e}"


# ============================================================
# KRIPTO NARXI (CoinGecko — bepul, kalitsiz, real-time)
# ============================================================

def do_get_crypto(coin: str) -> str:
    try:
        # Nomdan coin id ni aniqlash
        search = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": coin}, timeout=10,
        ).json()
        coins = search.get("coins", [])
        if not coins:
            return f"'{coin}' kriptovalyutasi topilmadi."
        coin_id = coins[0]["id"]
        symbol = coins[0]["symbol"].upper()
        name = coins[0]["name"]

        price = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=10,
        ).json()
        d = price.get(coin_id, {})
        usd = d.get("usd")
        change = d.get("usd_24h_change", 0)
        if usd is None:
            return f"{name} narxi topilmadi."
        arrow = "📈" if change >= 0 else "📉"
        return (
            f"💰 {name} ({symbol}):\n"
            f"Narx: ${usd:,.2f}\n"
            f"24 soat: {arrow} {change:+.2f}%"
        )
    except Exception as e:
        logger.exception("Kripto narx xato")
        return f"Kripto narxini olishda xatolik: {e}"


# ============================================================
# GEMINI FUNCTION CALLING
# ============================================================

FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="web_search",
        description="Internetdan yangi ma'lumot qidirish (yangiliklar, narxlar, valyuta kursi, mashhur odamlar, faktlar). DIQQAT: ob-havo uchun bu emas, get_weather ishlat!",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"query": types.Schema(type=types.Type.STRING, description="Qidiruv so'rovi. Aniq bo'lsin: agar O'zbekistonga oid bo'lsa 'O'zbekiston' so'zini qo'sh.")},
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_weather",
        description="Ob-havo ma'lumotini olish. 'Ob-havo qanaqa', 'bugun/ertaga havo' kabi savollarda ishlatiladi. Real aniq ma'lumot beradi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "location": types.Schema(type=types.Type.STRING, description="Shahar yoki tuman nomi (masalan: Buxoro, G'ijduvon, Toshkent)"),
                "when": types.Schema(type=types.Type.STRING, enum=["bugun", "ertaga", "indinga"], description="Qaysi kun"),
            },
            required=["location"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_crypto",
        description="Kriptovalyuta narxini real-time olish (Bitcoin, Ethereum, Toncoin va boshqalar). 'Bitcoin narxi qancha' kabi savollarda web_search EMAS, SHUNI ishlat.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "coin": types.Schema(type=types.Type.STRING, description="Kripto nomi (masalan: bitcoin, ethereum, ton, solana)"),
            },
            required=["coin"],
        ),
    ),
    types.FunctionDeclaration(
        name="fetch_url",
        description="Havola (URL/link) mazmunini o'qish va tahlil qilish. Foydalanuvchi link yuborsa yoki 'shu saytni ko'r', 'bu maqolani o'qib ber' desa ishlatiladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "url": types.Schema(type=types.Type.STRING, description="To'liq havola (masalan: https://example.com/page)"),
            },
            required=["url"],
        ),
    ),
    types.FunctionDeclaration(
        name="remember",
        description="Foydalanuvchi haqida MUHIM, uzoq muddat eslab qolish kerak bo'lgan faktni saqlash. Masalan: uning loyihasi, ishi, maqsadi, sevimli narsasi, oilasi, muhim sanalari, qarorlari. Foydalanuvchi o'zi haqida yangi muhim narsa aytsa — DARHOL shuni chaqir.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "fact": types.Schema(type=types.Type.STRING, description="Eslab qolinadigan qisqa fakt (masalan: 'Foydalanuvchi YouTube blog ochmoqchi, mavzu - sayohat')"),
            },
            required=["fact"],
        ),
    ),
    types.FunctionDeclaration(
        name="generate_image",
        description="Rasm/surat yaratish. 'Rasm chiz', 'surat yaratib ber', 'menga ... rasmini chiz' desa ishlatiladi. Prompt ingliz tilida va batafsil bo'lsa sifat yuqori bo'ladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "prompt": types.Schema(type=types.Type.STRING, description="Rasm tavsifi (ingliz tilida, batafsil: uslub, rang, kompozitsiya)"),
            },
            required=["prompt"],
        ),
    ),
    types.FunctionDeclaration(
        name="add_transaction",
        description="Kirim yoki chiqimni bazaga yozish. Pul sarflagani/topgani haqida aytsa yoki chek rasmida summa ko'rinsa ishlatiladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "tx_type": types.Schema(type=types.Type.STRING, enum=["kirim", "chiqim"]),
                "amount": types.Schema(type=types.Type.NUMBER, description="Summa so'mda. '50 ming' = 50000"),
                "category": types.Schema(type=types.Type.STRING, description="oziq-ovqat, transport, kommunal, maosh, savdo, boshqa..."),
                "note": types.Schema(type=types.Type.STRING, description="Qisqa izoh (ixtiyoriy)"),
            },
            required=["tx_type", "amount", "category"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_report",
        description="Kirim-chiqim hisoboti. 'Qancha sarfladim', 'hisobot', 'balans' desa ishlatiladi. Aniq sana oralig'i so'ralsa (masalan '1-iyundan 10-iyungacha', 'shu oyning 5-sanasi') start_date/end_date ber.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "period": types.Schema(type=types.Type.STRING, enum=["bugun", "kecha", "hafta", "oy"], description="Tayyor davr. Aniq sana berilsa bo'sh qoldir."),
                "start_date": types.Schema(type=types.Type.STRING, description="Boshlanish sanasi 'YYYY-MM-DD' (ixtiyoriy)"),
                "end_date": types.Schema(type=types.Type.STRING, description="Tugash sanasi 'YYYY-MM-DD' (ixtiyoriy, berilmasa start_date kuni)"),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="delete_last_transaction",
        description="Oxirgi kirim/chiqim yozuvini o'chirish ('xato yozdim', 'oxirgisini o'chir').",
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    ),
    types.FunctionDeclaration(
        name="set_reminder",
        description="Eslatma o'rnatish. 'Ertaga 9 da ... eslatib qo'y' kabi so'rovlarda. Hozirgi sana-vaqt system promptda berilgan — 'ertaga', 'bir soatdan keyin' kabilarni o'zing aniq vaqtga aylantir.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "text": types.Schema(type=types.Type.STRING, description="Nimani eslatish kerak"),
                "remind_at": types.Schema(type=types.Type.STRING, description="Vaqt 'YYYY-MM-DD HH:MM' formatida"),
            },
            required=["text", "remind_at"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_reminders",
        description="Faol eslatmalar ro'yxatini ko'rsatish.",
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    ),
    types.FunctionDeclaration(
        name="delete_reminder",
        description="Eslatmani raqami (№) bo'yicha o'chirish/bekor qilish.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"reminder_id": types.Schema(type=types.Type.INTEGER, description="Eslatma raqami")},
            required=["reminder_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="add_note",
        description="Qayd saqlash. 'Eslab qol: ...' desa ishlatiladi (parollar, raqamlar, manzillar, fikrlar).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"text": types.Schema(type=types.Type.STRING, description="Saqlanadigan matn")},
            required=["text"],
        ),
    ),
    types.FunctionDeclaration(
        name="find_notes",
        description="Saqlangan qaydlardan qidirish. 'Mashina raqami nima edi?' kabi savollarda. Bo'sh query = oxirgi qaydlar.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"query": types.Schema(type=types.Type.STRING, description="Qidiruv so'zi (masalan: mashina)")},
        ),
    ),
    types.FunctionDeclaration(
        name="delete_note",
        description="Qaydni raqami (№) bo'yicha o'chirish.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"note_id": types.Schema(type=types.Type.INTEGER)},
            required=["note_id"],
        ),
    ),
]

# Faqat Shoxa (Android ilova) orqali ishlaganda yoqiladi — Telegram'da emas,
# chunki bular HAQIQIY telefonni boshqaradi, buni faqat ilova bajara oladi.
DEVICE_ACTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="open_app",
        description="Telefondagi ilovani ochish. 'Telegram och', 'YouTube kir', 'Instagram ochib ber' kabi so'rovlarda ishlatiladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "app_name": types.Schema(
                    type=types.Type.STRING,
                    description="Ilova nomi: telegram, youtube, instagram, whatsapp, chrome, gmail, maps, camera, settings, spotify, tiktok, facebook va h.k.",
                ),
            },
            required=["app_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="set_alarm",
        description="Telefonda budilnik/alarm o'rnatish. 'Ertalab 7 da budilnik qo'y', 'bu vaqtga uyg'otib qo'y' kabi so'rovlarda ishlatiladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "hour": types.Schema(type=types.Type.INTEGER, description="Soat, 0-23 formatida"),
                "minute": types.Schema(type=types.Type.INTEGER, description="Minut, 0-59"),
                "label": types.Schema(type=types.Type.STRING, description="Budilnik nomi (ixtiyoriy)"),
            },
            required=["hour", "minute"],
        ),
    ),
    types.FunctionDeclaration(
        name="make_call",
        description="Qo'ng'iroq qilish. 'Onamga qo'ng'iroq qil', '+998901234567 ga qo'ng'iroq qil' kabi so'rovlarda ishlatiladi. Agar ism aytilsa (raqam emas) — contact_name ber, ilova telefondagi kontaktlardan o'zi qidiradi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "phone_number": types.Schema(type=types.Type.STRING, description="To'liq telefon raqami, agar aytilgan bo'lsa"),
                "contact_name": types.Schema(type=types.Type.STRING, description="Kontakt ismi, agar raqam o'rniga ism aytilgan bo'lsa (masalan: Ona, Ali)"),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="search_in_app",
        description="Ilova ichida biror narsani qidirish/ochish. 'YouTube'da bu qo'shiqni qidir', 'Yandex Mapsda restoran top' kabi 'ilova och VA shu ishni qil' birikma so'rovlarida ishlatiladi. FAQAT YANGI qidiruv so'ralganda chaqir — agar foydalanuvchi 'eshitamiz', 'xa', 'rahmat', 'zo'r' kabi oddiy javob/tasdiq aytsa, BU FUNKSIYANI QAYTA CHAQIRMA, shunchaki tabiiy javob ber.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "app_name": types.Schema(type=types.Type.STRING, description="Ilova nomi: youtube, instagram, chrome, maps va h.k."),
                "query": types.Schema(type=types.Type.STRING, description="Qidiriladigan narsa (qo'shiq nomi, joy, mavzu)"),
            },
            required=["app_name", "query"],
        ),
    ),
    types.FunctionDeclaration(
        name="open_telegram_chat",
        description=(
            "Telegram'da ma'lum bir kishi, kanal yoki guruhni ochish, ixtiyoriy ravishda xabar matnini tayyorlab qo'yish. "
            "Quyidagilarning HAMMASIDA ishlatiladi: 'Telegram'dan <kanal>ga kir', '<kishi> bilan suhbatni och', "
            "'Telegram'da <kimga> yoz: ...', '<kanal>ni och'. "
            "Agar @username aniq aytilmasa, kanal/kishi nomini username sifatida yoz (masalan 'Mavzu' kanali -> 'mavzu'). "
            "Foydalanuvchi baribir Yuborish tugmasini bosishi kerak (xabar avtomatik yuborilmaydi) — buni tabiiy ayt."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "username": types.Schema(type=types.Type.STRING, description="Telegram username (@ belgisiz) yoki kontakt ismi"),
                "message": types.Schema(type=types.Type.STRING, description="Tayyorlanadigan xabar matni (ixtiyoriy)"),
            },
            required=["username"],
        ),
    ),
    types.FunctionDeclaration(
        name="send_sms",
        description="SMS xabar yuborish. 'Bu raqamga SMS yubor: ...' kabi so'rovlarda ishlatiladi. Foydalanuvchi yuborishni xabar ilovasida tasdiqlashi kerak bo'ladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "phone_number": types.Schema(type=types.Type.STRING, description="Qabul qiluvchi raqam"),
                "message": types.Schema(type=types.Type.STRING, description="Xabar matni"),
            },
            required=["phone_number", "message"],
        ),
    ),
    types.FunctionDeclaration(
        name="set_volume",
        description="Telefon ovoz balandligini boshqarish. 'Ovozni baland qil', 'tovushni kamaytir', 'ovozni o'chir' kabi so'rovlarda ishlatiladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "direction": types.Schema(type=types.Type.STRING, enum=["up", "down", "mute"], description="Ovoz yo'nalishi"),
            },
            required=["direction"],
        ),
    ),
    types.FunctionDeclaration(
        name="toggle_flashlight",
        description="Telefon fonarini (chiroq) yoqish/o'chirish. 'Fonarni yoq', 'chiroqni o'chir' kabi so'rovlarda ishlatiladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "on": types.Schema(type=types.Type.BOOLEAN, description="True = yoq, False = o'chir"),
            },
            required=["on"],
        ),
    ),
]


def build_system_prompt(user_id: int | None = None) -> str:
    now = now_local()
    weekdays = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]

    profile_section = ""
    memory_section = ""
    if user_id:
        profile = db_get_profile(user_id)
        if profile:
            profile_section = f"""
FOYDALANUVCHI PROFILI:
- Ismi: {profile['name']}
- Kasbi: {profile['profession']}
- Qiziqishlari: {profile['interests']}
- Maqsadlari: {profile['goals']}
- Tili: {profile['language']}
Foydalanuvchiga doim ismi bilan murojaat qil. Kasbiga mos maslahatlar ber."""

        memory = db_get_memory(user_id)
        if memory:
            memory_section = f"\nOLDINGI SUHBATLARDAN XOTIRA:\n{memory}"

    return f"""Sen Buxorolik dasturchi tomonidan yaratilgan shaxsiy AI agentsan. Sirdosh deb ataladigan Telegram botda ishlaysan.
Agar "sen kimsan", "sen nimasan", "kim yaratdi", "qaysi model", "qaysi AI" kabi savollar berilsa — "Men Buxorolik dasturchi tomonidan yaratilgan shaxsiy AI agentman 😊" deb javob ber. Hech qachon Google yoki boshqa kompaniya nomini aytma.
Sen ovozli xabarlarni ham qabul qila olasan va tushunasan. Foydalanuvchi ovoz yuborsa, uni tushunib javob berasan.

HOZIRGI VAQT: {now.strftime('%Y-%m-%d %H:%M')}, {weekdays[now.weekday()]} (Asia/Tashkent).
"Ertaga" = {(now + timedelta(days=1)).strftime('%Y-%m-%d')}. Nisbiy vaqtlarni shu asosda hisobla.

Imkoniyatlaring:
1. Internet qidiruv (web_search) — yangi ma'lumot kerak bo'lsa taxmin qilma, qidir! O'zbekistonga oid odam/joy bo'lsa qidiruvga "O'zbekiston" qo'sh.
2. Ob-havo (get_weather) — ob-havo so'ralsa SHU funksiyani ishlat, web_search EMAS. Joy aytilmasa, profildagi joyni yoki "Toshkent" ni ol.
   Kripto narxi (get_crypto) — Bitcoin/Ethereum kabi narxlar so'ralsa SHU funksiyani ishlat, web_search EMAS (real-time aniq narx).
   Rasm yaratish (generate_image) — "rasm chiz", "surat yaratib ber" desa ishlatiladi. Prompt ni ingliz tilida, batafsil yoz.
   Hujjat (PDF/Word/Excel) — foydalanuvchi fayl yuborsa avtomatik o'qiysan va tahlil qilasan.
3. Buxgalteriya — xarajat/daromad aytilsa add_transaction. Hisobot so'ralsa get_report.
4. Eslatmalar — set_reminder (vaqtni aniq 'YYYY-MM-DD HH:MM' ga aylantir).
5. Qaydlar — "eslab qol" desa add_note, "nima edi?" desa find_notes.
6. Rasmlar — chek/kvitansiya rasmi kelsa, summa va do'konni aniqlab add_transaction chaqir.

TELEFONNI BOSHQARISH (agar shu funksiyalar mavjud bo'lsa — Shoxa ilovasidasan):
- "... och", "... kir", "... ochib ber" + ilova nomi → open_app. HECH IKKILANMA, albatta chaqir, faqat gapirib qo'ymagin.
- Ilova ochish VA shu ilova ichida biror narsa qilish birga aytilsa (masalan "YouTube'da Daler Mansurov qo'shig'ini qidir") → search_in_app (app_name + query), open_app EMAS.
- "Telegram'da <kimga> yoz/xabar yubor" → open_telegram_chat (username + message). Agar shaxs ismi aytilsa lekin @username noma'lum bo'lsa, contact_name sifatida ismni username maydoniga yoz.
- "Telegram'dan <kanal/guruh>ga kir", "<kanal>ni och" → open_telegram_chat (kanal nomini username sifatida ber).
- Qo'ng'iroq: raqam aytilsa phone_number, ism aytilsa (masalan "Onamga qo'ng'iroq qil") contact_name bilan make_call.
- Bularning barchasi HAQIQIY telefonda amalga oshadi — sen faqat signal berasan, natijani "amalga oshirilmoqda" deb tabiiy ayt, "men buni qila olmayman" demagin.
- MUHIM: bir amal (open_app/search_in_app/open_telegram_chat/make_call/set_alarm) bajarilgandan SO'NG, foydalanuvchi "xa", "rahmat", "zo'r", "eshitamiz", "yaxshi" kabi oddiy javob/tasdiq aytsa — HECH QANDAY funksiya chaqirma, faqat tabiiy, qisqa javob ber ("Marhamat!", "Yaxshi tinglang!" va h.k.). Faqat foydalanuvchi YANGI, aniq buyruq bersa qayta funksiya chaqir.

Qoidalar:
- Foydalanuvchi qaysi tilda gapirsa, o'sha tilda javob ber (asosan o'zbek).
- Javoblar qisqa va aniq. Aniq bilmasang "aniq ma'lumot topa olmadim" deb ayt — YOLG'ON to'qima!
- Foydalanuvchi avvalgi xabariga "ha", "yo'q" desa — kontekstni esla, qayta so'rama.
- Summalar: "50 ming" = 50000, "1.5 mln" = 1500000.
- Funksiya natijasini chiroyli, tushunarli qilib yetkaz.

MUHIM — SUHBAT SIFATI:
- Sen HAR SOHADA bilimli aqlli maslahatchisan: blogerlik, biznes, dasturlash, ta'lim, sog'liq, psixologiya, marketing, din, tarix, fan — istalgan mavzuda foydali javob ber.
- HECH QACHON bir xil umumiy javobni takrorlama ("men yordam bera olaman" kabi). Har savolga ANIQ, MAZMUNLI, AMALIY javob ber.
- Biror sohada yordam so'ralsa — umumiy gap urma, KONKRET maslahat, qadam-baqadam reja, aniq misollar ber.
- Foydalanuvchi link/havola yuborsa — fetch_url bilan o'qib, mazmunini tahlil qil. Agar fetch_url ishlamasa (ijtimoiy tarmoq/bloklangan), username yoki mavzuni web_search bilan qidirib top.
- Faqat zarur bo'lganda savol ber. Yetarli ma'lumot bo'lsa — darrov foydali javob ber.
- Sen oddiy "yordamchi" emas, haqiqiy aqlli SIRDOSHsan — chuqur, foydali, inson kabi muloqot qil.

XOTIRA VA G'OYA (eng muhim!):
- Foydalanuvchi o'zi haqida muhim narsa aytsa (ishi, loyihasi, maqsadi, qiziqishi, muammosi, qarori) — DARHOL "remember" funksiyasini chaqirib eslab qol. Keyingi suhbatlarda shuni hisobga ol.
- Foydalanuvchining ishini, loyihasini o'rgan va PROAKTIV ravishda foydali G'OYALAR, takliflar ber — so'ramasa ham. Masalan blogger bo'lsa kontent g'oyalari, tadbirkor bo'lsa biznes takliflari.
- Avvalgi suhbatlardagi xotirani eslab, "o'tgan safar aytgan loyihangiz qanday ketyapti?" kabi tabiiy, g'amxo'r muloqot qil.
- Sen egangning eng yaqin sirdoshi, maslahatchisi va ilhomchisisan.

FOYDALANUVCHINI TINGLA (juda muhim!):
- Foydalanuvchi biror narsani "kerakmas", "boshqasini ayt", "bu menga to'g'ri kelmaydi" desa — DARHOL o'sha mavzuni TASHLA va boshqa, YANGI yo'nalishda fikr ber. Eski mavzuni qayta-qayta takrorlama!
- Uning xohish va e'tirozlarini hurmat qil. Agar bir taklif yoqmasa, butunlay boshqacha variant taklif qil.
- O'zingdan bitta mavzuga yopishib olma — foydalanuvchi nima xohlayotganini diqqat bilan tingla va shunga moslash.
{profile_section}{memory_section}
"""


def execute_function(user_id: int, name: str, args: dict) -> str:
    try:
        if name == "web_search":
            return do_web_search(args.get("query", ""))
        if name == "get_weather":
            return do_get_weather(args.get("location", "Toshkent"), args.get("when", "bugun"))
        if name == "get_crypto":
            return do_get_crypto(args.get("coin", "bitcoin"))
        if name == "fetch_url":
            return do_fetch_url(args.get("url", ""))
        if name == "remember":
            fact = args.get("fact", "").strip()
            if fact:
                db_add_memory(user_id, fact)
                return "Eslab qoldim ✅"
            return "Eslab qolinadigan narsa yo'q."
        if name == "add_transaction":
            return db_add_transaction(
                user_id, args.get("tx_type", "chiqim"), float(args.get("amount", 0)),
                args.get("category", "boshqa"), args.get("note", ""),
            )
        if name == "get_report":
            return db_get_report(
                user_id, args.get("period", "oy"),
                args.get("start_date", ""), args.get("end_date", ""),
            )
        if name == "delete_last_transaction":
            return db_delete_last(user_id)
        if name == "set_reminder":
            return db_set_reminder(user_id, args.get("text", "Eslatma"), args.get("remind_at", ""))
        if name == "list_reminders":
            return db_list_reminders(user_id)
        if name == "delete_reminder":
            return db_delete_reminder(user_id, int(args.get("reminder_id", 0)))
        if name == "add_note":
            return db_add_note(user_id, args.get("text", ""))
        if name == "find_notes":
            return db_find_notes(user_id, args.get("query", ""))
        if name == "delete_note":
            return db_delete_note(user_id, int(args.get("note_id", 0)))
        return f"Noma'lum funksiya: {name}"
    except Exception as e:
        logger.exception("Funksiyada xato: %s", name)
        return f"Xatolik: {e}"


# ============================================================
# AGENT SIKLI
# ============================================================

MAX_HISTORY = 20
chat_history: dict[int, list[types.Content]] = {}
onboarding_state: dict[int, dict] = {}  # {user_id: {step, name, profession, interests, goals}}


def _save_history(user_id: int, user_parts: list[types.Part], answer: str):
    """Foydalanuvchi xabari va javobni tarixga saqlaydi (kontekst saqlanishi uchun)."""
    history = chat_history.setdefault(user_id, [])
    saved = [p if p.text else types.Part.from_text(text="[media xabar]") for p in user_parts]
    history.append(types.Content(role="user", parts=saved))
    history.append(types.Content(role="model", parts=[types.Part.from_text(text=answer)]))
    if len(history) > MAX_HISTORY * 2:
        chat_history[user_id] = history[-MAX_HISTORY * 2:]


async def ask_agent(
    user_id: int,
    user_parts: list[types.Part],
    image_sink: list | None = None,
    device_action_sink: list | None = None,
) -> str:
    history = chat_history.setdefault(user_id, [])
    contents = history + [types.Content(role="user", parts=user_parts)]

    # device_action_sink berilgan bo'lsa — Shoxa ilovasidan kelgan so'rov,
    # telefonni boshqarish funksiyalarini ham yoqamiz.
    declarations = FUNCTION_DECLARATIONS + (DEVICE_ACTION_DECLARATIONS if device_action_sink is not None else [])

    config = types.GenerateContentConfig(
        system_instruction=build_system_prompt(user_id),
        temperature=0.7,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=[types.Tool(function_declarations=declarations)],
    )

    answer = "Kechirasiz, javob topa olmadim. Boshqacharoq so'rab ko'ring."
    for _ in range(6):
        response = await asyncio.to_thread(
            client.models.generate_content, model=MODEL, contents=contents, config=config,
        )
        if not response.candidates:
            logger.warning("ask_agent: bo'sh candidates. prompt_feedback=%s", getattr(response, "prompt_feedback", None))
            break
        candidate = response.candidates[0]
        parts = (candidate.content.parts or []) if candidate.content else []
        if not parts:
            try:
                txt = (response.text or "").strip()
            except Exception:
                txt = ""
            if txt:
                answer = txt
            else:
                logger.warning(
                    "ask_agent: bo'sh parts. finish_reason=%s safety=%s",
                    getattr(candidate, "finish_reason", None),
                    getattr(candidate, "safety_ratings", None),
                )
            break

        function_calls = [p.function_call for p in parts if p.function_call]
        if not function_calls:
            try:
                txt = (response.text or "").strip()
            except Exception:
                txt = ""
            if txt:
                answer = txt
            else:
                logger.warning(
                    "ask_agent: matn yo'q. finish_reason=%s safety=%s parts=%s",
                    getattr(candidate, "finish_reason", None),
                    getattr(candidate, "safety_ratings", None),
                    parts,
                )
            break

        contents.append(candidate.content)
        result_parts = []
        for fc in function_calls:
            args = dict(fc.args or {})
            if fc.name == "generate_image":
                prompt = args.get("prompt", "")
                img = await asyncio.to_thread(do_generate_image, prompt)
                if img is not None and image_sink is not None:
                    image_sink.append((prompt, img))
                    result = "Rasm muvaffaqiyatli yaratildi va foydalanuvchiga yuborildi."
                else:
                    result = "Rasm yaratib bo'lmadi (xizmat vaqtincha ishlamayapti)."
            elif fc.name == "open_app" and device_action_sink is not None:
                app_name = args.get("app_name", "")
                device_action_sink.append({"type": "open_app", "app_name": app_name})
                result = f"'{app_name}' ilovasi ochilmoqda."
            elif fc.name == "set_alarm" and device_action_sink is not None:
                hour, minute = args.get("hour", 0), args.get("minute", 0)
                device_action_sink.append({
                    "type": "set_alarm", "hour": hour, "minute": minute,
                    "label": args.get("label", ""),
                })
                result = f"Budilnik {hour:02d}:{minute:02d} ga o'rnatilmoqda."
            elif fc.name == "make_call" and device_action_sink is not None:
                phone = args.get("phone_number", "")
                contact = args.get("contact_name", "")
                device_action_sink.append({"type": "make_call", "phone_number": phone, "contact_name": contact})
                result = f"{contact or phone} ga qo'ng'iroq qilinmoqda."
            elif fc.name == "search_in_app" and device_action_sink is not None:
                app_name = args.get("app_name", "")
                query = args.get("query", "")
                video_url = ""
                if "youtube" in app_name.lower():
                    video_url = await asyncio.to_thread(resolve_youtube_video, query)
                device_action_sink.append({
                    "type": "search_in_app", "app_name": app_name, "query": query, "video_url": video_url,
                })
                if video_url:
                    result = f"'{query}' YouTube'da topildi va ijro etilmoqda."
                else:
                    result = f"{app_name} ilovasida '{query}' qidirilmoqda."
            elif fc.name == "open_telegram_chat" and device_action_sink is not None:
                username = args.get("username", "")
                msg = args.get("message", "")
                device_action_sink.append({"type": "open_telegram_chat", "username": username, "message": msg})
                result = f"Telegram'da {username} bilan suhbat ochilmoqda."
            elif fc.name == "send_sms" and device_action_sink is not None:
                phone = args.get("phone_number", "")
                msg = args.get("message", "")
                device_action_sink.append({"type": "send_sms", "phone_number": phone, "message": msg})
                result = f"{phone} raqamiga SMS tayyorlanmoqda."
            elif fc.name == "set_volume" and device_action_sink is not None:
                direction = args.get("direction", "up")
                device_action_sink.append({"type": "set_volume", "direction": direction})
                result = "Ovoz sozlanmoqda."
            elif fc.name == "toggle_flashlight" and device_action_sink is not None:
                on = bool(args.get("on", True))
                device_action_sink.append({"type": "toggle_flashlight", "on": on})
                result = "Fonar yoqilmoqda." if on else "Fonar o'chirilmoqda."
            else:
                result = await asyncio.to_thread(execute_function, user_id, fc.name, args)
            result_parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
        contents.append(types.Content(role="user", parts=result_parts))

    # Har qanday holatda ham kontekstni saqlaymiz
    _save_history(user_id, user_parts, answer)
    return answer


async def agent_respond(message: Message, uid: int, parts: list[types.Part]):
    """Agentdan javob olib, rasm bo'lsa rasmni, matnni yuboradi."""
    images: list = []
    answer = await ask_agent(uid, parts, images)
    for cap, img in images:
        try:
            await message.answer_photo(
                BufferedInputFile(img, "rasm.png"),
                caption=(cap[:1000] if cap else None),
            )
        except Exception:
            logger.exception("Rasm yuborishda xato")
    if answer:
        await send_long(message, answer)


# ============================================================
# TELEGRAM HANDLERLAR
# ============================================================

router = Router()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats"),
         InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin_users")],
        [InlineKeyboardButton(text="⏳ Kutayotganlar", callback_data="admin_pending"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔴 Botni o'chir" if bot_enabled else "🟢 Botni yoq", callback_data="admin_toggle")],
    ])


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 Admin panel:", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin_"))
async def admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo'q!")
        return

    global bot_enabled
    action = callback.data

    if action == "admin_stats":
        text = await asyncio.to_thread(db_admin_stats)
        await callback.message.edit_text(text, reply_markup=admin_keyboard())

    elif action == "admin_users":
        text = await asyncio.to_thread(db_admin_users)
        await callback.message.edit_text(text, reply_markup=admin_keyboard())

    elif action == "admin_pending":
        rows = await asyncio.to_thread(db_pending_users)
        if not rows:
            await callback.message.edit_text("Kutayotgan foydalanuvchilar yo'q.", reply_markup=admin_keyboard())
        else:
            buttons = []
            for r in rows:
                name = f"@{r['username']}" if r['username'] else r['full_name']
                buttons.append([
                    InlineKeyboardButton(text=f"✅ {name}", callback_data=f"approve_{r['user_id']}"),
                    InlineKeyboardButton(text="❌", callback_data=f"revoke_{r['user_id']}"),
                ])
            buttons.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_back")])
            await callback.message.edit_text(
                "⏳ Ruxsat kutayotganlar:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )

    elif action == "admin_toggle":
        bot_enabled = not bot_enabled
        status = "🟢 Bot yoqildi!" if bot_enabled else "🔴 Bot o'chirildi!"
        await callback.message.edit_text(f"Admin panel:\n{status}", reply_markup=admin_keyboard())

    elif action == "admin_broadcast":
        await callback.message.edit_text(
            "📢 Broadcast xabar yuboring:\n/broadcast <xabar matni>",
            reply_markup=admin_keyboard()
        )

    elif action == "admin_back":
        await callback.message.edit_text("🛠 Admin panel:", reply_markup=admin_keyboard())

    elif action.startswith("approve_"):
        uid = int(action.split("_")[1])
        ok = await asyncio.to_thread(db_approve_user, uid)
        if ok:
            try:
                await start_onboarding(callback.bot, uid)
            except Exception:
                pass
        await callback.answer("✅ Ruxsat berildi!" if ok else "Foydalanuvchi topilmadi")
        rows = await asyncio.to_thread(db_pending_users)
        if not rows:
            await callback.message.edit_text("Kutayotgan foydalanuvchilar yo'q.", reply_markup=admin_keyboard())
        else:
            buttons = []
            for r in rows:
                name = f"@{r['username']}" if r['username'] else r['full_name']
                buttons.append([
                    InlineKeyboardButton(text=f"✅ {name}", callback_data=f"approve_{r['user_id']}"),
                    InlineKeyboardButton(text="❌", callback_data=f"revoke_{r['user_id']}"),
                ])
            buttons.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_back")])
            await callback.message.edit_text("⏳ Ruxsat kutayotganlar:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return

    elif action.startswith("revoke_"):
        uid = int(action.split("_")[1])
        await asyncio.to_thread(db_revoke_user, uid)
        await callback.answer("❌ Rad etildi")

    await callback.answer()


async def start_onboarding(bot: Bot, user_id: int):
    onboarding_state[user_id] = {"step": "name"}
    await bot.send_message(
        user_id,
        "✅ Botdan foydalanishga ruxsat berildi!\n\n"
        "Salom! 👋 Men sizning shaxsiy *Sirdosh AI* agentingizman.\n\n"
        "Keling, siz bilan tanishamiz — men sizni yaxshiroq bilsam, ko'proq yordam bera olaman! 😊\n\n"
        "Avvalo, *ismingiz* nima?",
        parse_mode="Markdown"
    )


@router.message(Command("approve"))
async def cmd_approve(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Foydalanish: /approve <user_id>")
        return
    uid = int(parts[1])
    ok = await asyncio.to_thread(db_approve_user, uid)
    if ok:
        try:
            await start_onboarding(bot, uid)
        except Exception:
            pass
        await message.answer(f"✅ {uid} ruxsat berildi.")
    else:
        await message.answer("Foydalanuvchi topilmadi.")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Foydalanish: /revoke <user_id>")
        return
    uid = int(parts[1])
    await asyncio.to_thread(db_revoke_user, uid)
    await message.answer(f"❌ {uid} ruxsati olib tashlandi.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.removeprefix("/broadcast").strip()
    if not text:
        await message.answer("Foydalanish: /broadcast <xabar matni>")
        return
    user_ids = await asyncio.to_thread(db_get_all_user_ids)
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"Broadcast tugadi: {sent} ta yuborildi, {failed} ta xato.")


@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    await asyncio.to_thread(db_track_user, uid, message.from_user.username, message.from_user.full_name)

    if not await asyncio.to_thread(db_is_approved, uid):
        # Adminga xabar yuboramiz
        if ADMIN_ID and BOT:
            name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
            try:
                await BOT.send_message(
                    ADMIN_ID,
                    f"🔔 Yangi foydalanuvchi botdan foydalanmoqchi:\n"
                    f"👤 {name} (ID: {uid})\n\n"
                    f"Ruxsat berish uchun: /approve {uid}\nRad etish: /revoke {uid}"
                )
            except Exception:
                pass
        await message.answer(
            "Salom! 👋\n\n"
            "Botdan foydalanish uchun admin ruxsati kerak.\n"
            "So'rovingiz adminga yuborildi — tez orada javob olasiz! ⏳"
        )
        return

    await message.answer(
        "Salom! 👋 Men sizning shaxsiy yordamchingizman.\n\n"
        "🔎 Internetdan ma'lumot — \"Dollar kursi qancha?\"\n"
        "💰 Buxgalter — \"50 ming taksiga ketdi\" yoki chek RASMINI yuboring\n"
        "📊 Hisobot — \"Bu oy qancha sarfladim?\"\n"
        "⏰ Eslatma — \"Ertaga 9 da dorini eslatib qo'y\"\n"
        "📝 Qayd — \"Eslab qol: mashina raqami 01A777BB\"\n"
        "🎤 Hammasi golosda ham ishlaydi!\n\n"
        "Komandalar: /hisobot, /eslatmalar, /clear, /forget"
    )


@router.message(Command("hisobot"))
async def cmd_report(message: Message):
    await message.answer(await asyncio.to_thread(db_get_report, message.from_user.id, "oy"))


@router.message(Command("eslatmalar"))
async def cmd_reminders(message: Message):
    await message.answer(await asyncio.to_thread(db_list_reminders, message.from_user.id))


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    chat_history.pop(message.from_user.id, None)
    await message.answer("Suhbat tarixi tozalandi ✅")


@router.message(Command("forget"))
async def cmd_forget(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Ha, xotirani o'chir", callback_data="forget_yes"),
        InlineKeyboardButton(text="Yo'q", callback_data="forget_no"),
    ]])
    await message.answer(
        "⚠️ Bu men siz haqingizda eslab qolgan barcha narsalarni o'chiradi "
        "(loyihalaringiz, maqsadlaringiz...). Profil ma'lumotlari saqlanadi.\n\nRostdan o'chiraymi?",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("forget_"))
async def forget_callback(callback: CallbackQuery):
    if callback.data == "forget_yes":
        chat_history.pop(callback.from_user.id, None)
        n = await asyncio.to_thread(db_clear_memory, callback.from_user.id)
        await callback.message.edit_text(f"Xotira tozalandi ✅ ({n} ta yozuv o'chirildi).")
    else:
        await callback.message.edit_text("Bekor qilindi. Hech narsa o'chirilmadi 👍")
    await callback.answer()


_TRANSCRIBE_PROMPT = (
    "TRANSKRIPSIYA VAZIFASI. Sen tarjimon emassan, faqat transkriptchisan.\n"
    "Quyidagi audioda inson nima gapirgan bo'lsa, FAQAT o'sha so'zlarni, xuddi shu tilda yoz.\n"
    "QATIY TAQIQ: bu ko'rsatmani, izoh, sarlavha, tirnoq belgisi yoki boshqa hech qanday qo'shimcha matn yozma.\n"
    "Agar audio bo'sh/tushunarsiz bo'lsa — bo'sh javob qaytar."
)


def _clean_transcript(text: str) -> str:
    """Model ba'zan o'z ko'rsatmasini ham qaytarib yuborishi mumkin — shularni tozalaymiz."""
    leak_markers = ("TRANSKRIPSIYA VAZIFASI", "Bu audio xabarni tinglaysan", "QATIY TAQIQ", "so'zma-so'z")
    if any(m.lower() in text.lower() for m in leak_markers):
        # Ko'rsatma sızib chiqqan — eng oxirgi qatorni yoki bo'sh qaytaramiz (ishonchsiz natija)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        lines = [l for l in lines if not any(m.lower() in l.lower() for m in leak_markers)]
        return lines[-1] if lines else ""
    return text.strip()


async def transcribe_audio(data: bytes, mime: str) -> str:
    """Gemini orqali audio ni matnga o'giradi."""
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL,
        contents=[
            types.Part.from_text(text=_TRANSCRIBE_PROMPT),
            types.Part.from_bytes(data=data, mime_type=mime),
        ],
        config=types.GenerateContentConfig(temperature=0.0),
    )
    if not response.candidates:
        return ""
    try:
        raw = (response.text or "").strip()
    except Exception:
        parts = (response.candidates[0].content.parts or []) if response.candidates[0].content else []
        raw = " ".join(p.text for p in parts if p.text).strip()
    return _clean_transcript(raw)


@router.message(F.voice | F.audio)
async def handle_voice(message: Message, bot: Bot):
    uid = message.from_user.id
    if not bot_enabled and uid != ADMIN_ID:
        await message.answer("Bot vaqtincha o'chirilgan. Tez orada qaytamiz!")
        return
    if not await asyncio.to_thread(db_is_approved, uid):
        await message.answer("Botdan foydalanish uchun admin ruxsati kerak. /start bosing.")
        return
    await asyncio.to_thread(db_track_user, uid, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        audio = message.voice or message.audio
        if audio.file_size and audio.file_size > 20 * 1024 * 1024:
            await message.answer("Audio juda katta (20 MB dan oshmasin).")
            return
        file = await bot.get_file(audio.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        mime = "audio/ogg" if message.voice else (audio.mime_type or "audio/mpeg")

        # Avval ovozni matnga o'giramiz
        text = await transcribe_audio(buf.getvalue(), mime)
        if not text:
            await message.answer("Ovozni tushunib bo'lmadi 😕 Iltimos qaytadan yuboring.")
            return

        # Keyin matn sifatida agentga yuboramiz
        await agent_respond(message, message.from_user.id, [types.Part.from_text(text=text)])
    except Exception:
        logger.exception("Golosli xabarda xato")
        await message.answer("Xatolik yuz berdi 😕 Qaytadan urinib ko'ring.")


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    uid = message.from_user.id
    if not bot_enabled and uid != ADMIN_ID:
        await message.answer("Bot vaqtincha o'chirilgan. Tez orada qaytamiz!")
        return
    if not await asyncio.to_thread(db_is_approved, uid):
        await message.answer("Botdan foydalanish uchun admin ruxsati kerak. /start bosing.")
        return
    await asyncio.to_thread(db_track_user, uid, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        photo = message.photo[-1]  # eng katta o'lcham
        file = await bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)

        caption = message.caption or ""
        instruction = (
            "Bu rasmni tahlil qil. Agar chek/kvitansiya/to'lov rasmi bo'lsa — "
            "summani va do'kon/xizmat nomini aniqlab add_transaction funksiyasini chaqir, "
            "keyin nimani yozganingni ayt. Boshqa rasm bo'lsa, shunchaki tushuntir."
        )
        if caption:
            instruction += f"\nFoydalanuvchi izohi: {caption}"

        parts = [
            types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
            types.Part.from_text(text=instruction),
        ]
        await agent_respond(message, message.from_user.id, parts)
    except Exception:
        logger.exception("Rasmda xato")
        await message.answer("Rasmni o'qishda xatolik 😕 Qaytadan urinib ko'ring.")


@router.message(F.document)
async def handle_document(message: Message, bot: Bot):
    uid = message.from_user.id
    if not bot_enabled and uid != ADMIN_ID:
        await message.answer("Bot vaqtincha o'chirilgan. Tez orada qaytamiz!")
        return
    if not await asyncio.to_thread(db_is_approved, uid):
        await message.answer("Botdan foydalanish uchun admin ruxsati kerak. /start bosing.")
        return
    await asyncio.to_thread(db_track_user, uid, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    doc = message.document
    fname = (doc.file_name or "fayl").lower()
    caption = message.caption or ""

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer("Fayl juda katta (20 MB dan oshmasin).")
        return

    try:
        file = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        data = buf.getvalue()

        instruction = caption or "Bu hujjatni tahlil qil, asosiy mazmunini va muhim nuqtalarini tushuntir."

        # PDF — Gemini to'g'ridan-to'g'ri o'qiydi
        if fname.endswith(".pdf") or doc.mime_type == "application/pdf":
            parts = [
                types.Part.from_bytes(data=data, mime_type="application/pdf"),
                types.Part.from_text(text=instruction),
            ]
            await agent_respond(message, uid, parts)
            return

        # Word / Excel / matn — matnni ajratamiz
        if fname.endswith(".docx"):
            text = await asyncio.to_thread(extract_docx, data)
        elif fname.endswith((".xlsx", ".xlsm")):
            text = await asyncio.to_thread(extract_xlsx, data)
        elif fname.endswith((".txt", ".csv", ".md", ".json")):
            text = data.decode("utf-8", errors="ignore")
        elif fname.endswith(".doc"):
            await message.answer("Eski .doc formati qo'llab-quvvatlanmaydi. Iltimos .docx ga aylantiring.")
            return
        else:
            await message.answer("Bu fayl turini o'qiy olmadim. PDF, Word (.docx), Excel (.xlsx) yoki matn yuboring.")
            return

        if not text.strip():
            await message.answer("Hujjatdan matn topilmadi (bo'sh yoki rasm ko'rinishida).")
            return

        text = text[:30000]  # juda katta hujjatlarni cheklash
        prompt = f"{instruction}\n\n=== HUJJAT MAZMUNI ({doc.file_name}) ===\n{text}"
        await agent_respond(message, uid, [types.Part.from_text(text=prompt)])
    except Exception:
        logger.exception("Hujjatda xato")
        await message.answer("Hujjatni o'qishda xatolik 😕 Qaytadan urinib ko'ring.")


@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    uid = message.from_user.id

    # Onboarding jarayoni
    if uid in onboarding_state:
        state = onboarding_state[uid]
        text = message.text.strip()
        step = state["step"]

        if step == "name":
            state["name"] = text
            state["step"] = "profession"
            await message.answer(
                f"Juda yaxshi, *{text}*! 😊\n\n"
                "Siz qanday soha bilan shug'ullanasiz?\n"
                "_(Masalan: dasturchi, tadbirkor, talaba, shifokor...)_",
                parse_mode="Markdown"
            )

        elif step == "profession":
            state["profession"] = text
            state["step"] = "interests"
            await message.answer(
                "Zo'r! 💪\n\n"
                "Qiziqishlaringiz nima?\n"
                "_(Masalan: texnologiya, biznes, sport, musiqa, sayohat...)_",
                parse_mode="Markdown"
            )

        elif step == "interests":
            state["interests"] = text
            state["step"] = "goals"
            await message.answer(
                "Ajoyib! 🌟\n\n"
                "Men sizga eng ko'p qaysi sohada yordam bera olaman?\n"
                "_(Masalan: ish, o'qish, moliyaviy hisob, eslatmalar, ma'lumot qidirish...)_",
                parse_mode="Markdown"
            )

        elif step == "goals":
            state["goals"] = text
            await asyncio.to_thread(
                db_save_profile, uid,
                state["name"], state["profession"],
                state["interests"], state["goals"]
            )
            del onboarding_state[uid]
            await message.answer(
                f"Tanishganimizdan xursandman, *{state['name']}*! 🎉\n\n"
                f"Men endi siz haqingizda ko'proq bilaman va sizga samarali yordam bera olaman.\n\n"
                f"📌 Sizning profilingiz:\n"
                f"👤 Ism: {state['name']}\n"
                f"💼 Kasb: {state['profession']}\n"
                f"🎯 Qiziqishlar: {state['interests']}\n"
                f"🚀 Maqsadlar: {state['goals']}\n\n"
                f"Endi menga istalgan savolni bering — men doim yordamga tayyorman! 😊\n\n"
                f"Komandalar: /hisobot, /eslatmalar, /clear, /forget",
                parse_mode="Markdown"
            )
        return

    if not bot_enabled and uid != ADMIN_ID:
        await message.answer("Bot vaqtincha o'chirilgan. Tez orada qaytamiz!")
        return
    if not await asyncio.to_thread(db_is_approved, uid):
        await message.answer("Botdan foydalanish uchun admin ruxsati kerak. /start bosing.")
        return
    await asyncio.to_thread(db_track_user, uid, message.from_user.username, message.from_user.full_name)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        await agent_respond(message, message.from_user.id, [types.Part.from_text(text=message.text)])
    except Exception:
        logger.exception("Matnli xabarda xato")
        await message.answer("Xatolik yuz berdi 😕 Qaytadan urinib ko'ring.")


async def send_long(message: Message, text: str):
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


async def main():
    global BOT
    db_init()
    BOT = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    scheduler.start()
    restore_reminders()
    logger.info("Shaxsiy yordamchi (v2) ishga tushdi...")
    await dp.start_polling(BOT)


if __name__ == "__main__":
    asyncio.run(main())
