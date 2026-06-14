# Shaxsiy AI Yordamchi — Telegram Bot 🤖

Gemini asosidagi AI agent. Matn, golos va rasmlarni tushunadi, o'zbek tilida ishlaydi.

## Imkoniyatlar

- 🔎 Internet qidiruv — "Dollar kursi qancha?" (Google Search orqali yangi ma'lumot)
- 💰 Shaxsiy buxgalter — "50 ming taksiga ketdi" → bazaga yoziladi
- 🖼️ Chek o'qish — chek rasmini yuboring, summani o'zi aniqlaydi
- 📊 Hisobot — "Bu oy qancha sarfladim?" → kategoriya bo'yicha hisobot
- ⏰ Eslatmalar — "Ertaga 9 da dorini eslatib qo'y" → vaqtida xabar keladi
- 📝 Qaydlar — "Eslab qol: ..." → keyin so'rasangiz topib beradi
- 🎤 Hammasi GOLOSDA ham ishlaydi (alohida STT kerak emas!)

## O'rnatish (5 daqiqa)

### 1. Tokenlarni oling
- Bot token: Telegram'da @BotFather ga /newbot yozing
- Gemini API key: https://aistudio.google.com/apikey (bepul tier bor)

### 2. Sozlash
```bash
cp .env.example .env
# .env ichiga tokenlaringizni yozing
```

### 3. Ishga tushirish
```bash
pip install -r requirements.txt
python bot.py
```

## Komandalar
- /start — boshlash
- /hisobot — oylik moliyaviy hisobot
- /eslatmalar — faol eslatmalar ro'yxati
- /clear — suhbat tarixini tozalash

## Hosting (24/7)
1. Render.com — Background Worker, Start Command: `python bot.py`, Environment'ga tokenlarni qo'shing
2. VPS (DigitalOcean, $5/oy) — systemd service yoki `nohup python bot.py &`

Muhim: assistant.db fayli (baza) o'chmasligi kerak — Render'da Disk qo'shing yoki VPS ishlating.

## Texnik tuzilish
- aiogram 3.x — Telegram bot framework
- Gemini API — AI (matn + audio + rasm tushunish, function calling)
- SQLite — kirim-chiqim, eslatmalar, qaydlar bazasi
- APScheduler — eslatmalarni vaqtida yuborish

## V3 g'oyalari
- Kunlik avtomatik xulosa (har kuni 21:00 da)
- Byudjet limitlari va ogohlantirishlar
- PDF/hujjat tahlili
- Golosli javob (TTS)
