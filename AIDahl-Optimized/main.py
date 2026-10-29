#!/usr/bin/env python3
"""
AIDahl Optimized – Telegram AI Bot
Version 2.0.0-optimized

Modular, cleaner, with optional key encryption and the same feature set.
"""
from __future__ import annotations

import logging
import sys

import telebot

from bot.config import settings
from bot.handlers.commands import register_commands
from bot.handlers.callbacks import register_callbacks
from bot.handlers.chat import register_chat_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("aidahl")


def main() -> None:
    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN is missing")
        sys.exit(1)
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        logger.error("SUPABASE_URL / SUPABASE_KEY missing")
        sys.exit(1)

    bot = telebot.TeleBot(settings.BOT_TOKEN, num_threads=4)
    try:
        bot.timeout = 30
    except Exception:
        pass

    register_commands(bot)
    register_callbacks(bot)
    register_chat_handlers(bot)

    logger.info("AIDahl %s starting…", settings.BOT_VERSION)
    logger.info("Admin IDs: %s", settings.ADMIN_TELEGRAM_IDS or "none")

    # Infinity polling – robust against network hiccups
    bot.infinity_polling(
        timeout=25,
        long_polling_timeout=20,
        allowed_updates=["message", "callback_query"],
        restart_on_change=False,
    )


if __name__ == "__main__":
    main()