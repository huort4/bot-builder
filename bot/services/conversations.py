"""
Conversation & message storage with hygiene policy.
"""
from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from bot.config import settings
from bot.db import sb_get, sb_post, sb_patch, sb_delete
from bot.utils import utcnow_iso

logger = logging.getLogger(__name__)


def create_conversation(user_id: int, model: str, title: str = "گفتگوی جدید") -> dict:
    cid = str(uuid.uuid4())
    payload = {
        "id": cid,
        "user_id": user_id,
        "model": model,
        "title": title[:80],
        "is_active": True,
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
    }
    # Deactivate previous
    sb_patch(f"conversations?user_id=eq.{user_id}&is_active=eq.true", {"is_active": False})
    created = sb_post("conversations", payload)
    return created[0] if created else payload


def list_conversations(user_id: int, limit: int = None) -> List[dict]:
    limit = limit or settings.CHAT_LIST_LIMIT
    return sb_get(
        f"conversations?user_id=eq.{user_id}&order=updated_at.desc&limit={limit}&select=*"
    )


def get_conversation_by_id(conversation_id: str) -> Optional[dict]:
    rows = sb_get(f"conversations?id=eq.{conversation_id}&select=*")
    return rows[0] if rows else None


def get_active_conversation(user_id: int, model: str) -> dict:
    rows = sb_get(
        f"conversations?user_id=eq.{user_id}&is_active=eq.true&select=*&limit=1"
    )
    if rows:
        return rows[0]
    return create_conversation(user_id, model)


def set_active_conversation(user_id: int, conversation_id: str) -> bool:
    sb_patch(f"conversations?user_id=eq.{user_id}&is_active=eq.true", {"is_active": False})
    return bool(
        sb_patch(
            f"conversations?id=eq.{conversation_id}&user_id=eq.{user_id}",
            {"is_active": True, "updated_at": utcnow_iso()},
        )
    )


def rename_conversation(conversation_id: str, title: str) -> bool:
    return bool(
        sb_patch(
            f"conversations?id=eq.{conversation_id}",
            {"title": title[:80], "updated_at": utcnow_iso()},
        )
    )


def touch_conversation(conversation_id: str, model: str, title: Optional[str] = None) -> None:
    payload = {"updated_at": utcnow_iso(), "model": model}
    if title:
        payload["title"] = title[:80]
    sb_patch(f"conversations?id=eq.{conversation_id}", payload)


def purge_user_old_conversations(user_id: int, keep_conversation_id: Optional[str]) -> int:
    """Delete all conversations except the active one (and their messages)."""
    rows = sb_get(f"conversations?user_id=eq.{user_id}&select=id")
    deleted = 0
    for r in rows:
        cid = r["id"]
        if keep_conversation_id and cid == keep_conversation_id:
            continue
        sb_delete(f"messages?conversation_id=eq.{cid}")
        if sb_delete(f"conversations?id=eq.{cid}"):
            deleted += 1
    return deleted


def trim_conversation_messages(conversation_id: str, keep_last: Optional[int] = None) -> int:
    keep_last = keep_last or settings.KEEP_MESSAGES_IN_ACTIVE_CHAT
    rows = sb_get(
        f"messages?conversation_id=eq.{conversation_id}&order=created_at.desc&select=id"
    )
    if len(rows) <= keep_last:
        return 0
    to_delete = [r["id"] for r in rows[keep_last:]]
    for mid in to_delete:
        sb_delete(f"messages?id=eq.{mid}")
    return len(to_delete)


def get_conversation_history(conversation_id: str, limit: int = None) -> list:
    limit = limit or settings.HISTORY_LIMIT
    rows = sb_get(
        f"messages?conversation_id=eq.{conversation_id}"
        f"&order=created_at.asc&limit={limit}&select=role,content"
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def save_message(
    conversation_id: str,
    role: str,
    content: str,
    tokens: int = 0,
) -> None:
    payload = {
        "conversation_id": conversation_id,
        "role": role,
        "content": content[:12000],
        "tokens": tokens,
        "created_at": utcnow_iso(),
    }
    sb_post("messages", payload, prefer="return=minimal")


def count_conversation_messages(conversation_id: str) -> int:
    rows = sb_get(f"messages?conversation_id=eq.{conversation_id}&select=id")
    return len(rows)