"""
AIDahl v2 — compact Telegram AI bot.

Multi-provider chat (BYOK, OpenAI-compatible, streaming) + optional Manus agent
(image create/edit, multi-turn follow-ups), quotas, saved prompts, admin stats.

Run:  python main.py        (needs security.py next to it)
"""
from __future__ import annotations

import base64
import functools
import html
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import telebot
from dotenv import load_dotenv
from telebot.apihelper import ApiTelegramException
from telebot.types import BotCommand
from telebot.types import InlineKeyboardButton as Btn
from telebot.types import InlineKeyboardMarkup as Kb

from security import (
    KeyVault,
    install_log_redaction,
    is_trusted_host,
    q,
    validate_public_https_url,
)

load_dotenv()
VERSION = "2.0.0"

# ════════════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════════════


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    return env(name, str(default)).lower() in ("1", "true", "yes")


BOT_TOKEN = env("BOT_TOKEN")
SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = env("SUPABASE_KEY")
DAHL_API_KEY = env("DAHL_API_KEY")
ALLOW_SHARED_KEY = env_bool("ALLOW_SHARED_KEY", True)
HISTORY_LIMIT = env_int("HISTORY_LIMIT", 20)
STREAMING_ENABLED = env_bool("STREAMING_ENABLED", True)
STREAM_EDIT_INTERVAL = env_float("STREAM_EDIT_INTERVAL", 1.2)
DAILY_QUOTA = env_int("DEFAULT_DAILY_TOKEN_QUOTA", 50_000)
LIFETIME_QUOTA = env_int("DEFAULT_LIFETIME_TOKEN_QUOTA", 3_000_000)
LIFETIME_WARN_PCT = env_float("LIFETIME_WARN_PCT", 75)
QUOTA_ON_OWN_KEYS = env_bool("QUOTA_APPLIES_TO_OWN_KEYS", False)
KEEP_MESSAGES = env_int("KEEP_MESSAGES_IN_ACTIVE_CHAT", 40)
ADMIN_IDS = {int(x) for x in env("ADMIN_TELEGRAM_IDS").split(",") if x.strip().isdigit()}

KEY_ENCRYPTION_SECRET = env("KEY_ENCRYPTION_SECRET")
CUSTOM_ALLOW_PRIVATE = env_bool("CUSTOM_URL_ALLOW_PRIVATE", False)
CUSTOM_ALLOWED_HOSTS = [h.strip() for h in env("CUSTOM_URL_ALLOWED_HOSTS").split(",") if h.strip()]

MANUS_BASE = env("MANUS_BASE_URL", "https://api.manus.ai").rstrip("/")
MANUS_SHARED_KEY = env("MANUS_API_KEY") or env("MANUS_SHARED_API_KEY")
MANUS_PROFILE = env("MANUS_DEFAULT_PROFILE", "standard").lower()
MANUS_LOCALE = env("MANUS_LOCALE")  # leave empty: invalid codes such as "fa" are rejected
MANUS_TIMEOUT = env_int("MANUS_TIMEOUT_SEC", 90)
MANUS_POLL = env_float("MANUS_POLL_INTERVAL", 4)
MANUS_WORKERS = max(1, env_int("MANUS_MAX_CONCURRENT", 2))
MANUS_QUEUE_MAX = max(1, env_int("MANUS_QUEUE_MAX", 15))
MANUS_FOLLOWUP_TTL = env_int("MANUS_FOLLOWUP_TTL_SEC", 7200)
MANUS_STALE = env_int("MANUS_STALE_LOCK_SEC", MANUS_TIMEOUT + 90)
MANUS_TRUSTED = [h.strip() for h in env("MANUS_TRUSTED_HOSTS", "manus.ai,manus.im,manuscdn.com").split(",") if h.strip()]
MANUS_MAX_MB = max(1, env_int("MANUS_MAX_DOWNLOAD_MB", 25))

PROMPTOPIA_URL = env("PROMPTOPIA_URL", "https://mohsen-niksirat.github.io/promptopia/")
PROMPTOPIA_LABEL = env("PROMPTOPIA_LABEL", "📚 پرامپت‌های آماده")

SYSTEM_PROMPT = (
    "You are a helpful AI assistant on Telegram. "
    "Answer clearly. Use short paragraphs, lists, and code blocks when useful."
)

# provider → name, base url, where to get a key, [(model_id, label)]
PROVIDERS: Dict[str, dict] = {
    "dahl": dict(
        name="Dahl", url="https://inference.dahl.global/v1", key_url="https://inference.dahl.global/account",
        models=[("zai-org/GLM-5.3-Flash", "GLM-5.3-Flash"),
                ("deepseek-ai/DeepSeek-V4-Flash-0731", "DeepSeek-V4-Flash"),
                ("MiniMaxAI/MiniMax-M2.7", "MiniMax-M2.7")]),
    "openrouter": dict(
        name="OpenRouter", url="https://openrouter.ai/api/v1", key_url="https://openrouter.ai/keys",
        models=[("meta-llama/llama-3.3-70b-instruct:free", "Llama 3.3 70B (free)"),
                ("google/gemini-2.0-flash-exp:free", "Gemini Flash (free)")]),
    "groq": dict(
        name="Groq", url="https://api.groq.com/openai/v1", key_url="https://console.groq.com/keys",
        models=[("llama-3.3-70b-versatile", "Llama 3.3 70B"), ("llama-3.1-8b-instant", "Llama 3.1 8B")]),
    "cerebras": dict(
        name="Cerebras", url="https://api.cerebras.ai/v1", key_url="https://cloud.cerebras.ai/",
        models=[("llama-3.3-70b", "Llama 3.3 70B")]),
    "siliconflow": dict(
        name="SiliconFlow", url="https://api.siliconflow.cn/v1", key_url="https://cloud.siliconflow.cn/account/ak",
        models=[("Qwen/Qwen2.5-7B-Instruct", "Qwen2.5 7B")]),
    "mistral": dict(
        name="Mistral", url="https://api.mistral.ai/v1", key_url="https://console.mistral.ai/api-keys",
        models=[("mistral-small-latest", "Mistral Small")]),
    "github": dict(
        name="GitHub Models", url="https://models.github.ai/inference", key_url="https://github.com/marketplace/models",
        models=[("openai/gpt-4o", "GPT-4o")]),
    "custom": dict(name="Custom (OpenAI-compatible)", url=None, key_url=None, models=[]),
}
KEY_PROVIDERS = list(PROVIDERS) + ["manus"]  # providers a user can store a key for
MANUS_KEY_URL = "https://manus.im/app#settings/developers"
DEFAULT_MODEL = "dahl|zai-org/GLM-5.3-Flash"
STYLES = {"html": "HTML (پیشنهادی)", "markdown": "Markdown", "plain": "متن ساده"}
CONTEXT_CHOICES = [5, 10, 20, 40]

# ════════════════════════════════════════════════════════════════════════════
# Startup, logging, bot
# ════════════════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
install_log_redaction([BOT_TOKEN, DAHL_API_KEY, SUPABASE_KEY, MANUS_SHARED_KEY, KEY_ENCRYPTION_SECRET])
VAULT = KeyVault(KEY_ENCRYPTION_SECRET)


def check_env() -> None:
    missing = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_KEY", SUPABASE_KEY)) if not v]
    if missing:
        raise SystemExit("Missing required env vars: " + ", ".join(missing))
    try:  # warn when SUPABASE_KEY is not a service_role JWT
        part = SUPABASE_KEY.split(".")[1]
        role = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("role")
        if role and role != "service_role":
            logging.warning("SUPABASE_KEY role is %r — use the service_role key (server-side only).", role)
    except Exception:
        pass


if __name__ == "__main__" or BOT_TOKEN:
    check_env()
bot = telebot.TeleBot(BOT_TOKEN or "0:placeholder", num_threads=8)
bot.timeout = 30


# ════════════════════════════════════════════════════════════════════════════
# Small utilities
# ════════════════════════════════════════════════════════════════════════════


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def mask(key: str) -> str:
    key = key or ""
    return "*" * len(key) if len(key) <= 12 else f"{key[:4]}…{key[-4:]}"


class TTL:
    """Tiny thread-safe cache with per-entry expiry."""

    def __init__(self, ttl: float):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, k):
        with self._lock:
            hit = self._d.get(k)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            self._d.pop(k, None)
        return None

    def set(self, k, v):
        with self._lock:
            if len(self._d) > 5000:
                self._d.clear()
            self._d[k] = (time.time(), v)

    def drop(self, k):
        with self._lock:
            self._d.pop(k, None)


# ════════════════════════════════════════════════════════════════════════════
# Supabase layer (PostgREST)
# ════════════════════════════════════════════════════════════════════════════

_http = httpx.Client(timeout=10.0, limits=httpx.Limits(max_connections=30, max_keepalive_connections=15))


def sb(method: str, path: str, *, json_body=None, prefer: Optional[str] = None, timeout: float = 10.0) -> httpx.Response:
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    return _http.request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=json_body, timeout=timeout)


def sb_ok(method: str, path: str, **kw) -> bool:
    try:
        r = sb(method, path, **kw)
        if r.status_code >= 400:
            logging.error("supabase %s %s -> %s %s", method, path.split("?")[0], r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        logging.error("supabase %s %s failed: %s", method, path.split("?")[0], e)
        return False


def sb_rows(path: str) -> List[dict]:
    try:
        r = sb("GET", path)
        if r.status_code >= 400:
            logging.error("supabase GET %s -> %s %s", path.split("?")[0], r.status_code, r.text[:200])
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logging.error("supabase GET %s failed: %s", path.split("?")[0], e)
        return []


def sb_count(path: str) -> int:
    try:
        sep = "&" if "?" in path else "?"
        r = sb("GET", f"{path}{sep}select=*&limit=1", prefer="count=exact")
        total = (r.headers.get("content-range") or "").split("/")[-1]
        return int(total) if total.isdigit() else 0
    except Exception:
        return 0


# ── users ──────────────────────────────────────────────────────────────────
_users = TTL(30)


def user_get(uid: int) -> dict:
    u = _users.get(uid)
    if u is None:
        rows = sb_rows(f"users?telegram_id=eq.{int(uid)}&select=*")
        u = rows[0] if rows else {}
        _users.set(uid, u)
    return u


def user_set(uid: int, **fields) -> bool:
    ok = sb_ok("PATCH", f"users?telegram_id=eq.{int(uid)}", json_body=fields, prefer="return=minimal")
    _users.drop(uid)
    return ok


def ensure_user(u) -> None:
    cur = user_get(u.id)
    if not cur:
        sb_ok("POST", "users", prefer="resolution=ignore-duplicates,return=minimal", json_body={
            "telegram_id": u.id, "username": u.username, "first_name": u.first_name,
            "selected_model": DEFAULT_MODEL, "last_active_at": now_iso()})
        _users.drop(u.id)
        return
    try:
        last = datetime.fromisoformat(str(cur.get("last_active_at")).replace("Z", "+00:00"))
        fresh = (datetime.now(timezone.utc) - last).total_seconds() < 600
    except Exception:
        fresh = False
    if not fresh:
        user_set(u.id, username=u.username, first_name=u.first_name, last_active_at=now_iso())


def parse_model(ref: str) -> Tuple[str, str]:
    ref = (ref or "").strip() or DEFAULT_MODEL
    if "|" in ref:
        p, m = ref.split("|", 1)
        return p.strip(), m.strip()
    return "dahl", ref  # legacy rows stored a bare model id


def settings(uid: int) -> dict:
    u = user_get(uid)
    stream = u.get("stream_enabled")
    return {
        "style": u.get("response_style") or "html",
        "ctx": u.get("context_messages"),
        "model": u.get("selected_model") or DEFAULT_MODEL,
        "stream": STREAMING_ENABLED if stream is None else bool(stream),
        "mode": u.get("bot_mode") or "chat",
    }


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or bool(user_get(uid).get("is_admin"))


# ── BYOK keys ───────────────────────────────────────────────────────────────
_keys = TTL(30)


def keys_get(uid: int) -> Dict[str, dict]:
    ks = _keys.get(uid)
    if ks is None:
        ks = {}
        for r in sb_rows(f"user_api_keys?user_id=eq.{int(uid)}&is_active=eq.true&select=provider,api_key,base_url"):
            r["api_key"] = VAULT.decrypt(r.get("api_key") or "")
            ks[r["provider"]] = r
        _keys.set(uid, ks)
    return ks


def key_set(uid: int, provider: str, api_key: str, base_url: Optional[str] = None) -> bool:
    ok = sb_ok("POST", "user_api_keys?on_conflict=user_id,provider", prefer="resolution=merge-duplicates,return=minimal",
               json_body={"user_id": uid, "provider": provider, "api_key": VAULT.encrypt(api_key), "base_url": base_url,
                          "is_active": True, "updated_at": now_iso()})
    _keys.drop(uid)
    return ok


def key_del(uid: int, provider: str) -> bool:
    ok = sb_ok("DELETE", f"user_api_keys?user_id=eq.{int(uid)}&provider=eq.{q(provider)}")
    _keys.drop(uid)
    return ok


# ── quota ───────────────────────────────────────────────────────────────────
def quota_view(row: dict) -> dict:
    dl = int(row.get("daily_limit") or DAILY_QUOTA)
    used = 0 if str(row.get("reset_at") or "") < today() else int(row.get("used_today") or 0)
    ll = int(row.get("lifetime_limit") or LIFETIME_QUOTA)
    lu = int(row.get("lifetime_used") or 0)
    return {"dl": dl, "used": used, "d_over": dl > 0 and used >= dl,
            "ll": ll, "lu": lu, "l_over": ll > 0 and lu >= ll, "l_pct": (lu / ll * 100) if ll > 0 else 0.0}


def quota_get(uid: int) -> dict:
    rows = sb_rows(f"user_quotas?user_id=eq.{int(uid)}&select=*")
    if rows:
        return quota_view(rows[0])
    row = {"user_id": uid, "daily_limit": DAILY_QUOTA, "used_today": 0, "reset_at": today(),
           "lifetime_limit": LIFETIME_QUOTA, "lifetime_used": 0}
    sb_ok("POST", "user_quotas", prefer="resolution=ignore-duplicates,return=minimal", json_body=row)
    return quota_view(row)


_bump_warned = False


def quota_add(uid: int, tokens: int) -> Optional[dict]:
    """Atomic via sql/06 bump_quota(); falls back to read-modify-write if missing."""
    global _bump_warned
    if tokens <= 0:
        return None
    try:
        r = sb("POST", "rpc/bump_quota", json_body={"p_user_id": uid, "p_tokens": int(tokens),
                                                    "p_default_daily": DAILY_QUOTA, "p_default_lifetime": LIFETIME_QUOTA})
        if r.status_code < 400 and r.json():
            o = r.json()[0]
            return quota_view({"daily_limit": o["o_daily_limit"], "used_today": o["o_used_today"], "reset_at": today(),
                               "lifetime_limit": o["o_lifetime_limit"], "lifetime_used": o["o_lifetime_used"]})
        if not _bump_warned:
            _bump_warned = True
            logging.warning("bump_quota() unavailable (%s) — run sql/06. Using non-atomic fallback.", r.status_code)
    except Exception as e:
        logging.error("bump_quota failed: %s", e)
    v = quota_get(uid)
    sb_ok("PATCH", f"user_quotas?user_id=eq.{int(uid)}", prefer="return=minimal", json_body={
        "used_today": v["used"] + tokens, "reset_at": today(), "lifetime_used": v["lu"] + tokens, "updated_at": now_iso()})
    return quota_get(uid)


def quota_check(uid: int, source: str) -> Tuple[bool, dict, str]:
    v = quota_get(uid)
    if v["l_over"]:
        return False, v, "lifetime"
    if (source == "shared" or QUOTA_ON_OWN_KEYS) and v["d_over"]:
        return False, v, "daily"
    return True, v, ""


def quota_line(v: dict) -> str:
    return f"📊 امروز <code>{v['used']:,}</code>/<code>{v['dl']:,}</code> · کل <code>{v['lu']:,}</code>/<code>{v['ll']:,}</code>"


# ── conversations (only the active one is kept) ─────────────────────────────
_verified: set = set()


def conv_new(uid: int) -> str:
    cid = str(uuid.uuid4())
    sb_ok("POST", "conversations", prefer="return=minimal", json_body={
        "id": cid, "user_id": uid, "title": "گفتگوی جدید", "model": settings(uid)["model"],
        "created_at": now_iso(), "updated_at": now_iso()})
    user_set(uid, active_conversation_id=cid)
    _verified.add(cid)
    old = [r["id"] for r in sb_rows(f"conversations?user_id=eq.{int(uid)}&id=neq.{q(cid)}&select=id")]
    if old:  # purge previous conversations (storage policy)
        ids = ",".join(q(i) for i in old)
        sb_ok("DELETE", f"messages?conversation_id=in.({ids})")
        sb_ok("DELETE", f"conversations?id=in.({ids})")
    return cid


def conv_active(uid: int) -> str:
    cid = user_get(uid).get("active_conversation_id")
    if cid and cid not in _verified:
        if sb_rows(f"conversations?id=eq.{q(cid)}&select=id"):
            _verified.add(cid)
        else:
            cid = None
    return cid or conv_new(uid)


def conv_history(cid: str, n: int) -> List[dict]:
    rows = sb_rows(f"messages?conversation_id=eq.{q(cid)}&order=id.desc&limit={int(n)}&select=role,content")
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)
            if r.get("role") in ("user", "assistant") and (r.get("content") or "").strip()]


def msg_save(cid: str, role: str, content: str, in_t: int = 0, out_t: int = 0) -> None:
    sb_ok("POST", "messages", prefer="return=minimal", json_body={
        "conversation_id": cid, "role": role, "content": content,
        "input_tokens": in_t, "output_tokens": out_t, "created_at": now_iso()})


def msg_trim(cid: str) -> None:
    extra = sb_rows(f"messages?conversation_id=eq.{q(cid)}&order=id.desc&offset={KEEP_MESSAGES}&limit=200&select=id")
    if extra:
        sb_ok("DELETE", f"messages?id=in.({','.join(str(r['id']) for r in extra)})")


# ── saved prompts ───────────────────────────────────────────────────────────
def prompts_list(uid: int) -> List[dict]:
    return sb_rows(f"user_prompts?user_id=eq.{int(uid)}&order=name.asc&select=id,name,prompt")


def prompt_save(uid: int, name: str, text: str) -> bool:
    return sb_ok("POST", "user_prompts?on_conflict=user_id,name", prefer="resolution=merge-duplicates,return=minimal",
                 json_body={"user_id": uid, "name": name[:64], "prompt": text[:8000], "updated_at": now_iso()})


# ════════════════════════════════════════════════════════════════════════════
# Telegram helpers: rendering, sending, editing
# ════════════════════════════════════════════════════════════════════════════


def md_to_tg_html(text: str) -> str:
    b = html.escape(text, quote=False)
    b = re.sub(r"```(?:\w*)\n?(.*?)```", lambda m: f"<pre>{m.group(1).strip(chr(10))}</pre>", b, flags=re.S)
    b = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", b, flags=re.S)
    b = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", b)
    b = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", b)
    return re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", b, flags=re.M)


def render(text: str, style: str) -> Tuple[str, Optional[str]]:
    if style == "plain":
        return text, None
    if style == "markdown":
        return text, "Markdown"
    if style == "ui":  # already-safe HTML written by the bot itself
        return text, "HTML"
    return md_to_tg_html(text), "HTML"


def split_text(text: str, limit: int = 3500) -> List[str]:
    text, out = (text or "").strip() or "…", []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    for i in range(len(out) - 1):  # keep ``` fences balanced across chunks
        if out[i].count("```") % 2:
            out[i] += "\n```"
            out[i + 1] = "```\n" + out[i + 1]
    return out


def _parse_err(e: Exception) -> bool:
    s = str(e).lower()
    return "parse" in s or "entities" in s


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"</?[a-z][^>]*>", "", text))


def tg_send(chat_id: int, text: str, style: str = "ui", reply_to: Optional[int] = None, markup=None):
    body, mode = render(text, style)
    kw: dict = {"reply_markup": markup}
    if reply_to:
        kw.update(reply_to_message_id=reply_to, allow_sending_without_reply=True)
    try:
        return bot.send_message(chat_id, body, parse_mode=mode, **kw)
    except ApiTelegramException as e:
        if not _parse_err(e):
            raise
    return bot.send_message(chat_id, _plain(body) if mode == "HTML" else text, **kw)


def tg_edit(chat_id: int, mid: int, text: str, style: str = "ui", markup=None) -> bool:
    body, mode = render(text, style)
    for b, m in ((body, mode), (_plain(body) if mode == "HTML" else text, None)):
        try:
            bot.edit_message_text(b, chat_id, mid, parse_mode=m, reply_markup=markup)
            return True
        except ApiTelegramException as e:
            if "not modified" in str(e).lower():
                return True
            if not _parse_err(e):
                logging.warning("edit failed: %s", str(e)[:150])
                return False
    return False


def send_long(chat_id: int, text: str, style: str, reply_to: Optional[int] = None, edit_mid: Optional[int] = None) -> None:
    chunks = split_text(text)
    for i, ch in enumerate(chunks):
        if i == 0 and edit_mid:
            tg_edit(chat_id, edit_mid, ch, style)
        else:
            tg_send(chat_id, ch, style, reply_to if i == 0 else None)


def kb(*rows: List[Tuple[str, str]]) -> Kb:
    m = Kb()
    for r in rows:
        m.row(*[Btn(t, url=d) if d.startswith("http") else Btn(t, callback_data=d) for t, d in r])
    return m


def guard(fn):
    @functools.wraps(fn)
    def wrapper(arg, *a, **k):
        try:
            return fn(arg, *a, **k)
        except Exception:
            logging.exception("handler %s failed", fn.__name__)
            try:
                msg = getattr(arg, "message", None) or arg
                bot.send_message(msg.chat.id, "❌ خطای داخلی. لطفاً دوباره تلاش کنید.")
            except Exception:
                pass
    return wrapper


# ── per-user state kept in memory ───────────────────────────────────────────
PENDING: Dict[int, dict] = {}      # multi-step input flows (expire after 10 min)
PICKED: Dict[int, str] = {}        # saved prompt chosen for the next Manus photo
_locks: Dict[int, threading.Lock] = {}
_warned: set = set()


def user_lock(uid: int) -> threading.Lock:
    return _locks.setdefault(uid, threading.Lock())


def pend_set(uid: int, action: str, **kw) -> None:
    PENDING[uid] = {"action": action, "exp": time.time() + 600, **kw}


def pend_get(uid: int) -> Optional[dict]:
    p = PENDING.get(uid)
    if p and p["exp"] < time.time():
        PENDING.pop(uid, None)
        return None
    return p


# ════════════════════════════════════════════════════════════════════════════
# LLM (OpenAI-compatible, optional streaming)
# ════════════════════════════════════════════════════════════════════════════


def creds(uid: int) -> Optional[dict]:
    prov, model = parse_model(settings(uid)["model"])
    row = keys_get(uid).get(prov)
    default_url = (PROVIDERS.get(prov) or {}).get("url")
    if row and row.get("api_key"):
        base = (row.get("base_url") or default_url or "").rstrip("/")
        if not base:
            return None
        if base != (default_url or "").rstrip("/"):  # custom endpoint → SSRF check on every use
            ok, why = validate_public_https_url(base, allow_private=CUSTOM_ALLOW_PRIVATE, allowed_hosts=CUSTOM_ALLOWED_HOSTS)
            if not ok:
                logging.warning("blocked custom endpoint for %s: %s", uid, why)
                return None
        return {"provider": prov, "model": model, "base": base, "key": row["api_key"], "source": "user"}
    if ALLOW_SHARED_KEY and prov == "dahl" and DAHL_API_KEY:
        return {"provider": prov, "model": model, "base": default_url, "key": DAHL_API_KEY, "source": "shared"}
    return None


def call_llm(c: dict, messages: list, stream: bool, on_partial: Optional[Callable[[str], None]] = None) -> dict:
    headers = {"Authorization": f"Bearer {c['key']}", "Content-Type": "application/json"}
    url = f"{c['base']}/chat/completions"
    payload: dict = {"model": c["model"], "messages": messages, "stream": stream}
    timeout = httpx.Timeout(120.0, connect=15.0)
    if not stream:
        r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        u = d.get("usage") or {}
        pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        return {"text": d["choices"][0]["message"]["content"] or "", "pt": pt, "ct": ct,
                "total": int(u.get("total_tokens") or pt + ct)}
    payload["stream_options"] = {"include_usage": True}
    parts: List[str] = []
    usage: dict = {}
    last = 0.0
    with httpx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as r:
        if r.status_code >= 400:
            body = r.read().decode("utf-8", "replace")[:300]
            raise httpx.HTTPStatusError(f"{r.status_code}: {body}", request=r.request, response=r)
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = chunk.get("usage") or usage
            ch = (chunk.get("choices") or [{}])[0]
            piece = (ch.get("delta") or {}).get("content") or ""
            if piece:
                parts.append(piece)
                if on_partial and time.time() - last >= STREAM_EDIT_INTERVAL:
                    last = time.time()
                    on_partial("".join(parts))
    text = "".join(parts)
    pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    if not usage:  # provider omitted usage in the stream → rough estimate
        pt, ct = sum(len(m.get("content") or "") for m in messages) // 4, max(len(text) // 4, 1)
    return {"text": text, "pt": pt, "ct": ct, "total": int(usage.get("total_tokens") or pt + ct)}


def llm_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hint = {401: "کلید نامعتبر است.", 403: "کلید دسترسی ندارد.", 402: "اعتبار حساب کافی نیست.",
                404: "مدل یا آدرس پیدا نشد.", 429: "محدودیت نرخ؛ کمی بعد تلاش کنید."}.get(code)
        return f"❌ خطای سرویس ({code}): {hint or 'مشکل موقت سرویس.'}"
    if isinstance(e, httpx.TimeoutException):
        return "❌ زمان پاسخ سرویس تمام شد."
    return "❌ ارتباط با سرویس مدل برقرار نشد."


# ════════════════════════════════════════════════════════════════════════════
# Chat flow
# ════════════════════════════════════════════════════════════════════════════

NO_KEY_TEXT = ("🔑 برای این مدل کلید API ندارید.\nاز «کلیدها» کلید خودتان را ثبت کنید، "
               "یا مدل دیگری انتخاب کنید.")


def chat_flow(m) -> None:
    uid, text = m.from_user.id, (m.text or "").strip()
    if not text:
        return
    lock = user_lock(uid)
    if not lock.acquire(blocking=False):
        tg_send(m.chat.id, "⏳ پاسخ قبلی هنوز در حال ساخت است.", reply_to=m.message_id)
        return
    try:
        _chat(m, uid, text)
    finally:
        lock.release()


def _chat(m, uid: int, text: str) -> None:
    chat = m.chat.id
    s, c = settings(uid), creds(uid)
    if not c:
        tg_send(chat, NO_KEY_TEXT, reply_to=m.message_id, markup=kb([("🔑 کلیدها", "keys"), ("🧠 مدل", "sm")]))
        return
    ok, qv, why = quota_check(uid, c["source"])
    if not ok:
        tg_send(chat, "⛔️ سهمیه " + ("کل حساب" if why == "lifetime" else "امروز") + " تمام شده است.\n" + quota_line(qv),
                reply_to=m.message_id, markup=kb([("🔑 کلید شخصی", "keys")]))
        return
    cid = conv_active(uid)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + conv_history(cid, s["ctx"] or HISTORY_LIMIT) \
        + [{"role": "user", "content": text}]
    bot.send_chat_action(chat, "typing")
    status = tg_send(chat, "⏳ …", style="plain", reply_to=m.message_id)

    def partial(buf: str) -> None:
        tg_edit(chat, status.message_id, buf[-3500:] + " ▌", "plain")

    try:
        try:
            res = call_llm(c, msgs, s["stream"], partial if s["stream"] else None)
        except Exception as e:
            if not s["stream"]:
                raise
            logging.warning("stream failed (%s) — retrying without streaming", type(e).__name__)
            res = call_llm(c, msgs, False)
    except Exception as e:
        logging.error("LLM error: %s", str(e)[:300])
        tg_edit(chat, status.message_id, llm_error(e), "plain")
        return

    answer = res["text"].strip() or "(پاسخ خالی)"
    msg_save(cid, "user", text, res["pt"], 0)
    msg_save(cid, "assistant", answer, 0, res["ct"])
    msg_trim(cid)
    q2 = quota_add(uid, res["total"])
    foot = f"\n\n—\n{res['total']:,} توکن" if s["style"] == "plain" else f"\n\n—\n`{res['total']:,}` توکن"
    send_long(chat, answer + foot, s["style"], m.message_id, edit_mid=status.message_id)
    if q2 and not q2["l_over"] and q2["l_pct"] >= LIFETIME_WARN_PCT and uid not in _warned:
        _warned.add(uid)
        tg_send(chat, f"⚠️ {q2['l_pct']:.0f}% از سهمیه کل حساب مصرف شده.\n" + quota_line(q2))


# ════════════════════════════════════════════════════════════════════════════
# Manus agent
# ════════════════════════════════════════════════════════════════════════════

TERMINAL = ("stopped", "error", "waiting", "completed")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff")
DOC_EXT = (".pdf", ".md", ".zip", ".docx", ".mp4", ".webm")
HOST_HINTS = ("manuscdn.com", "files.manus", "manus.im", "amazonaws.com", "s3.", "blob.core.windows.net", "googleusercontent.com")
PATH_HINTS = ("/file", "/asset", "/media", "/upload", "/download", "/cdn", "image", "png", "jpg", "webp")
URL_KEYS = ("fileUrl", "file_url", "url", "download_url", "downloadUrl", "src", "href", "image_url", "imageUrl",
            "file_url_signed", "public_url")
INPUT_HINTS = ("telegram_photo", "telegram_document", "/input/", "input_image", "source_image", "original_image",
               "upload", "attachment")
SESSIONS: Dict[int, dict] = {}   # uid -> {task_id, url, t, delivered:set}


def manus_key(uid: int) -> Optional[str]:
    row = keys_get(uid).get("manus")
    return (row or {}).get("api_key") or MANUS_SHARED_KEY or None


def manus_session(uid: int) -> Optional[dict]:
    s = SESSIONS.get(uid)
    if s and time.time() - s["t"] > MANUS_FOLLOWUP_TTL:
        SESSIONS.pop(uid, None)
        return None
    return s


def manus_call(method: str, path: str, key: str, payload: Optional[dict] = None, params: Optional[dict] = None) -> Any:
    hdrs = {"x-manus-api-key": key, "Content-Type": "application/json"}
    r = httpx.request(method, f"{MANUS_BASE}/v2/{path}", headers=hdrs, json=payload, params=params, timeout=60.0)
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        err = (data.get("error") if isinstance(data, dict) else None) or {}
        raise RuntimeError(f"{path} {r.status_code}: {err.get('code') or ''} {err.get('message') or r.text[:150]}".strip())
    return data


def manus_parts(prompt: str, attachment: Optional[dict]) -> list:
    parts: list = [{"type": "text", "text": (prompt or "").strip()[:4800]}]
    if attachment:
        parts.append(attachment)
    return parts


def is_media_url(u: str) -> bool:
    u = (u or "").lower()
    if not u.startswith("http") or any(x in u for x in ("avatar", "favicon", "icon-32", "logo-light")):
        return False
    if any(e in u for e in IMG_EXT + DOC_EXT):
        return True
    return any(h in u for h in HOST_HINTS) and any(p in u for p in PATH_HINTS)


def walk(obj, files: list, texts: list, depth: int = 0) -> None:
    """Deep-scan a Manus JSON payload for downloadable files and assistant text."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        t = obj.get("text")
        if isinstance(t, str) and len(t.strip()) > 40 and not t.startswith("http"):
            texts.append(t.strip())
        for k in URL_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and is_media_url(v):
                name = obj.get("fileName") or obj.get("filename") or obj.get("name") or re.sub(r"[^\w.\-]+", "_", v.split("?")[0].rstrip("/").split("/")[-1])[:80] or "manus-output"
                files.append({"url": v, "name": str(name)[:80], "mime": str(obj.get("mimeType") or obj.get("mime_type") or "")})
        for v in obj.values():
            walk(v, files, texts, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, files, texts, depth + 1)
    elif isinstance(obj, str):
        for u in re.findall(r"https?://[^\s)\"']+", obj):
            if is_media_url(u):
                files.append({"url": u, "name": re.sub(r"[^\w.\-]+", "_", u.split("?")[0].rstrip("/").split("/")[-1])[:80] or "manus-output", "mime": ""})


def is_image(f: dict) -> bool:
    blob = f"{f.get('mime', '')} {f.get('url', '')} {f.get('name', '')}".lower()
    return "image/" in blob or any(e in blob for e in IMG_EXT)


def manus_parse(items: list) -> dict:
    status, error, texts, files = None, None, [], []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        su = it.get("status_update")
        if isinstance(su, dict):
            status = su.get("agent_status") or status
        err = it.get("error_message")
        if err:
            error = err.get("message") if isinstance(err, dict) else str(err)
        am = it.get("assistant_message")
        if isinstance(am, dict):
            c = am.get("content")
            if isinstance(c, str) and c.strip() and not c.startswith("http"):
                texts.append(c.strip())
            elif isinstance(c, list):
                texts += [str(p["text"]).strip() for p in c if isinstance(p, dict) and p.get("text")]
        walk(it, files, texts)
    seen, uniq = set(), []
    for f in files:
        if f["url"] not in seen:
            seen.add(f["url"])
            uniq.append(f)
    uniq.sort(key=lambda f: 0 if is_image(f) else 1)
    texts = list(dict.fromkeys(t for t in texts if t))
    return {"status": status, "error": error, "text": "\n\n".join(texts[-3:]), "files": uniq}


def manus_messages(key: str, task_id: str) -> list:
    try:
        d = manus_call("GET", "task.listMessages", key, params={"task_id": task_id, "order": "desc", "limit": 50})
    except Exception as e:
        logging.warning("listMessages: %s", str(e)[:150])
        return []
    if isinstance(d, list):
        return d
    for k in ("messages", "data", "events", "items", "result"):
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for sub in ("messages", "data", "events", "items"):
                if isinstance(v.get(sub), list):
                    return v[sub]
    return []


def manus_wait(key: str, task_id: str, on_progress: Callable[[str, int], None]) -> dict:
    start = time.time()
    deadline = start + MANUS_TIMEOUT
    late_until = None
    res = {"status": "running", "text": "", "files": [], "error": None, "timed_out": False}
    while True:
        p = manus_parse(manus_messages(key, task_id))
        res["status"] = p["status"] or res["status"]
        res["error"] = p["error"] or res["error"]
        res["text"] = p["text"] or res["text"]
        have = {f["url"] for f in res["files"]}
        res["files"] += [f for f in p["files"] if f["url"] not in have]
        on_progress(res["status"], int(time.time() - start))
        if res["status"] in TERMINAL:
            if res["files"] or res["status"] in ("error", "waiting"):
                break
            late_until = late_until or time.time() + 75  # images can appear shortly after the agent stops
            if time.time() > late_until:
                break
        elif time.time() > deadline:
            res["timed_out"] = True
            break
        time.sleep(MANUS_POLL)
    if not res["files"]:  # last resort: task.detail
        try:
            files: list = []
            walk(manus_call("GET", "task.detail", key, params={"task_id": task_id}), files, [])
            res["files"] = files
        except Exception as e:
            logging.warning("task.detail: %s", str(e)[:150])
    return res


def manus_trusted(url: str) -> bool:
    hosts = list(MANUS_TRUSTED)
    try:
        hosts.append(httpx.URL(MANUS_BASE).host)
    except Exception:
        pass
    return is_trusted_host(url, hosts)


def manus_fetch(url: str, key: str) -> Optional[bytes]:
    """Download with manual redirects; the API key only goes to trusted Manus hosts."""
    cap, current = MANUS_MAX_MB * 1024 * 1024, url
    with httpx.Client(timeout=60.0, follow_redirects=False) as cl:
        for _ in range(5):
            ok, why = validate_public_https_url(current, use_cache=False)
            if not ok:
                raise ValueError(f"blocked url ({why})")
            h = {"User-Agent": f"AIDahl/{VERSION}"}
            if key and manus_trusted(current):
                h.update({"x-manus-api-key": key, "Authorization": f"Bearer {key}"})
            with cl.stream("GET", current, headers=h) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = str(httpx.URL(current).join(r.headers["location"]))
                    continue
                if r.status_code != 200:
                    return None
                if int(r.headers.get("content-length") or 0) > cap:
                    raise ValueError("file too large")
                buf = bytearray()
                for ch in r.iter_bytes():
                    buf += ch
                    if len(buf) > cap:
                        raise ValueError("file too large")
                return bytes(buf) if len(buf) > 100 else None
    return None


def manus_deliver(chat: int, key: str, files: list, sess: dict) -> int:
    fresh = [f for f in files if f["url"] not in sess["delivered"]]
    strict = [f for f in fresh if not any(h in f"{f['name']} {f['url']} {f['mime']}".lower() for h in INPUT_HINTS)]
    pick = (strict or fresh)[:3]
    sent = 0
    for f in pick:
        try:
            data = manus_fetch(f["url"], key)
        except Exception as e:
            logging.warning("manus download failed: %s", str(e)[:150])
            continue
        if not data:
            continue
        name = f["name"] or "manus-output"
        try:
            if is_image(f):
                if not name.lower().endswith(IMG_EXT):
                    name += ".png"
                if len(data) < 10 * 1024 * 1024:
                    bot.send_photo(chat, data)
                bot.send_document(chat, (name, data), caption="📎 فایل اصلی")
            else:
                bot.send_document(chat, (name, data))
            sess["delivered"].add(f["url"])
            sent += 1
        except Exception as e:
            logging.warning("telegram send failed: %s", str(e)[:150])
    return sent


def run_manus(uid: int, chat: int, reply_to: int, prompt: str, attachment: Optional[dict]) -> None:
    key = manus_key(uid)
    status = tg_send(chat, "🎨 ارسال به Manus…", reply_to=reply_to)
    mid = status.message_id
    sess = manus_session(uid)
    try:
        if sess:
            task_id = sess["task_id"]
            manus_call("POST", "task.sendMessage", key, {
                "task_id": task_id,
                "message": {"content": manus_parts(prompt or "Continue editing based on the previous result and briefly describe the change.", attachment)},
                **({"locale": MANUS_LOCALE} if MANUS_LOCALE else {})})
        else:
            profile = MANUS_PROFILE if MANUS_PROFILE in ("standard", "lite", "max") else "standard"
            d = manus_call("POST", "task.create", key, {
                "message": {"content": manus_parts(prompt or "Edit/improve this image. Keep the main subject, return a polished result and briefly describe the changes.", attachment)},
                "agent_profile": profile, "hide_in_task_list": False, "interactive_mode": False,
                **({"locale": MANUS_LOCALE} if MANUS_LOCALE else {})})
            task_id = d.get("task_id")
            if not task_id:
                raise RuntimeError("task.create returned no task_id")
            sess = SESSIONS[uid] = {"task_id": task_id, "url": d.get("task_url") or f"https://manus.im/app/{task_id}",
                                    "t": time.time(), "delivered": set()}
    except Exception as e:
        logging.error("manus start failed: %s", str(e)[:300])
        low = str(e).lower()
        hint = "کلید نامعتبر است." if "401" in low else "اعتبار (credits) کافی نیست." if "402" in low else "درخواست ناموفق بود."
        tg_edit(chat, mid, f"❌ Manus: {hint}\n<code>{esc(str(e)[:200])}</code>")
        return

    last_edit = [0.0]

    def progress(st: str, secs: int) -> None:
        if time.time() - last_edit[0] > 8:
            last_edit[0] = time.time()
            tg_edit(chat, mid, f"🎨 Manus در حال کار… <code>{esc(st)}</code> · {secs}s\n<a href=\"{esc(sess['url'])}\">لینک تسک</a>")

    res = manus_wait(key, task_id, progress)
    sess["t"] = time.time()
    sent = manus_deliver(chat, key, res["files"], sess)
    if res["text"]:
        send_long(chat, res["text"], "html", reply_to)
    link = f"\n<a href=\"{esc(sess['url'])}\">لینک تسک</a>"
    if res["error"]:
        tg_edit(chat, mid, f"⚠️ خطای Manus: <code>{esc(str(res['error'])[:300])}</code>{link}")
    elif res["timed_out"] and not sent:
        tg_edit(chat, mid, f"⏳ هنوز تمام نشده؛ در لینک تسک ادامه دهید.{link}")
    elif sent:
        tg_edit(chat, mid, f"✅ انجام شد. برای ویرایش بیشتر پیام/عکس بعدی را بفرستید.{link}",
                markup=kb([("🆕 تسک جدید", "mn"), ("✖️ پایان", "mx")]))
    else:
        tg_edit(chat, mid, f"ℹ️ خروجی فایل پیدا نشد.{link}", markup=kb([("🆕 تسک جدید", "mn")]))


# ── job queue ───────────────────────────────────────────────────────────────
_pool = ThreadPoolExecutor(max_workers=MANUS_WORKERS, thread_name_prefix="manus")
_jobs: Dict[int, dict] = {}
_jobs_lock = threading.Lock()


def manus_submit(uid: int, fn: Callable[[], None]) -> str:
    """Returns '' when accepted, otherwise a user-facing refusal."""
    with _jobs_lock:
        for u, j in list(_jobs.items()):  # reap hung jobs
            if j["state"] == "running" and time.time() - j["t"] > MANUS_STALE + MANUS_TIMEOUT + 75:
                _jobs.pop(u, None)
        if uid in _jobs:
            return "⏳ یک درخواست Manus از شما در حال اجراست."
        if len(_jobs) >= MANUS_WORKERS + MANUS_QUEUE_MAX:
            return "🚦 صف Manus پر است؛ کمی بعد تلاش کنید."
        _jobs[uid] = {"state": "queued", "t": time.time()}

    def wrapped() -> None:
        with _jobs_lock:
            if uid not in _jobs:  # reset while queued
                return
            _jobs[uid] = {"state": "running", "t": time.time()}
        try:
            fn()
        except Exception:
            logging.exception("manus job crashed")
        finally:
            with _jobs_lock:
                _jobs.pop(uid, None)

    _pool.submit(wrapped)
    return ""


def tg_download(file_id: str, max_bytes: int = 8 * 1024 * 1024) -> bytes:
    info = bot.get_file(file_id)
    if info.file_size and info.file_size > max_bytes:
        raise ValueError("حجم فایل بیش از ۸MB است.")
    r = _http.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info.file_path}", timeout=30.0)
    if r.status_code != 200:  # never raise_for_status(): the message would contain the bot token
        raise RuntimeError(f"telegram download HTTP {r.status_code}")
    return r.content


def manus_flow(m) -> None:
    uid, chat = m.from_user.id, m.chat.id
    if not manus_key(uid):
        tg_send(chat, "🔑 کلید Manus ثبت نشده.", reply_to=m.message_id,
                markup=kb([("🔑 ثبت کلید Manus", "ka:manus")], [("🌐 ساخت کلید", MANUS_KEY_URL)]))
        return
    prompt = (m.caption if m.content_type != "text" else m.text) or ""
    prompt = prompt.strip() or PICKED.pop(uid, "")
    attachment = None
    if m.content_type in ("photo", "document"):
        try:
            if m.content_type == "photo":
                fid, name, mime = m.photo[-1].file_id, "telegram_photo.jpg", "image/jpeg"
            else:
                fid, name, mime = m.document.file_id, m.document.file_name or "telegram_document", m.document.mime_type or "application/octet-stream"
            raw = tg_download(fid)
            attachment = {"type": "file", "filename": name, "file_data": f"data:{mime};base64,{base64.b64encode(raw).decode()}"}
        except Exception as e:
            tg_send(chat, f"❌ دریافت فایل ناموفق: {esc(e)}", reply_to=m.message_id)
            return
    elif not prompt:
        return
    refusal = manus_submit(uid, lambda: run_manus(uid, chat, m.message_id, prompt, attachment))
    if refusal:
        tg_send(chat, refusal, reply_to=m.message_id)


# ════════════════════════════════════════════════════════════════════════════
# Screens (inline menus)
# ════════════════════════════════════════════════════════════════════════════

Screen = Tuple[str, Kb]


def screen_home(uid: int) -> Screen:
    s = settings(uid)
    prov, mid = parse_model(s["model"])
    manus = s["mode"] == "manus"
    text = (f"🤖 <b>AIDahl</b> <i>v{VERSION}</i>\n\nحالت: <b>{'🎨 Manus' if manus else '💬 چت'}</b>\n"
            f"مدل: <code>{esc(prov)}</code> / <code>{esc(mid)}</code>\n{quota_line(quota_get(uid))}\n\n"
            "پیام یا عکس بفرستید، یا از منو استفاده کنید.")
    return text, kb(
        [("💬 چت" + (" ✅" if not manus else ""), "mode:chat"), ("🎨 Manus" + (" ✅" if manus else ""), "mode:manus")],
        [("⚙️ تنظیمات", "set"), ("🔑 کلیدها", "keys")],
        [("🆕 گفتگوی جدید", "new"), ("📊 مصرف", "usage")],
        [("📝 پرامپت‌های من", "prm"), (PROMPTOPIA_LABEL, PROMPTOPIA_URL)])


def screen_settings(uid: int) -> Screen:
    s = settings(uid)
    prov, mid = parse_model(s["model"])
    text = (f"⚙️ <b>تنظیمات</b>\n\nمدل: <code>{esc(prov)}</code> / <code>{esc(mid)}</code>\n"
            f"فرمت پاسخ: <b>{STYLES.get(s['style'], s['style'])}</b>\n"
            f"تاریخچه: <b>{s['ctx'] or HISTORY_LIMIT}</b> پیام\nاستریم: <b>{'روشن' if s['stream'] else 'خاموش'}</b>")
    return text, kb(
        [("🧠 مدل", "sm")],
        [("🎨 فرمت", "sf"), ("📜 تاریخچه", "sc")],
        [("⚡ استریم: " + ("خاموش کن" if s["stream"] else "روشن کن"), "st")],
        [("⬅️ منو", "home")])


def screen_keys(uid: int) -> Screen:
    ks = keys_get(uid)
    lines = []
    for p in KEY_PROVIDERS:
        name = "Manus" if p == "manus" else PROVIDERS[p]["name"]
        r = ks.get(p)
        lines.append(f"{'✅' if r else '▫️'} {esc(name)}" + (f" — <code>{esc(mask(r['api_key']))}</code>" if r else ""))
    rows = [[(("Manus" if p == "manus" else PROVIDERS[p]["name"]), f"kp:{p}")] for p in KEY_PROVIDERS]
    rows = [sum(rows[i:i + 2], []) for i in range(0, len(rows), 2)]
    return "🔑 <b>کلیدهای شخصی (BYOK)</b>\n\n" + "\n".join(lines), kb(*rows, [("⬅️ منو", "home")])


def screen_key_provider(uid: int, p: str) -> Screen:
    if p not in KEY_PROVIDERS:
        return screen_keys(uid)
    name = "Manus" if p == "manus" else PROVIDERS[p]["name"]
    has = p in keys_get(uid)
    url = MANUS_KEY_URL if p == "manus" else PROVIDERS[p]["key_url"]
    rows = [[("✏️ ثبت/جایگزینی کلید", f"ka:{p}")]]
    if has:
        rows.append([("🗑 حذف کلید", f"kd:{p}")])
    if url:
        rows.append([("🌐 گرفتن کلید", url)])
    rows.append([("⬅️ کلیدها", "keys")])
    return f"🔑 <b>{esc(name)}</b>\nوضعیت: {'✅ ثبت شده' if has else '❌ ثبت نشده'}", kb(*rows)


def screen_usage(uid: int) -> Screen:
    v = quota_get(uid)
    return (f"📊 <b>مصرف شما</b>\n\nامروز: <code>{v['used']:,}</code> / <code>{v['dl']:,}</code>\n"
            f"کل حساب: <code>{v['lu']:,}</code> / <code>{v['ll']:,}</code> ({v['l_pct']:.1f}%)\n\n"
            "سهمیه روزانه فقط برای کلید مشترک اعمال می‌شود."), kb([("⬅️ منو", "home")])


def screen_prompts(uid: int) -> Screen:
    rows = prompts_list(uid)
    btns = [[(f"▶️ {r['name'][:28]}", f"pp:{r['id']}"), ("🗑", f"pd:{r['id']}")] for r in rows[:12]]
    text = "📝 <b>پرامپت‌های من</b>\n" + ("برای اجرا روی عکس، پرامپت را انتخاب و بعد عکس بفرستید." if rows else "هنوز چیزی ذخیره نکرده‌اید.")
    return text, kb(*btns, [("➕ پرامپت جدید", "ps")], [("⬅️ منو", "home")])


def screen_manus(uid: int) -> Screen:
    sess = manus_session(uid)
    with _jobs_lock:
        job = _jobs.get(uid)
        load = f"{sum(1 for j in _jobs.values() if j['state'] == 'running')}/{MANUS_WORKERS} · صف {sum(1 for j in _jobs.values() if j['state'] == 'queued')}"
    text = (f"🎨 <b>Manus Agent</b>\nکلید: {'✅' if manus_key(uid) else '❌'}\n"
            f"تسک فعال: {'<a href=\"%s\">باز کردن</a>' % esc(sess['url']) if sess else '—'}\n"
            f"وضعیت شما: {job['state'] if job else 'آزاد'} · ظرفیت {load}\n\n"
            "متن یا عکس (+کپشن) بفرستید؛ پیام‌های بعدی ادامه‌ی همان تسک است.")
    return text, kb([("🆕 تسک جدید", "mn"), ("✖️ پایان تسک", "mx")], [("🧹 آزادسازی قفل", "mr"), ("🔑 کلید", "kp:manus")],
                    [("⬅️ منو", "home")])


def screen_admin() -> Screen:
    day_ago = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat().replace("+", "%2B")
    quotas = sb_rows("user_quotas?select=lifetime_used,used_today,reset_at&limit=5000")
    kc = Counter(r["provider"] for r in sb_rows("user_api_keys?select=provider&limit=5000"))
    text = (f"👑 <b>آمار ادمین</b> · v{VERSION}\n\nکاربران: <code>{sb_count('users'):,}</code> · فعال ۲۴ساعت: "
            f"<code>{sb_count('users?last_active_at=gte.' + day_ago):,}</code>\n"
            f"گفتگوها: <code>{sb_count('conversations'):,}</code> · پیام‌ها: <code>{sb_count('messages'):,}</code>\n"
            f"توکن کل: <code>{sum(int(r.get('lifetime_used') or 0) for r in quotas):,}</code> · امروز: "
            f"<code>{sum(int(r.get('used_today') or 0) for r in quotas if str(r.get('reset_at')) >= today()):,}</code>\n"
            f"کلیدها: {', '.join(f'{esc(p)}={n}' for p, n in kc.most_common()) or '—'}\n"
            f"صف Manus: <code>{len(_jobs)}</code>\n<i>(جمع‌ها تا ۵۰۰۰ ردیف اول)</i>")
    return text, kb([("🔄 بروزرسانی", "adm"), ("⬅️ منو", "home")])


# ════════════════════════════════════════════════════════════════════════════
# Callback router
# ════════════════════════════════════════════════════════════════════════════

CB: Dict[str, Callable] = {}


def cb(name: str):
    def deco(fn):
        CB[name] = fn
        return fn
    return deco


@cb("home")
def _(c): return screen_home(c.from_user.id)


@cb("set")
def _(c): return screen_settings(c.from_user.id)


@cb("keys")
def _(c): return screen_keys(c.from_user.id)


@cb("usage")
def _(c): return screen_usage(c.from_user.id)


@cb("prm")
def _(c): return screen_prompts(c.from_user.id)


@cb("kp")
def _(c, p): return screen_key_provider(c.from_user.id, p)


@cb("mode")
def _(c, mode):
    uid = c.from_user.id
    user_set(uid, bot_mode=mode if mode in ("chat", "manus") else "chat")
    return (*(screen_manus(uid) if mode == "manus" else screen_home(uid)), "حالت تغییر کرد")


@cb("new")
def _(c):
    uid = c.from_user.id
    conv_new(uid)
    _warned.discard(uid)
    return (*screen_home(uid), "گفتگوی جدید شروع شد")


@cb("sm")
def _(c):
    uid = c.from_user.id
    ks = keys_get(uid)
    rows = [[(("✅ " if (p in ks or (p == "dahl" and DAHL_API_KEY and ALLOW_SHARED_KEY)) else "") + v["name"], f"sp:{p}")]
            for p, v in PROVIDERS.items()]
    return "🧠 <b>انتخاب سرویس</b>\n✅ = کلید/دسترسی دارید.", kb(*rows, [("⬅️ تنظیمات", "set")])


@cb("sp")
def _(c, p):
    v = PROVIDERS.get(p)
    if not v:
        return screen_settings(c.from_user.id)
    if not v["models"]:
        return (f"{esc(v['name'])}: کلید را از «کلیدها» ثبت کنید؛ بعد از آن مدل انتخاب می‌شود.",
                kb([("🔑 کلیدها", f"kp:{p}")], [("⬅️ سرویس‌ها", "sm")]))
    rows = [[(label, f"mo:{p}:{i}")] for i, (_, label) in enumerate(v["models"])]
    return f"🧠 <b>{esc(v['name'])}</b> — مدل را انتخاب کنید:", kb(*rows, [("⬅️ سرویس‌ها", "sm")])


@cb("mo")
def _(c, p, i):
    uid = c.from_user.id
    models = (PROVIDERS.get(p) or {}).get("models") or []
    if not i.isdigit() or int(i) >= len(models):
        return screen_settings(uid)
    user_set(uid, selected_model=f"{p}|{models[int(i)][0]}")
    note = "" if (p in keys_get(uid) or (p == "dahl" and DAHL_API_KEY and ALLOW_SHARED_KEY)) else " (برای این سرویس کلید ثبت کنید)"
    return (*screen_settings(uid), "مدل تغییر کرد" + note)


@cb("sf")
def _(c):
    cur = settings(c.from_user.id)["style"]
    return "🎨 <b>فرمت پاسخ</b>", kb(*[[(("✅ " if k == cur else "") + v, f"ss:{k}")] for k, v in STYLES.items()], [("⬅️ تنظیمات", "set")])


@cb("ss")
def _(c, style):
    if style in STYLES:
        user_set(c.from_user.id, response_style=style)
    return screen_settings(c.from_user.id)


@cb("sc")
def _(c):
    cur = settings(c.from_user.id)["ctx"] or HISTORY_LIMIT
    return "📜 <b>تعداد پیام‌های تاریخچه</b>", kb([((("✅ " if n == cur else "") + str(n)), f"sn:{n}") for n in CONTEXT_CHOICES], [("⬅️ تنظیمات", "set")])


@cb("sn")
def _(c, n):
    if n.isdigit() and int(n) in CONTEXT_CHOICES:
        user_set(c.from_user.id, context_messages=int(n))
    return screen_settings(c.from_user.id)


@cb("st")
def _(c):
    uid = c.from_user.id
    user_set(uid, stream_enabled=not settings(uid)["stream"])
    return screen_settings(uid)


@cb("ka")
def _(c, p):
    uid = c.from_user.id
    if p not in KEY_PROVIDERS:
        return screen_keys(uid)
    if p == "custom":
        pend_set(uid, "custom_base")
        text = "🔧 مرحله ۱/۳ — <b>Base URL</b> را بفرستید (فقط https)، مثل <code>https://api.example.com/v1</code>"
    else:
        pend_set(uid, "set_key", provider=p)
        text = "🔑 کلید API را بفرستید. پیام شما بعد از ذخیره از چت پاک می‌شود."
    return text + "\n/cancel برای لغو", kb([("⬅️ انصراف", f"kp:{p}")])


@cb("kd")
def _(c, p):
    uid = c.from_user.id
    ok = key_del(uid, p) if p in KEY_PROVIDERS else False
    return (*screen_keys(uid), "حذف شد" if ok else "ناموفق")


@cb("mn")
def _(c):
    uid = c.from_user.id
    SESSIONS.pop(uid, None)
    user_set(uid, bot_mode="manus")
    return (*screen_manus(uid), "تسک جدید: پیام بعدی تسک تازه می‌سازد")


@cb("mx")
def _(c):
    SESSIONS.pop(c.from_user.id, None)
    return (*screen_manus(c.from_user.id), "ادامه‌ی تسک پایان یافت")


@cb("mr")
def _(c):
    with _jobs_lock:
        _jobs.pop(c.from_user.id, None)
    return (*screen_manus(c.from_user.id), "قفل آزاد شد")


@cb("pp")
def _(c, pid):
    uid = c.from_user.id
    row = next((r for r in prompts_list(uid) if str(r["id"]) == pid), None)
    if not row:
        return screen_prompts(uid)
    PICKED[uid] = row["prompt"]
    user_set(uid, bot_mode="manus")
    return (f"✅ پرامپت «{esc(row['name'])}» انتخاب شد.\nحالا عکس را (بدون کپشن) بفرستید.\n\n<i>{esc(row['prompt'][:300])}</i>",
            kb([("⬅️ پرامپت‌ها", "prm")]), "انتخاب شد")


@cb("pd")
def _(c, pid):
    uid = c.from_user.id
    if pid.isdigit():
        sb_ok("DELETE", f"user_prompts?user_id=eq.{int(uid)}&id=eq.{int(pid)}")
    return (*screen_prompts(uid), "حذف شد")


@cb("ps")
def _(c):
    pend_set(c.from_user.id, "prompt_name")
    return "📝 یک <b>نام</b> برای پرامپت بفرستید.\n/cancel برای لغو", kb([("⬅️ انصراف", "prm")])


@cb("adm")
def _(c):
    return screen_admin() if is_admin(c.from_user.id) else ("⛔️ دسترسی ندارید.", kb([("⬅️ منو", "home")]))


@bot.callback_query_handler(func=lambda c: True)
@guard
def on_callback(call) -> None:
    name, *args = (call.data or "").split(":")
    fn = CB.get(name)
    out = fn(call, *args) if fn else None
    toast = out[2] if out and len(out) > 2 else None
    try:
        bot.answer_callback_query(call.id, text=toast)
    except Exception:
        pass
    if out:
        tg_edit(call.message.chat.id, call.message.message_id, out[0], "ui", out[1])


# ════════════════════════════════════════════════════════════════════════════
# Pending (multi-step) input
# ════════════════════════════════════════════════════════════════════════════

SECRET_ACTIONS = ("set_key", "custom_key")


def handle_pending(m, p: dict) -> None:
    uid, chat, raw, act = m.from_user.id, m.chat.id, (m.text or "").strip(), p["action"]
    try:
        if act == "set_key":
            prov = p["provider"]
            if len(raw) < 8:
                tg_send(chat, "کلید نامعتبر است. دوباره بفرستید یا /cancel.")
                return
            if not key_set(uid, prov, raw):
                PENDING.pop(uid, None)
                tg_send(chat, "❌ ذخیره‌ی کلید ناموفق بود (SQL و SUPABASE_KEY را بررسی کنید).")
                return
            PENDING.pop(uid, None)
            if prov != "manus" and parse_model(settings(uid)["model"])[0] != prov and PROVIDERS[prov]["models"]:
                user_set(uid, selected_model=f"{prov}|{PROVIDERS[prov]['models'][0][0]}")
            tg_send(chat, f"✅ کلید ذخیره شد: <code>{esc(mask(raw))}</code>", markup=kb([("🏠 منو", "home")]))
        elif act == "custom_base":
            ok, why = validate_public_https_url(raw, allow_private=CUSTOM_ALLOW_PRIVATE, allowed_hosts=CUSTOM_ALLOWED_HOSTS, use_cache=False)
            if not ok:
                tg_send(chat, f"❌ آدرس قابل قبول نیست: {esc(why)}\nمثال: <code>https://api.example.com/v1</code>")
                return
            pend_set(uid, "custom_key", base=raw.rstrip("/"))
            tg_send(chat, "مرحله ۲/۳ — <b>API Key</b> را بفرستید:")
        elif act == "custom_key":
            if len(raw) < 4:
                tg_send(chat, "کلید نامعتبر است.")
                return
            pend_set(uid, "custom_model", base=p["base"], key=raw)
            tg_send(chat, "مرحله ۳/۳ — <b>Model ID</b> را بفرستید:")
        elif act == "custom_model":
            if not raw:
                return
            PENDING.pop(uid, None)
            if key_set(uid, "custom", p["key"], base_url=p["base"]):
                user_set(uid, selected_model=f"custom|{raw}")
                tg_send(chat, f"✅ سرویس سفارشی ذخیره شد.\n<code>{esc(p['base'])}</code> · <code>{esc(raw)}</code>", markup=kb([("🏠 منو", "home")]))
            else:
                tg_send(chat, "❌ ذخیره ناموفق بود.")
        elif act == "prompt_name":
            pend_set(uid, "prompt_text", name=raw[:64])
            tg_send(chat, f"حالا <b>متن</b> پرامپت «{esc(raw[:64])}» را بفرستید (تا ۸۰۰۰ کاراکتر):")
        elif act == "prompt_text":
            PENDING.pop(uid, None)
            ok = bool(raw) and prompt_save(uid, p["name"], raw)
            tg_send(chat, "✅ ذخیره شد." if ok else "❌ ذخیره ناموفق بود.", markup=kb([("📝 پرامپت‌ها", "prm")]))
    finally:
        if act in SECRET_ACTIONS:  # wipe messages that carry API keys
            try:
                bot.delete_message(chat, m.message_id)
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════════
# Commands & message handlers
# ════════════════════════════════════════════════════════════════════════════

COMMANDS = [("start", "شروع و منوی اصلی"), ("help", "راهنما"), ("settings", "تنظیمات"), ("keys", "کلیدهای API شخصی"),
            ("mode", "تغییر حالت چت/Manus"), ("manus", "تسک جدید Manus"), ("clear", "گفتگوی جدید"),
            ("prompts", "پرامپت‌های ذخیره‌شده"), ("saveprompt", "ذخیره‌ی پرامپت"), ("usage", "مصرف"),
            ("admin", "آمار ادمین"), ("cancel", "لغو عملیات جاری")]


def send_screen(m, screen: Screen) -> None:
    tg_send(m.chat.id, screen[0], markup=screen[1])


@bot.message_handler(commands=["start"])
@guard
def cmd_start(m):
    ensure_user(m.from_user)
    send_screen(m, screen_home(m.from_user.id))


@bot.message_handler(commands=["help"])
@guard
def cmd_help(m):
    ensure_user(m.from_user)
    tg_send(m.chat.id, "📖 <b>راهنما</b>\n\n• پیام بفرستید تا پاسخ بگیرید (حالت چت).\n"
            "• /keys — کلید API شخصی (Groq، OpenRouter، سفارشی، Manus…)\n• /settings — مدل، فرمت، تاریخچه، استریم\n"
            "• /mode یا /manus — حالت Manus برای ساخت/ویرایش تصویر\n• /clear — گفتگوی جدید\n"
            "• /prompts و /saveprompt — پرامپت‌های ذخیره‌شده\n• /usage — مصرف\n• /cancel — لغو",
            markup=kb([("🏠 منو", "home")]))


def _simple(cmd: str, screen_fn: Callable[[int], Screen]) -> None:
    @bot.message_handler(commands=[cmd])
    @guard
    def handler(m):
        ensure_user(m.from_user)
        send_screen(m, screen_fn(m.from_user.id))
    handler.__name__ = f"cmd_{cmd}"


_simple("settings", screen_settings)
_simple("keys", screen_keys)
_simple("mode", screen_home)
_simple("prompts", screen_prompts)
_simple("usage", screen_usage)


@bot.message_handler(commands=["manus"])
@guard
def cmd_manus(m):
    uid = m.from_user.id
    ensure_user(m.from_user)
    SESSIONS.pop(uid, None)
    user_set(uid, bot_mode="manus")
    send_screen(m, screen_manus(uid))


@bot.message_handler(commands=["clear", "newchat"])
@guard
def cmd_clear(m):
    ensure_user(m.from_user)
    conv_new(m.from_user.id)
    _warned.discard(m.from_user.id)
    tg_send(m.chat.id, "🆕 گفتگوی جدید شروع شد. حافظه‌ی قبلی از سرور پاک شد.")


@bot.message_handler(commands=["saveprompt"])
@guard
def cmd_saveprompt(m):
    ensure_user(m.from_user)
    pend_set(m.from_user.id, "prompt_name")
    tg_send(m.chat.id, "📝 یک <b>نام</b> برای پرامپت بفرستید. /cancel برای لغو")


@bot.message_handler(commands=["admin"])
@guard
def cmd_admin(m):
    ensure_user(m.from_user)
    if not is_admin(m.from_user.id):
        tg_send(m.chat.id, f"⛔️ دسترسی ندارید.\nآیدی شما: <code>{m.from_user.id}</code>")
        return
    send_screen(m, screen_admin())


@bot.message_handler(commands=["cancel"])
@guard
def cmd_cancel(m):
    PENDING.pop(m.from_user.id, None)
    PICKED.pop(m.from_user.id, None)
    tg_send(m.chat.id, "✅ لغو شد.")


@bot.message_handler(content_types=["photo", "document"])
@guard
def on_media(m):
    ensure_user(m.from_user)
    if settings(m.from_user.id)["mode"] != "manus":
        tg_send(m.chat.id, "🖼 برای کار با عکس، حالت Manus را فعال کنید.", reply_to=m.message_id,
                markup=kb([("🎨 فعال‌سازی Manus", "mode:manus")]))
        return
    manus_flow(m)


@bot.message_handler(content_types=["text"])
@guard
def on_text(m):
    uid = m.from_user.id
    ensure_user(m.from_user)
    p = pend_get(uid)
    if p:
        handle_pending(m, p)
        return
    if (m.text or "").startswith("/"):
        tg_send(m.chat.id, "دستور ناشناخته. /help")
        return
    (manus_flow if settings(uid)["mode"] == "manus" else chat_flow)(m)


# ════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    except Exception as e:
        logging.warning("set_my_commands failed: %s", e)
    bot.remove_webhook()
    logging.info("AIDahl v%s started", VERSION)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
