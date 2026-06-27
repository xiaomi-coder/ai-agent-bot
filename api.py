"""
Sirdosh — Backend API
=====================
Android ilova (Shoxa) shu API'ga ulanadi.
Hozirgi Telegram botning "miyasi" (Gemini agent, xotira, funksiyalar) qayta ishlatiladi.

Endpointlar:
  GET  /              — health check
  POST /chat          — matnli savol  -> javob
  POST /voice         — ovozli savol (audio fayl) -> transkript + javob

Ishga tushirish (lokal):
  uvicorn api:app --reload --port 8000

Railway'da alohida "web" servis sifatida:
  uvicorn api:app --host 0.0.0.0 --port $PORT
"""

import os
import logging

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from google.genai import types

# Hozirgi botning miyasini import qilamiz (polling ishga tushmaydi — u __main__ da)
import bot

logger = logging.getLogger("sirdosh-api")

API_SECRET = os.getenv("API_SECRET", "")  # ilova shu kalit bilan ulanadi

app = FastAPI(title="Sirdosh API", version="1.0")

# Android ilova istalgan joydan ulanishi uchun
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    bot.db_init()
    logger.info("Sirdosh API ishga tushdi")


def _check_auth(authorization: str | None):
    if not API_SECRET:
        return  # kalit o'rnatilmagan bo'lsa, ochiq (faqat test uchun)
    token = (authorization or "").replace("Bearer ", "").strip()
    if token != API_SECRET:
        raise HTTPException(status_code=401, detail="Noto'g'ri API kaliti")


class ChatRequest(BaseModel):
    user_id: int
    text: str
    name: str | None = None


class ChatResponse(BaseModel):
    reply: str
    actions: list[dict] = []


@app.get("/")
def health():
    return {"status": "ok", "service": "Sirdosh API"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    # Foydalanuvchini bazaga belgilaymiz va ruxsat beramiz (ilova foydalanuvchilari)
    bot.db_track_user(req.user_id, None, req.name or "Ilova foydalanuvchisi")
    bot.db_approve_user(req.user_id)

    actions: list = []
    reply = await bot.ask_agent(req.user_id, [types.Part.from_text(text=req.text)], device_action_sink=actions)
    return ChatResponse(reply=reply, actions=actions)


class SpeakRequest(BaseModel):
    text: str


@app.post("/speak")
async def speak(req: SpeakRequest, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    import asyncio
    wav = await asyncio.to_thread(bot.do_tts, req.text)
    if wav is None:
        raise HTTPException(status_code=503, detail="Ovoz yaratib bo'lmadi")
    return Response(content=wav, media_type="audio/wav")


@app.post("/voice")
async def voice(
    user_id: int = Form(...),
    audio: UploadFile = File(...),
    name: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)
    data = await audio.read()
    mime = audio.content_type or "audio/ogg"

    bot.db_track_user(user_id, None, name or "Ilova foydalanuvchisi")
    bot.db_approve_user(user_id)

    # Gemini ovozni o'zbek tilida tushunadi
    transcript = await bot.transcribe_audio(data, mime)
    if not transcript:
        return {"transcript": "", "reply": "Ovozni tushunib bo'lmadi, qaytadan urinib ko'ring.", "actions": []}

    actions: list = []
    reply = await bot.ask_agent(user_id, [types.Part.from_text(text=transcript)], device_action_sink=actions)
    return {"transcript": transcript, "reply": reply, "actions": actions}
