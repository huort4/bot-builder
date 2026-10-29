"""
Command handlers (/start, /settings, /keys, /manus, …)
"""
from __future__ import annotations

import logging

from telebot import TeleBot
from telebot.types import Message

from bot.config import settings, is_admin
from bot.keyboards import (
    main_keyboard,
    settings_keyboard,
    providers_keyboard,
    chats_keyboard,
    manus_keyboard,
    admin_keyboard,
)
from bot.services.users import (
    ensure_user,
    get_user_settings,
    get_bot_mode,
    set_bot_mode,
    list_user_keys,
    quota_status,
    format_quota_block,
    mode_fa,
)
from bot.services.conversations import (
    list_conversations,
    get_active_conversation,
    create_conversation,
    purge_user_old_conversations,
)
from bot.services.manus import queue_status_text, force_unlock, get_manus_key

logger = logging.getLogger(__name__)


def register_commands(bot: TeleBot) -> None:

    @bot.message_handler(commands=["start", "help"])
    def cmd_start(message: Message):
        ensure_user(message.from_user)
        mode = get_bot_mode(message.from_user.id)
        text = (
            f"سلام {message.from_user.first_name or ''} 👋\n\n"
            f"<b>AIDahl</b> — دستیار هوشمند تلگرام\n"
            f"نسخه: <code>{settings.BOT_VERSION}</code>\n\n"
            f"حالت فعلی: {mode_fa(mode)}\n\n"
            "دستورات اصلی:\n"
            "/settings — تنظیمات\n"
            "/keys — کلیدهای API (BYOK)\n"
            "/manus — حالت Agent تصویر\n"
            "/chats — مدیریت گفتگوها\n"
            "/clear — گفتگوی جدید\n"
            "/usage — مصرف توکن\n"
        )
        bot.send_message(
            message.chat.id,
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(mode),
        )

    @bot.message_handler(commands=["settings", "config"])
    def cmd_settings(message: Message):
        ensure_user(message.from_user)
        s = get_user_settings(message.from_user.id)
        text = (
            f"⚙️ <b>تنظیمات</b>\n\n"
            f"فرمت پاسخ: <code>{s['reply_style']}</code>\n"
            f"استریم: {'روشن' if s['streaming'] else 'خاموش'}\n"
            f"مدل: <code>{s['model_ref']}</code>\n"
            f"حالت: {mode_fa(s['bot_mode'])}"
        )
        bot.send_message(
            message.chat.id,
            text,
            parse_mode="HTML",
            reply_markup=settings_keyboard(s["reply_style"], s["streaming"], s["model_ref"]),
        )

    @bot.message_handler(commands=["keys", "setkey", "byok"])
    def cmd_keys(message: Message):
        ensure_user(message.from_user)
        keys = list_user_keys(message.from_user.id)
        lines = ["🔑 <b>کلیدهای شما (BYOK)</b>\n"]
        if not keys:
            lines.append("هنوز کلیدی ثبت نکرده‌اید.")
        else:
            for p, info in keys.items():
                lines.append(f"• <b>{p}</b>: <code>{info['masked']}</code>")
        lines.append("\nیک ارائه‌دهنده را انتخاب کنید:")
        bot.send_message(
            message.chat.id,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=providers_keyboard(),
        )

    @bot.message_handler(commands=["chats", "chatlist"])
    def cmd_chats(message: Message):
        ensure_user(message.from_user)
        uid = message.from_user.id
        chats = list_conversations(uid)
        active = get_active_conversation(uid, get_user_settings(uid)["model_ref"])
        active_id = active.get("id") if active else None
        text = "💬 <b>گفتگوهای شما</b>\nیکی را انتخاب کنید یا گفتگوی جدید بسازید."
        bot.send_message(
            message.chat.id,
            text,
            parse_mode="HTML",
            reply_markup=chats_keyboard(chats, active_id),
        )

    @bot.message_handler(commands=["clear", "newchat"])
    def cmd_clear(message: Message):
        ensure_user(message.from_user)
        uid = message.from_user.id
        model = get_user_settings(uid)["model_ref"]
        conv = create_conversation(uid, model)
        purge_user_old_conversations(uid, conv["id"])
        bot.reply_to(message, "✅ گفتگوی جدید شروع شد. تاریخچه قبلی پاک شد.")

    @bot.message_handler(commands=["usage"])
    def cmd_usage(message: Message):
        ensure_user(message.from_user)
        status = quota_status(message.from_user.id)
        text = "📊 <b>مصرف توکن</b>\n\n" + format_quota_block(status)
        bot.send_message(message.chat.id, text, parse_mode="HTML")

    @bot.message_handler(commands=["mode"])
    def cmd_mode(message: Message):
        ensure_user(message.from_user)
        uid = message.from_user.id
        current = get_bot_mode(uid)
        new_mode = "manus" if current == "dahl" else "dahl"
        set_bot_mode(uid, new_mode)
        bot.reply_to(message, f"حالت به {mode_fa(new_mode)} تغییر کرد.")

    @bot.message_handler(commands=["manus", "agent"])
    def cmd_manus(message: Message):
        ensure_user(message.from_user)
        uid = message.from_user.id
        set_bot_mode(uid, "manus")
        key = get_manus_key(uid)
        if not key:
            bot.reply_to(
                message,
                "کلید Manus تنظیم نشده. از /keys یک کلید Manus اضافه کنید "
                "یا ادمین کلید مشترک تنظیم کند.",
            )
            return
        bot.send_message(
            message.chat.id,
            "🎨 <b>حالت Manus Agent</b>\n\n"
            "پیام متنی یا عکس بفرستید تا به Manus ارسال شود.\n"
            "برای follow-up روی همان تسک، فقط پیام بعدی را بفرستید.",
            parse_mode="HTML",
            reply_markup=manus_keyboard(uid),
        )

    @bot.message_handler(commands=["manusqueue", "queue"])
    def cmd_queue(message: Message):
        bot.reply_to(message, queue_status_text())

    @bot.message_handler(commands=["manusreset"])
    def cmd_manus_reset(message: Message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "فقط ادمین.")
            return
        n = force_unlock()
        bot.reply_to(message, f"صف و قفل‌ها پاک شد ({n} مورد).")

    @bot.message_handler(commands=["admin"])
    def cmd_admin(message: Message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "دسترسی ندارید.")
            return
        bot.send_message(
            message.chat.id,
            "🛠 پنل ادمین",
            reply_markup=admin_keyboard(),
        )

    @bot.message_handler(commands=["prompts", "promptopia", "gallery"])
    def cmd_prompts(message: Message):
        bot.send_message(
            message.chat.id,
            f'<a href="{settings.PROMPTOPIA_URL}">{settings.PROMPTOPIA_LABEL}</a>',
            parse_mode="HTML",
            disable_web_page_preview=False,
        )

    @bot.message_handler(commands=["cancel"])
    def cmd_cancel(message: Message):
        # Clear any pending input state if we had one
        bot.reply_to(message, "عملیات لغو شد.")