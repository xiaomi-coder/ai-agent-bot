# SIRDOSH — loyiha qo'llanmasi (Claude Code uchun)

O'zbek tilidagi shaxsiy AI agent. Bitta "miya" ikkita yuzga xizmat qiladi:
**Telegram bot** (aiogram 3, polling) va **Shoxa Android ilovasi** (FastAPI orqali).
Foydalanuvchi bilan muloqot **o'zbekcha**; texnik atamalar inglizcha qolaveradi.

## Tuzilma

| Fayl | Vazifa |
|---|---|
| `bot.py` (~3700 qator) | Butun mantiq: DB, Gemini agent, funksiyalar, handlerlar, rejimlar |
| `api.py` | FastAPI — ilova uchun `/chat`, `/voice`, `/speak` (Bearer `API_SECRET`) |
| `main.py` | Bot + API bitta jarayonda (`python main.py`) |
| `nixpacks.toml` | Railway build'ga `ffmpeg` (TTS → OGG/Opus voice bubble) |
| `requirements.txt` | Versiyalar yuqori chegara bilan (`<4`, `<2`) — aniq pin hali yo'q |

Android ilova alohida repoda: `~/developer/shoxa-android` (Kotlin/Compose).

## Deploy

- Hosting: **Railway** (Postgres ham o'sha yerda). `git push origin main` → avtomatik deploy (~1-2 daqiqa). VPS yo'q.
- Push'dan oldin **har doim**: `python3 -m py_compile bot.py api.py main.py` + o'zgargan helper'larni izolyatsiyada sinash.
- Lokalda `.env` va kutubxonalar yo'q — bot lokalda ishga tushmaydi. Jonli tekshirish uchun Railway CLI:
  `railway variables --service ai-agent-bot --json` (kalitlarni olib API'ni to'g'ridan-to'g'ri sinash mumkin).
- Xatolar adminga Telegram orqali keladi (`AdminAlertHandler`, `ADMIN_ID`) — loglarni qo'lda titkilash shart emas.

Env: `BOT_TOKEN GEMINI_API_KEY DATABASE_URL ADMIN_ID API_SECRET GEMINI_MODEL GEMINI_FALLBACK_MODEL IMAGE_MODEL IMAGE_MODEL_PRO TTS_MODEL TTS_VOICE GOOGLE_CSE_KEY GOOGLE_CSE_ID TZ PORT`

## Ma'lumotlar bazasi — QAT'IY QOIDA

Real foydalanuvchilar bor. **Migratsiyalar faqat additiv**: `CREATE TABLE IF NOT EXISTS`,
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Hech qachon DROP/RENAME/destruktiv UPDATE yo'q.
Jadvallar: `users profiles transactions reminders notes(+title,tags,code,lang) long_memory chat_history(user_id,chat_id)
business_chats business_log image_templates(BYTEA)`.

## Gemini modellari — TUZOQLAR

- Asosiy va zaxira: `gemini-3.5-flash` (2026-09-01 dan). `gemini-2.5-flash` yangi kalitlar uchun **404** (retired).
- `ask_agent` config'ida `thinking_config=ThinkingConfig(thinking_budget=0)` bor. **`gemini-3.6-flash` va
  `gemini-3.5-flash-lite` buni RAD qiladi (400 INVALID_ARGUMENT)** — faqat 3.5-flash qabul qiladi.
  Model almashtirsang, thinking_config'ni ham tekshir.
- Rasm: `gemini-3.1-flash-image` (oddiy), `gemini-3-pro-image` (shablon/"professional"). Rasm generatsiyasi
  **billing talab qiladi** — bepul kalitda 429. 429 → `_is_quota_error` → foydalanuvchiga tushunarli xabar.
- TTS: `gemini-2.5-flash-preview-tts` (Kore). TTS'ga matn "Read aloud the following text..." bilan o'raladi,
  aks holda model matnga javob berishga urinib 400 beradi. Bo'sh `content` bo'lishi mumkin — guard bor.
- Barcha chaqiruvlar `gemini_generate()` orqali (2 urinish + zaxira model, bo'sh javob ham qayta uriniladi).
- Yangi model muammosida taxmin qilma — kalit bilan `generativelanguage.googleapis.com/v1beta/models` ni
  jonli so'rab, modelni va config'ni REST orqali sinab ko'r (shu usul 2026-09-01 da hamma narsani hal qildi).

## Arxitektura qoidalari

- **Sinxron DB/HTTP chaqiruvlar async handlerda faqat `await asyncio.to_thread(...)` bilan.** "await'ni olib
  tashlash" — event loop'ni bloklaydi, taqiqlanadi.
- **Telegram fayl yuklash aiogram sessiyasi orqali EMAS** — `_fetch_telegram_file()` (`requests`, to'g'ridan-to'g'ri
  `api.telegram.org`). Aiogram polling sessiyasida `get_file` 60s timeout bo'lardi (2026-07-14).
- `ask_agent(user_id, parts, image_sink, device_action_sink, chat_id, system_prompt, tools_override, allow_tools)`.
  Kontekst kaliti `(user_id, chat_id)`; RAM + Postgres (`chat_history`). `chat_id`: 0 = Telegram oddiy,
  777 = /loyiha, 778 = /kod, 779 = /dizayn, ilova = o'z ID'lari (millis), biznes = mijoz chat ID'si.
- Tarix limiti: oddiy 20 almashinuv, 777/778/779 uchun 60 (`_history_limit`).
- **Begona suhbatdoshlar (Telegram Business) uchun faqat `SAFE_BUSINESS_DECLARATIONS`** (web_search, ob-havo,
  kurs, fetch_url). Egasining moliya/eslatma/qayd funksiyalari ularga OCHILMAYDI.
- Javob chiqishi `send_long()`: Markdown → Telegram **HTML** (`_md_to_html`), kod bloklari nusxalanadigan,
  bo'linganda fence yopilib qayta ochiladi (`_split_chunks`). Legacy `parse_mode="Markdown"` ishlatma — `_` da sinadi.
- O'zbekcha kirillcha kirish har joyda qo'llanadi: `_CYR2LAT` bilan transliteratsiya qilib keyin keyword tekshir.
- Har `logger.exception` adminga boradi (60s throttle) — xatolarni yutib yuborma, log qil.

## Rejimlar va komandalar

`/sozlamalar` (matn/ovoz javob; "ovozli javob ber"/"matnda javob ber" bir martalik override — `_detect_reply_override`),
`/biznes` (Telegram Business paneli: avto-javob, ma'lumot, ish vaqti, xabarnoma, statistika),
`/loyiha` (mahsulot strateg, 8 bosqich → hujjat + UI promptlari), `/kod` (senior review, traceback avto-aniqlash
`_looks_like_error`, kod fayllari), `/dizayn` (Claude Design/v0 uchun to'liq prompt, `_looks_like_design_request`),
`/shablon` (rasm shablonlari: caption `shablon: nom`), `/clear /forget /hisobot /eslatmalar`, admin: `/admin /approve /revoke /broadcast`.
Rejimlar RAM'da (`*_mode_users`) — deploy'da tushadi, tarix bazada qoladi.

## Rasm

- Yuklangan rasm 15 daqiqa `last_user_image`da; caption yoki keyingi xabar tahrir buyrug'i bo'lsa —
  `do_edit_image` (aynan o'sha rasm, lokal tahrir promptи). Savol bo'lsa — tahlil (chek → add_transaction).
- Shablon: `image_templates`, matnda "nom shabloni..." → shablon asos, yangi rasm ustiga (`extra_images`), pro model.
- **Prompt va siyosat: ID-karta/pasport/guvohnoma kabi hujjatlarga yuz/ism/raqam o'zgartirilmaydi** —
  foydalanuvchi so'rasa ham rad etiladi (2026-09-01 holati).

## Telegram Business

BotFather'da "Secretary/Business Mode" yoqilgan. `business_message` handler egasi nomidan (birinchi shaxsda,
AI ekanini aytmaydi) javob beradi; `send_business_message` funksiyasi egasi "X ga yozib yubor" desa haqiqiy yuboradi
(`business_chats` dan topadi). Egasining o'z xabari/botlar e'tiborsiz. Har kuni 21:00 `business_daily_job`.

## Android ilova (shoxa-android)

- API kaliti `app/build.gradle.kts` ichida (`API_SECRET`) — Railway'dagi `API_SECRET` bilan **bir xil** bo'lishi shart.
- Build: tizim Java 25 mos emas — `JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew assembleDebug`.
- Suhbatlar `ChatStore` (JSON fayl), har suhbat backend'ga `chat_id` yuboradi. Quick Settings tile, budilnik (sana bilan).

## Ish uslubi

- Foydalanuvchi o'zbekcha yozadi, tez natija kutadi: tuzatish → compile → sinov → commit → push (u odatda "xa" deydi).
- Xotira fayllari (`~/.claude/projects/.../memory/`) va bu CLAUDE.md ni sinxron tut.
- Roadmap (qolgan): xarajat himoyasi/rate-limit, kunlik shaxsiy xulosa 21:00, takroriy eslatmalar, byudjet limitlari,
  aniq versiya pin, bot.py ni modullarga bo'lish, ilova: davomiy ovozli suhbat, streaming, FCM push.
