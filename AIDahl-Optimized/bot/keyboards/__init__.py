"""
Inline keyboards – kept simple and consistent.
"""
from __future__ import annotations

from typing import List, Optional

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.config import settings
from bot.services.users import mode_fa


def main_keyboard(mode: str = "dahl") -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("⚙️ تنظیمات", callback_data="settings"),
        InlineKeyboardButton("🔑 کلیدها", callback_data="keys"),
    )
    kb.add(
        InlineKeyboardButton("💬 گفتگوها", callback_data="chats"),
        InlineKeyboardButton("📊 مصرف", callback_data="usage"),
    )
    kb.add(
        InlineKeyboardButton(
            f"حالت: {mode_fa(mode)}", callback_data="toggle_mode"
        )
    )
    kb.add(
        InlineKeyboardButton(settings.PROMPTOPIA_LABEL, url=settings.PROMPTOPIA_URL)
    )
    return kb


def settings_keyboard(style: str, streaming: bool, model_ref: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"فرمت: {style}", callback_data="set_style"),
        InlineKeyboardButton(
            f"استریم: {'روشن' if streaming else 'خاموش'}", callback_data="toggle_stream"
        ),
    )
    kb.add(InlineKeyboardButton(f"مدل: {model_ref}", callback_data="set_model"))
    kb.add(InlineKeyboardButton("« بازگشت", callback_data="main_menu"))
    return kb


def style_keyboard(current: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    for s in ("html", "markdown", "plain"):
        mark = "✓ " if s == current else ""
        kb.add(InlineKeyboardButton(f"{mark}{s}", callback_data=f"style:{s}"))
    kb.add(InlineKeyboardButton("« بازگشت", callback_data="settings"))
    return kb


def providers_keyboard() -> InlineKeyboardMarkup:
    providers = [
        ("dahl", "Dahl"),
        ("openrouter", "OpenRouter"),
        ("groq", "Groq"),
        ("cerebras", "Cerebras"),
        ("siliconflow", "SiliconFlow"),
        ("mistral", "Mistral"),
        ("github", "GitHub"),
        ("manus", "Manus"),
        ("custom", "Custom"),
    ]
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(label, callback_data=f"keyprov:{p}") for p, label in providers]
    kb.add(*buttons)
    kb.add(InlineKeyboardButton("« بازگشت", callback_data="keys"))
    return kb


def chats_keyboard(chats: List[dict], active_id: Optional[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for c in chats:
        title = c.get("title") or "بدون عنوان"
        mark = "📍 " if c.get("id") == active_id else ""
        kb.add(
            InlineKeyboardButton(
                f"{mark}{title[:40]}", callback_data=f"chat:{c['id']}"
            )
        )
    kb.add(InlineKeyboardButton("➕ گفتگوی جدید", callback_data="new_chat"))
    kb.add(InlineKeyboardButton("« بازگشت", callback_data="main_menu"))
    return kb


def manus_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    from bot.services.manus import session_label, queue_status_text

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🎨 ارسال به Manus", callback_data="manus_ready"))
    if user_id:
        kb.add(InlineKeyboardButton(session_label(user_id), callback_data="manus_session"))
    kb.add(InlineKeyboardButton("📋 وضعیت صف", callback_data="manus_queue"))
    kb.add(InlineKeyboardButton("« بازگشت", callback_data="main_menu"))
    return kb


def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("آمار", callback_data="admin_stats"),
        InlineKeyboardButton("ریست صف Manus", callback_data="admin_manus_reset"),
    )
    kb.add(InlineKeyboardButton("« بازگشت", callback_data="main_menu"))
    return kb