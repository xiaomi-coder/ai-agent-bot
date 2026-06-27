"""
Sirdosh — Birlashgan ishga tushirish
====================================
Bitta jarayonda HAM Telegram bot, HAM HTTP API (Android ilova uchun) ishlaydi.
Ikkalasi bitta "miya"ni (Gemini agent + xotira + Postgres) baham ko'radi.

Railway start command: python main.py
"""

import asyncio
import logging
import os

import uvicorn

import bot
from api import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sirdosh")


async def run_api():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    logger.info("Sirdosh: bot + API birga ishga tushmoqda...")
    await asyncio.gather(
        bot.main(),   # Telegram bot (polling)
        run_api(),    # HTTP API (Android ilova)
    )


if __name__ == "__main__":
    asyncio.run(main())
