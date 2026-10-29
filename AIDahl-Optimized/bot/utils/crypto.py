"""
Optional encryption for user API keys at rest.
If ENCRYPTION_KEY is not set, keys are stored as plain text (same as original).
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

from bot.config import settings

logger = logging.getLogger(__name__)

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    key = settings.ENCRYPTION_KEY
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet
    except Exception as e:
        logger.warning("Invalid ENCRYPTION_KEY – storing keys in plain text: %s", e)
        return None


def encrypt_key(plain: str) -> str:
    """Encrypt a key. Returns plain text if encryption is disabled."""
    f = _get_fernet()
    if not f or not plain:
        return plain
    try:
        return f.encrypt(plain.encode()).decode()
    except Exception as e:
        logger.error("encrypt failed: %s", e)
        return plain


def decrypt_key(cipher: str) -> str:
    """Decrypt a key. Returns original if not encrypted or encryption disabled."""
    f = _get_fernet()
    if not f or not cipher:
        return cipher
    try:
        return f.decrypt(cipher.encode()).decode()
    except Exception:
        # Probably stored as plain text (migration) or wrong key
        return cipher


def mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "••••••••"
    return key[:4] + "••••" + key[-4:]