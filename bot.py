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

import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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


def db_get_report(user_id: int, period: str) -> str:
    now = now_local().replace(tzinfo=None)
    if period == "bugun":
        start = now.replace(hour=0, minute=0, second=0)
    elif period == "hafta":
        start = now - timedelta(days=7)
    else:
        start = now - timedelta(days=30)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT type, category, SUM(amount), COUNT(*)
                   FROM transactions
                   WHERE user_id = %s AND created_at >= %s
                   GROUP BY type, category
                   ORDER BY type, SUM(amount) DESC""",
                (user_id, start),
            )
            rows = cur.fetchall()

    if not rows:
        return f"Bu davr ({period}) uchun yozuvlar topilmadi."

    kirim_total, chiqim_total = 0.0, 0.0
    lines = [f"Hisobot ({period}):"]
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


# ============================================================
# INTERNET QIDIRUV
# ============================================================

def do_web_search(query: str) -> str:
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
            text = (response.text or "").strip()
        except Exception:
            # grounding metadata bor, text yo'q — parts dan qidiramiz
            parts = response.candidates[0].content.parts if response.candidates[0].content else []
            text = " ".join(p.text for p in parts if p.text).strip()
        return text or "Ma'lumot topilmadi."
    except Exception as e:
        logger.exception("Qidiruvda xato")
        return f"Qidiruvda xatolik: {e}"


# ============================================================
# GEMINI FUNCTION CALLING
# ============================================================

FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="web_search",
        description="Internetdan yangi va aniq ma'lumot qidirish (kurslar, yangiliklar, narxlar, ob-havo, faktlar).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"query": types.Schema(type=types.Type.STRING, description="Qidiruv so'rovi")},
            required=["query"],
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
        description="Kirim-chiqim hisoboti. 'Qancha sarfladim', 'hisobot', 'balans' desa ishlatiladi.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"period": types.Schema(type=types.Type.STRING, enum=["bugun", "hafta", "oy"])},
            required=["period"],
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
1. Internet qidiruv (web_search) — yangi ma'lumot kerak bo'lsa taxmin qilma, qidir!
2. Buxgalteriya — xarajat/daromad aytilsa add_transaction. Hisobot so'ralsa get_report.
3. Eslatmalar — set_reminder (vaqtni aniq 'YYYY-MM-DD HH:MM' ga aylantir).
4. Qaydlar — "eslab qol" desa add_note, "nima edi?" desa find_notes.
5. Rasmlar — chek/kvitansiya rasmi kelsa, summa va do'konni aniqlab add_transaction chaqir va nimani yozganingni ayt.

Qoidalar:
- Foydalanuvchi qaysi tilda gapirsa, o'sha tilda javob ber (asosan o'zbek).
- Javoblar qisqa va aniq.
- Summalar: "50 ming" = 50000, "1.5 mln" = 1500000.
- Funksiya natijasini chiroyli, tushunarli qilib yetkaz.
{profile_section}{memory_section}
"""


def execute_function(user_id: int, name: str, args: dict) -> str:
    try:
        if name == "web_search":
            return do_web_search(args.get("query", ""))
        if name == "add_transaction":
            return db_add_transaction(
                user_id, args.get("tx_type", "chiqim"), float(args.get("amount", 0)),
                args.get("category", "boshqa"), args.get("note", ""),
            )
        if name == "get_report":
            return db_get_report(user_id, args.get("period", "oy"))
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


async def ask_agent(user_id: int, user_parts: list[types.Part]) -> str:
    history = chat_history.setdefault(user_id, [])
    contents = history + [types.Content(role="user", parts=user_parts)]

    config = types.GenerateContentConfig(
        system_instruction=build_system_prompt(user_id),
        temperature=0.7,
        tools=[types.Tool(function_declarations=FUNCTION_DECLARATIONS)],
    )

    for _ in range(5):
        response = await asyncio.to_thread(
            client.models.generate_content, model=MODEL, contents=contents, config=config,
        )
        if not response.candidates:
            return "Kechirasiz, javob topa olmadim (model blok qildi)."
        candidate = response.candidates[0]
        parts = (candidate.content.parts or []) if candidate.content else []
        if not parts:
            try:
                return (response.text or "").strip() or "Kechirasiz, javob topa olmadim."
            except Exception:
                return "Kechirasiz, javob topa olmadim."
        function_calls = [p.function_call for p in parts if p.function_call]

        if not function_calls:
            try:
                answer = (response.text or "").strip() or "Kechirasiz, javob topa olmadim."
            except Exception:
                answer = "Kechirasiz, javob topa olmadim."
            saved = [p if p.text else types.Part.from_text(text="[media xabar]") for p in user_parts]
            history.append(types.Content(role="user", parts=saved))
            history.append(types.Content(role="model", parts=[types.Part.from_text(text=answer)]))
            if len(history) > MAX_HISTORY * 2:
                chat_history[user_id] = history[-MAX_HISTORY * 2:]
            return answer

        contents.append(candidate.content)
        result_parts = []
        for fc in function_calls:
            result = await asyncio.to_thread(execute_function, user_id, fc.name, dict(fc.args or {}))
            result_parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
        contents.append(types.Content(role="user", parts=result_parts))

    return "So'rov juda murakkab bo'lib ketdi, soddaroq qilib qaytadan so'rang."


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
        "Komandalar: /hisobot, /eslatmalar, /clear"
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


async def transcribe_audio(data: bytes, mime: str) -> str:
    """Gemini orqali audio ni matnga o'giradi."""
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=data, mime_type=mime),
            types.Part.from_text(text="Bu audio xabarni tinglaysan va so'zma-so'z o'zbek (yoki qanday til bo'lsa) tiliga transkripsiya qilasan. Faqat aytilgan so'zlarni yoz, boshqa hech narsa qo'shma."),
        ],
    )
    if not response.candidates:
        return ""
    try:
        return (response.text or "").strip()
    except Exception:
        parts = (response.candidates[0].content.parts or []) if response.candidates[0].content else []
        return " ".join(p.text for p in parts if p.text).strip()


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
        await send_long(message, await ask_agent(message.from_user.id, [types.Part.from_text(text=text)]))
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
        await send_long(message, await ask_agent(message.from_user.id, parts))
    except Exception:
        logger.exception("Rasmda xato")
        await message.answer("Rasmni o'qishda xatolik 😕 Qaytadan urinib ko'ring.")


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
                f"Komandalar: /hisobot, /eslatmalar, /clear",
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
        await send_long(message, await ask_agent(message.from_user.id, [types.Part.from_text(text=message.text)]))
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
