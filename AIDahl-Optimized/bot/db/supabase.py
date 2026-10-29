"""
Thin Supabase PostgREST client.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "apikey": settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def sb_get(path: str, timeout: float = 12.0) -> List[Dict[str, Any]]:
    url = f"{settings.SUPABASE_URL}/rest/v1/{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url, headers=_headers())
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else [data]
    except Exception as e:
        logger.error("sb_get %s → %s", path, e)
        return []


def sb_post(
    path: str,
    payload: dict | list,
    prefer: str = "return=representation",
    timeout: float = 12.0,
) -> List[Dict[str, Any]]:
    url = f"{settings.SUPABASE_URL}/rest/v1/{path}"
    headers = _headers()
    headers["Prefer"] = prefer
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            if not r.content:
                return []
            data = r.json()
            return data if isinstance(data, list) else [data]
    except Exception as e:
        logger.error("sb_post %s → %s", path, e)
        return []


def sb_patch(path: str, payload: dict, timeout: float = 12.0) -> List[Dict[str, Any]]:
    url = f"{settings.SUPABASE_URL}/rest/v1/{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.patch(url, headers=_headers(), json=payload)
            r.raise_for_status()
            if not r.content:
                return []
            data = r.json()
            return data if isinstance(data, list) else [data]
    except Exception as e:
        logger.error("sb_patch %s → %s", path, e)
        return []


def sb_delete(path: str, timeout: float = 12.0) -> bool:
    url = f"{settings.SUPABASE_URL}/rest/v1/{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.delete(url, headers=_headers())
            r.raise_for_status()
            return True
    except Exception as e:
        logger.error("sb_delete %s → %s", path, e)
        return False