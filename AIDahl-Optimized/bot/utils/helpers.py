"""
Small pure helpers used across the bot.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone, date
from typing import Optional, Tuple

from bot.config import settings


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_date_str() -> str:
    return date.today().isoformat()


def truncate_message(text: str, limit: int = None) -> str:
    limit = limit or settings.MAX_TG_MESSAGE
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n… (truncated)"


def escape_telegram_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", text)


def llm_text_to_html(text: str) -> str:
    """Very light conversion of common markdown to HTML for Telegram."""
    if not text:
        return ""
    # Escape first
    t = html.escape(text)
    # Bold **text**
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    # Italic *text*
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", t)
    # Inline code `code`
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    # Code blocks ```...```
    t = re.sub(r"```(?:\w+)?\n?(.*?)```", r"<pre>\1</pre>", t, flags=re.DOTALL)
    return t


def render_for_telegram(text: str, style: str = "html") -> Tuple[str, Optional[str]]:
    """
    Returns (text, parse_mode).
    style: html | markdown | plain
    """
    if not text:
        return "", None
    if style == "html":
        return llm_text_to_html(text), "HTML"
    if style == "markdown":
        return escape_telegram_markdown(text), "MarkdownV2"
    return text, None


def parse_model_ref(raw: str) -> Tuple[str, str]:
    """
    Parse 'provider/model' or just 'model'.
    Returns (provider, model_id)
    """
    raw = (raw or "").strip()
    if "/" in raw:
        provider, model = raw.split("/", 1)
        return provider.lower().strip(), model.strip()
    return "dahl", raw


def format_model_ref(provider: str, model_id: str) -> str:
    return f"{provider}/{model_id}"