"""
Callback query handler – cleaned and modular.
"""
from __future__ import annotations

import logging

from telebot import TeleBot
from telebot.types import CallbackQuery

from bot.config import is_admin
from bot.keyboards import (
    main_keyboard,
    settings_keyboard,
    style_keyboard,
    providers_keyboard,
    chats_keyboard,
    manus_keyboard,
    admin_keyboard,
)
from bot.services.users import (
    get_user_settings,
    set_user_settings,
    get_bot_mode,
    set_bot_mode,
    list_user_keys,
    delete_user_key,
    mode_fa,
    quota_status,
    format_quota_block,
)
from bot.services.conversations import (
    list_conversations,
    get_active_conversation,
    set_active_conversation,
    create_conversation,
    purge_user_old_conversations,
)
from bot.services.manus import queue_status_text, force_unlock

logger = logging.getLogger(__name__)

# Simple pending state for key input
PENDING: dict[int, dict] = {}


def register_callbacks(bot: TeleBot) -> None:

    @bot.callback_query_handler(func=lambda call: True)
    def on_callback(call: CallbackQuery):
        try:
            _dispatch(bot, call)
        except Exception:
            logger.exception("callback error")
            try:
                bot.answer_callback_query(call.id, "خطا رخ داد", show_alert=True)
            except Exception:
                pass


def _dispatch(bot: TeleBot, call: CallbackQuery) -> None:
    data = call.data or ""
    uid = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    def answer(text: str = None, alert: bool = False):
        try:
            bot.answer_callback_query(call.id, text, show_alert=alert)
        except Exception:
            pass

    def edit(text: str, markup=None, parse_mode="HTML"):
        try:
            bot.edit_message_text(
                text, chat_id, msg_id, parse_mode=parse_mode, reply_markup=markup
            )
        except Exception:
            pass

    # ---- Main menu ----
    if data == "main_menu":
        mode = get_bot_mode(uid)
        edit(
            f"منوی اصلی\nحالت: {mode_fa(mode)}",
            main_keyboard(mode),
        )
        answer()
        return

    # ---- Settings ----
    if data == "settings":
        s = get_user_settings(uid)
        edit(
            f"⚙️ تنظیمات\nفرمت: {s['reply_style']} | استریم: {s['streaming']}\nمدل: {s['model_ref']}",
            settings_keyboard(s["reply_style"], s["streaming"], s["model_ref"]),
        )
        answer()
        return

    if data == "set_style":
        s = get_user_settings(uid)
        edit("فرمت پاسخ را انتخاب کنید:", style_keyboard(s["reply_style"]))
        answer()
        return

    if data.startswith("style:"):
        style = data.split(":", 1)[1]
        set_user_settings(uid, reply_style=style)
        s = get_user_settings(uid)
        edit(
            f"فرمت به <code>{style}</code> تغییر کرد.",
            settings_keyboard(s["reply_style"], s["streaming"], s["model_ref"]),
        )
        answer("ذخیره شد")
        return

    if data == "toggle_stream":
        s = get_user_settings(uid)
        new_val = not s["streaming"]
        set_user_settings(uid, streaming=new_val)
        s = get_user_settings(uid)
        edit(
            f"استریم {'روشن' if new_val else 'خاموش'} شد.",
            settings_keyboard(s["reply_style"], s["streaming"], s["model_ref"]),
        )
        answer()
        return

    if data == "set_model":
        answer("مدل را با فرمت provider/model بفرستید\nمثال: dahl/dahl-1 یا groq/llama-3.3-70b", alert=True)
        PENDING[uid] = {"action": "set_model"}
        return

    # ---- Mode ----
    if data == "toggle_mode":
        current = get_bot_mode(uid)
        new_mode = "manus" if current == "dahl" else "dahl"
        set_bot_mode(uid, new_mode)
        edit(f"حالت به {mode_fa(new_mode)} تغییر کرد.", main_keyboard(new_mode))
        answer()
        return

    # ---- Keys ----
    if data == "keys":
        keys = list_user_keys(uid)
        lines = ["🔑 کلیدهای شما\n"]
        if not keys:
            lines.append("کلیدی ثبت نشده.")
        else:
            for p, info in keys.items():
                lines.append(f"• {p}: {info['masked']}")
        edit("\n".join(lines), providers_keyboard())
        answer()
        return

    if data.startswith("keyprov:"):
        provider = data.split(":", 1)[1]
        PENDING[uid] = {"action": "set_key", "provider": provider}
        edit(
            f"کلید <b>{provider}</b> را ارسال کنید.\n"
            "برای حذف، کلمه <code>delete</code> را بفرستید.",
        )
        answer()
        return

    # ---- Chats ----
    if data == "chats":
        chats = list_conversations(uid)
        active = get_active_conversation(uid, get_user_settings(uid)["model_ref"])
        active_id = active.get("id") if active else None
        edit("گفتگوهای شما:", chats_keyboard(chats, active_id))
        answer()
        return

    if data == "new_chat":
        model = get_user_settings(uid)["model_ref"]
        conv = create_conversation(uid, model)
        purge_user_old_conversations(uid, conv["id"])
        edit("✅ گفتگوی جدید ساخته شد.", main_keyboard(get_bot_mode(uid)))
        answer()
        return

    if data.startswith("chat:"):
        cid = data.split(":", 1)[1]
        set_active_conversation(uid, cid)
        edit("گفتگو فعال شد.", main_keyboard(get_bot_mode(uid)))
        answer()
        return

    # ---- Usage ----
    if data == "usage":
        status = quota_status(uid)
        edit("📊 مصرف توکن\n\n" + format_quota_block(status), main_keyboard(get_bot_mode(uid)))
        answer()
        return

    # ---- Manus ----
    if data == "manus_ready":
        edit(
            "پیام یا عکس خود را بفرستید تا به Manus ارسال شود.",
            manus_keyboard(uid),
        )
        answer()
        return

    if data == "manus_queue":
        edit(queue_status_text(), manus_keyboard(uid))
        answer()
        return

    if data == "manus_session":
        from bot.services.manus import session_label, clear_session
        clear_session(uid)
        edit("جلسه Manus پاک شد.", manus_keyboard(uid))
        answer()
        return

    # ---- Admin ----
    if data == "admin_stats" and is_admin(uid):
        from bot.services.manus import queue_stats
        st = queue_stats()
        text = (
            f"آمار Manus\n"
            f"فعال: {st['active']}/{st['max_concurrent']}\n"
            f"صف: {st['queued']}/{st['max_queue']}"
        )
        edit(text, admin_keyboard())
        answer()
        return

    if data == "admin_manus_reset" and is_admin(uid):
        n = force_unlock()
        edit(f"ریست انجام شد ({n}).", admin_keyboard())
        answer()
        return

    answer()