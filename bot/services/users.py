"""
User settings, keys (BYOK), quotas and mode.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from bot.config import settings
from bot.db import sb_get, sb_post, sb_patch, sb_delete
from bot.utils import utcnow_iso, today_date_str, encrypt_key, decrypt_key, mask_key
from bot.utils.helpers import parse_model_ref, format_model_ref

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def ensure_user(from_user) -> None:
    """Upsert basic user row."""
    uid = from_user.id
    rows = sb_get(f"users?user_id=eq.{uid}&select=user_id")
    if rows:
        return
    payload = {
        "user_id": uid,
        "username": getattr(from_user, "username", None),
        "first_name": getattr(from_user, "first_name", None) or "",
        "created_at": utcnow_iso(),
    }
    sb_post("users", payload, prefer="resolution=merge-duplicates,return=minimal")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "reply_style": "html",
    "history_limit": None,          # None → use global HISTORY_LIMIT
    "streaming": True,
    "model_ref": "dahl/dahl-1",
    "bot_mode": "dahl",             # dahl | manus
}


def get_user_settings(user_id: int) -> dict:
    rows = sb_get(f"user_settings?user_id=eq.{user_id}&select=*")
    if not rows:
        return dict(DEFAULT_SETTINGS)
    row = rows[0]
    return {
        "reply_style": row.get("reply_style") or "html",
        "history_limit": row.get("history_limit"),
        "streaming": bool(row.get("streaming", True)),
        "model_ref": row.get("model_ref") or "dahl/dahl-1",
        "bot_mode": row.get("bot_mode") or "dahl",
    }


def set_user_settings(user_id: int, **fields) -> bool:
    allowed = {"reply_style", "history_limit", "streaming", "model_ref", "bot_mode"}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return False
    payload["updated_at"] = utcnow_iso()

    existing = sb_get(f"user_settings?user_id=eq.{user_id}&select=user_id")
    if existing:
        return bool(sb_patch(f"user_settings?user_id=eq.{user_id}", payload))
    payload["user_id"] = user_id
    return bool(sb_post("user_settings", payload))


def get_user_model(user_id: int) -> str:
    return get_user_settings(user_id).get("model_ref") or "dahl/dahl-1"


def set_user_model(user_id: int, model_ref: str) -> None:
    set_user_settings(user_id, model_ref=model_ref)


def get_bot_mode(user_id: int) -> str:
    return get_user_settings(user_id).get("bot_mode") or "dahl"


def set_bot_mode(user_id: int, mode: str) -> bool:
    mode = mode if mode in ("dahl", "manus") else "dahl"
    return set_user_settings(user_id, bot_mode=mode)


def mode_fa(mode: str) -> str:
    return "🎨 Manus" if mode == "manus" else "💬 Dahl"


# ---------------------------------------------------------------------------
# API Keys (BYOK) – with optional encryption
# ---------------------------------------------------------------------------

def list_user_keys(user_id: int) -> Dict[str, dict]:
    rows = sb_get(f"user_api_keys?user_id=eq.{user_id}&select=*")
    result = {}
    for r in rows:
        provider = r.get("provider")
        if not provider:
            continue
        raw_key = r.get("api_key") or ""
        result[provider] = {
            "provider": provider,
            "api_key": decrypt_key(raw_key),
            "base_url": r.get("base_url"),
            "is_active": r.get("is_active", True),
            "masked": mask_key(decrypt_key(raw_key)),
        }
    return result


def upsert_user_key(
    user_id: int,
    provider: str,
    api_key: str,
    base_url: Optional[str] = None,
) -> bool:
    provider = provider.lower().strip()
    payload = {
        "user_id": user_id,
        "provider": provider,
        "api_key": encrypt_key(api_key),
        "base_url": base_url,
        "is_active": True,
        "updated_at": utcnow_iso(),
    }
    # Try update first
    existing = sb_get(f"user_api_keys?user_id=eq.{user_id}&provider=eq.{provider}&select=id")
    if existing:
        return bool(
            sb_patch(
                f"user_api_keys?user_id=eq.{user_id}&provider=eq.{provider}",
                payload,
            )
        )
    payload["created_at"] = utcnow_iso()
    return bool(sb_post("user_api_keys", payload))


def delete_user_key(user_id: int, provider: str) -> bool:
    return sb_delete(f"user_api_keys?user_id=eq.{user_id}&provider=eq.{provider}")


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------

def ensure_quota_row(user_id: int) -> dict:
    rows = sb_get(f"user_quotas?user_id=eq.{user_id}&select=*")
    if rows:
        row = rows[0]
        # Reset daily if needed
        if row.get("daily_date") != today_date_str():
            sb_patch(
                f"user_quotas?user_id=eq.{user_id}",
                {
                    "daily_used": 0,
                    "daily_date": today_date_str(),
                    "updated_at": utcnow_iso(),
                },
            )
            row["daily_used"] = 0
            row["daily_date"] = today_date_str()
        return row

    payload = {
        "user_id": user_id,
        "daily_used": 0,
        "daily_limit": settings.DEFAULT_DAILY_TOKEN_QUOTA,
        "lifetime_used": 0,
        "lifetime_limit": settings.DEFAULT_LIFETIME_TOKEN_QUOTA,
        "daily_date": today_date_str(),
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
    }
    created = sb_post("user_quotas", payload)
    return created[0] if created else payload


def quota_status(user_id: int) -> dict:
    row = ensure_quota_row(user_id)
    daily_used = int(row.get("daily_used") or 0)
    daily_limit = int(row.get("daily_limit") or settings.DEFAULT_DAILY_TOKEN_QUOTA)
    lifetime_used = int(row.get("lifetime_used") or 0)
    lifetime_limit = int(row.get("lifetime_limit") or settings.DEFAULT_LIFETIME_TOKEN_QUOTA)

    return {
        "daily_used": daily_used,
        "daily_limit": daily_limit,
        "daily_left": max(0, daily_limit - daily_used),
        "lifetime_used": lifetime_used,
        "lifetime_limit": lifetime_limit,
        "lifetime_left": max(0, lifetime_limit - lifetime_used),
        "lifetime_pct": round(100 * lifetime_used / lifetime_limit, 1) if lifetime_limit else 0,
    }


def quota_add_usage(user_id: int, tokens: int) -> dict:
    if tokens <= 0:
        return quota_status(user_id)
    row = ensure_quota_row(user_id)
    new_daily = int(row.get("daily_used") or 0) + tokens
    new_life = int(row.get("lifetime_used") or 0) + tokens
    sb_patch(
        f"user_quotas?user_id=eq.{user_id}",
        {
            "daily_used": new_daily,
            "lifetime_used": new_life,
            "updated_at": utcnow_iso(),
        },
    )
    return quota_status(user_id)


def quota_allowed(user_id: int, source: str = "shared") -> Tuple[bool, dict]:
    """
    source: 'shared' | 'own'
    Returns (allowed, status)
    """
    status = quota_status(user_id)
    if source == "own" and not settings.QUOTA_APPLIES_TO_OWN_KEYS:
        return True, status
    if status["daily_left"] <= 0 or status["lifetime_left"] <= 0:
        return False, status
    return True, status


def format_quota_block(status: dict, applies_note: str = "") -> str:
    lines = [
        f"📅 روزانه: {status['daily_used']:,} / {status['daily_limit']:,}",
        f"♾ مادام‌العمر: {status['lifetime_used']:,} / {status['lifetime_limit']:,} ({status['lifetime_pct']}%)",
    ]
    if applies_note:
        lines.append(applies_note)
    return "\n".join(lines)


def quota_warning_text(status: dict) -> Optional[str]:
    if status["lifetime_pct"] >= settings.LIFETIME_WARN_PCT:
        return (
            f"⚠️ شما به {status['lifetime_pct']}% از سهمیه مادام‌العمر رسیده‌اید. "
            f"باقی‌مانده: {status['lifetime_left']:,} توکن."
        )
    if status["daily_left"] < 5000:
        return f"⚠️ سهمیه روزانه شما رو به اتمام است ({status['daily_left']:,} باقی‌مانده)."
    return None