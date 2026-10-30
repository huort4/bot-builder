"""
Main text + media handlers.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from telebot import TeleBot
from telebot.types import Message

from bot.config import settings
from bot.handlers.callbacks import PENDING
from bot.services.users import (
    ensure_user,
    get_user_settings,
    set_user_settings,
    get_bot_mode,
    upsert_user_key,
    delete_user_key,
    quota_allowed,
    quota_add_usage,
    quota_warning_text,
    list_user_keys,
)
from bot.services.conversations import (
    get_active_conversation,
    get_conversation_history,
    save_message,
    touch_conversation,
    trim_conversation_messages,
)
from bot.services.llm import resolve_inference_credentials, call_llm
from bot.services.manus import (
    get_manus_key,
    get_session,
    submit_job,
)
from bot.utils.helpers import render_for_telegram, truncate_message

logger = logging.getLogger(__name__)


def register_chat_handlers(bot: TeleBot) -> None:

    @bot.message_handler(content_types=["photo", "document"])
    def on_media(message: Message):
        ensure_user(message.from_user)
        uid = message.from_user.id
        if get_bot_mode(uid) != "manus":
            bot.reply_to(message, "برای ارسال رسانه ابتدا /manus را بزنید.")
            return
        _handle_manus(bot, message, has_media=True)

    @bot.message_handler(func=lambda m: m.content_type == "text", content_types=["text"])
    def on_text(message: Message):
        ensure_user(message.from_user)
        uid = message.from_user.id
        text = (message.text or "").strip()
        if not text:
            return

        # Pending input (set key / set model)
        if uid in PENDING:
            if _handle_pending(bot, message):
                return

        mode = get_bot_mode(uid)
        if mode == "manus":
            _handle_manus(bot, message, has_media=False)
        else:
            _handle_dahl_chat(bot, message)


def _handle_pending(bot: TeleBot, message: Message) -> bool:
    uid = message.from_user.id
    state = PENDING.get(uid)
    if not state:
        return False
    text = (message.text or "").strip()
    action = state.get("action")

    if action == "set_key":
        provider = state["provider"]
        del PENDING[uid]
        if text.lower() in ("delete", "del", "پاک", "حذف"):
            delete_user_key(uid, provider)
            bot.reply_to(message, f"کلید {provider} حذف شد.")
            return True
        upsert_user_key(uid, provider, text)
        bot.reply_to(message, f"✅ کلید {provider} ذخیره شد.")
        return True

    if action == "set_model":
        del PENDING[uid]
        set_user_settings(uid, model_ref=text)
        bot.reply_to(message, f"✅ مدل به <code>{text}</code> تغییر کرد.", parse_mode="HTML")
        return True

    return False


def _handle_dahl_chat(bot: TeleBot, message: Message) -> None:
    uid = message.from_user.id
    text = message.text.strip()
    settings_u = get_user_settings(uid)
    model_ref = settings_u["model_ref"]

    creds = resolve_inference_credentials(uid)
    if not creds:
        bot.reply_to(
            message,
            "هیچ کلید API در دسترس نیست.\n"
            "از /keys یک کلید اضافه کنید یا ادمین کلید مشترک تنظیم کند.",
        )
        return

    allowed, qstatus = quota_allowed(uid, creds["source"])
    if not allowed:
        bot.reply_to(message, "❌ سهمیه توکن شما تمام شده است.\n" + format_quota_block(qstatus))
        return

    conv = get_active_conversation(uid, model_ref)
    history = get_conversation_history(
        conv["id"],
        limit=settings_u.get("history_limit") or settings.HISTORY_LIMIT,
    )
    messages = [{"role": "system", "content": settings.SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    # Save user message early
    save_message(conv["id"], "user", text)

    status_msg = bot.reply_to(message, "⏳ در حال فکر کردن…")
    last_edit = 0.0
    full_text = ""

    def on_partial(current: str):
        nonlocal last_edit, full_text
        full_text = current
        now = time.time()
        if now - last_edit < settings.STREAM_EDIT_INTERVAL:
            return
        last_edit = now
        rendered, parse_mode = render_for_telegram(
            truncate_message(current + " ▍"), settings_u["reply_style"]
        )
        try:
            bot.edit_message_text(
                rendered,
                message.chat.id,
                status_msg.message_id,
                parse_mode=parse_mode,
            )
        except Exception:
            pass

    use_stream = settings.STREAMING_ENABLED and settings_u.get("streaming", True)

    result = call_llm(
        messages,
        creds,
        stream=use_stream,
        on_partial=on_partial if use_stream else None,
    )

    if result.get("error"):
        bot.edit_message_text(
            f"❌ خطا: {result['error']}",
            message.chat.id,
            status_msg.message_id,
        )
        return

    content = result["content"] or full_text or "پاسخی دریافت نشد."
    tokens = result.get("tokens") or 0

    # Final render
    rendered, parse_mode = render_for_telegram(content, settings_u["reply_style"])
    try:
        bot.edit_message_text(
            rendered,
            message.chat.id,
            status_msg.message_id,
            parse_mode=parse_mode,
        )
    except Exception:
        bot.send_message(message.chat.id, rendered, parse_mode=parse_mode)

    save_message(conv["id"], "assistant", content, tokens=tokens)
    touch_conversation(conv["id"], model_ref)
    trim_conversation_messages(conv["id"])

    if tokens:
        new_status = quota_add_usage(uid, tokens)
        warn = quota_warning_text(new_status)
        if warn:
            bot.send_message(message.chat.id, warn)


def _handle_manus(bot: TeleBot, message: Message, has_media: bool = False) -> None:
    uid = message.from_user.id
    api_key = get_manus_key(uid)
    if not api_key:
        bot.reply_to(message, "کلید Manus موجود نیست. از /keys اضافه کنید.")
        return

    prompt = (message.caption or message.text or "").strip()
    if not prompt and not has_media:
        bot.reply_to(message, "لطفاً یک توضیح بنویسید.")
        return
    if not prompt:
        prompt = "Analyze / edit this image as requested."

    # Follow-up?
    session = get_session(uid)
    is_followup = bool(session)
    task_id = session["task_id"] if session else None

    status = bot.reply_to(message, "🎨 در صف Manus…")
    ok, msg = submit_job(
        bot,
        uid,
        message.chat.id,
        prompt,
        api_key=api_key,
        status_msg_id=status.message_id,
        is_followup=is_followup,
        task_id=task_id,
    )
    if not ok:
        bot.edit_message_text(f"❌ {msg}", message.chat.id, status.message_id)


# helper import for quota text
from bot.services.users import format_quota_block  # noqa: E402