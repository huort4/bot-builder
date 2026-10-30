"""
Security helpers for AIDahl.

- validate_public_https_url : SSRF guard for user-supplied / API-supplied URLs
- is_trusted_host           : decide whether an API key may be sent to a host
- KeyVault                  : optional at-rest encryption of BYOK keys (Fernet)
- install_log_redaction     : strip bot token / API keys from every log line
- q                         : URL-encode values used inside PostgREST filters

Only the standard library is required. `cryptography` is optional (KeyVault
degrades to plaintext with a loud warning if it is missing or unconfigured).
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import re
import socket
import threading
import time
from typing import Iterable, Optional, Tuple
from urllib.parse import quote, urlparse

# ---------------------------------------------------------------------------
# PostgREST filter quoting
# ---------------------------------------------------------------------------


def q(value) -> str:
    """Percent-encode a value so it cannot break out of a PostgREST filter."""
    return quote(str(value), safe="")


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

_URL_CACHE: dict = {}
_URL_CACHE_LOCK = threading.Lock()
_URL_CACHE_TTL = 300.0


def _ip_is_public(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return False
    # IPv4-mapped IPv6 (::ffff:10.0.0.1) must be judged by the embedded v4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_https_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_hosts: Optional[Iterable[str]] = None,
    use_cache: bool = True,
) -> Tuple[bool, str]:
    """
    Return (ok, reason). Rules:
      * https only, no credentials in the URL, hostname required
      * if `allowed_hosts` is given the host must match one of them
      * every resolved address must be public (unless allow_private)

    Note: a DNS answer can change between this check and the request
    (DNS rebinding). For a hobby bot this is an acceptable residual risk;
    for stricter setups pin the resolved IP or use an egress proxy.
    """
    url = (url or "").strip()
    if not url:
        return False, "آدرس خالی است."
    now = time.time()
    if use_cache:
        with _URL_CACHE_LOCK:
            hit = _URL_CACHE.get(url)
            if hit and now - hit[0] < _URL_CACHE_TTL:
                return hit[1], hit[2]

    ok, reason = _validate_uncached(url, allow_private, allowed_hosts)
    if use_cache:
        with _URL_CACHE_LOCK:
            if len(_URL_CACHE) > 512:
                _URL_CACHE.clear()
            _URL_CACHE[url] = (now, ok, reason)
    return ok, reason


def _validate_uncached(url, allow_private, allowed_hosts) -> Tuple[bool, str]:
    try:
        p = urlparse(url)
    except Exception:
        return False, "آدرس نامعتبر است."
    if p.scheme != "https" and not (allow_private and p.scheme == "http"):
        return False, "فقط https مجاز است."
    if p.username or p.password:
        return False, "آدرس نباید شامل نام‌کاربری/رمز باشد."
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return False, "هاست آدرس مشخص نیست."
    try:
        port = p.port or 443
    except ValueError:
        return False, "پورت نامعتبر است."

    if allowed_hosts:
        allowed = [h.strip().lower() for h in allowed_hosts if h and h.strip()]
        if allowed and not any(host == h or host.endswith("." + h) for h in allowed):
            return False, "این هاست در فهرست مجاز نیست."

    if allow_private:
        return True, "ok"

    # Literal IP in the URL
    try:
        ipaddress.ip_address(host)
        return (_ip_is_public(host), "ok" if _ip_is_public(host) else "آی‌پی خصوصی/داخلی مجاز نیست.")
    except ValueError:
        pass

    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        return False, "هاست داخلی مجاز نیست."
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        return False, "دامنه resolve نشد."
    if not infos:
        return False, "دامنه resolve نشد."
    for info in infos:
        if not _ip_is_public(info[4][0]):
            return False, "دامنه به آدرس خصوصی/داخلی اشاره می‌کند."
    return True, "ok"


def is_trusted_host(url: str, suffixes: Iterable[str]) -> bool:
    """True if `url` is https and its host equals / is a subdomain of a suffix."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower().rstrip(".")
    for s in suffixes:
        s = (s or "").strip().lower().lstrip(".")
        if s and (host == s or host.endswith("." + s)):
            return True
    return False


# ---------------------------------------------------------------------------
# At-rest encryption of BYOK keys (optional)
# ---------------------------------------------------------------------------

_PREFIX = "enc:v1:"


class KeyVault:
    """
    Encrypts API keys before they reach the database.

    Enable with env KEY_ENCRYPTION_SECRET=<long random string> and
    `pip install cryptography`. Existing plaintext rows keep working and are
    re-encrypted the next time the user saves the key.

    LOSING THE SECRET MAKES STORED KEYS UNRECOVERABLE (users must re-enter).
    """

    def __init__(self, secret: Optional[str]):
        self._fernet = None
        self.enabled = False
        secret = (secret or "").strip()
        if not secret:
            logging.warning(
                "KEY_ENCRYPTION_SECRET is not set: BYOK keys are stored in PLAINTEXT."
            )
            return
        try:
            from cryptography.fernet import Fernet  # type: ignore
        except Exception:
            logging.warning(
                "KEY_ENCRYPTION_SECRET is set but `cryptography` is not installed: "
                "keys stay in PLAINTEXT. Run: pip install cryptography"
            )
            return
        raw = hashlib.pbkdf2_hmac(
            "sha256", secret.encode(), b"aidahl-key-vault-v1", 200_000, dklen=32
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(raw))
        self.enabled = True

    def encrypt(self, plaintext: str) -> str:
        if not plaintext or not self.enabled:
            return plaintext
        token = self._fernet.encrypt(plaintext.encode()).decode()
        return _PREFIX + token

    def decrypt(self, stored: str) -> str:
        if not stored or not stored.startswith(_PREFIX):
            return stored  # legacy plaintext row
        if not self.enabled:
            logging.error("Encrypted key found but KeyVault is not enabled.")
            return ""
        try:
            return self._fernet.decrypt(stored[len(_PREFIX):].encode()).decode()
        except Exception:
            logging.error("Could not decrypt a stored key (wrong KEY_ENCRYPTION_SECRET?).")
            return ""


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------

_PATTERNS = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_\-]{30,}"), "bot<redacted>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)(x-manus-api-key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._\-]{8,}"), r"\1<redacted>"),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9]{16,}|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"), "<redacted>"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[Optional[str]]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def _clean(self, text: str) -> str:
        for s in self._secrets:
            if s in text:
                text = text.replace(s, "<redacted>")
        for rx, repl in _PATTERNS:
            text = rx.sub(repl, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        cleaned = self._clean(msg)
        if cleaned != msg:
            record.msg = cleaned
            record.args = ()
        return True


def install_log_redaction(secrets: Iterable[Optional[str]]) -> None:
    """Attach the filter to every root handler and quiet noisy HTTP loggers."""
    flt = RedactingFilter(secrets)
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(flt)
    # httpx logs full request URLs at INFO (that includes the Telegram file URL
    # which contains the bot token) — keep them out of the logs entirely.
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
