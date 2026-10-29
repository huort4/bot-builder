"""
Manus Agent – create tasks, poll, deliver media, multi-turn follow-up.
Designed for low-RAM free tiers with an in-memory concurrency queue.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx

from bot.config import settings
from bot.services.users import list_user_keys
from bot.utils import utcnow_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory concurrency control (single container)
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_ACTIVE = 0
_QUEUE: deque = deque()
_RUNNING_USERS: Set[int] = set()
_QUEUED_USERS: Set[int] = set()
_JOB_META: Dict[int, float] = {}          # user_id → start time
_SESSIONS: Dict[int, dict] = {}           # user_id → session info
_DELIVERED: Dict[int, Set[str]] = {}      # user_id → already sent URLs


def get_manus_key(user_id: int) -> Optional[str]:
    keys = list_user_keys(user_id)
    if "manus" in keys and keys["manus"].get("api_key"):
        return keys["manus"]["api_key"]
    return settings.MANUS_API_KEY or None


def manus_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Session (multi-turn)
# ---------------------------------------------------------------------------

def save_session(user_id: int, task_id: str, task_url: str = "", title: str = "") -> None:
    _SESSIONS[user_id] = {
        "task_id": task_id,
        "task_url": task_url,
        "title": title,
        "updated_at": time.time(),
    }


def get_session(user_id: int) -> Optional[dict]:
    s = _SESSIONS.get(user_id)
    if not s:
        return None
    if time.time() - s["updated_at"] > settings.MANUS_FOLLOWUP_TTL_SEC:
        _SESSIONS.pop(user_id, None)
        return None
    return s


def clear_session(user_id: int) -> None:
    _SESSIONS.pop(user_id, None)


def session_label(user_id: int) -> str:
    s = get_session(user_id)
    if not s:
        return "بدون جلسه فعال"
    title = s.get("title") or s.get("task_id", "")[:12]
    return f"🔗 {title}"


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def queue_stats() -> dict:
    with _LOCK:
        return {
            "active": _ACTIVE,
            "queued": len(_QUEUE),
            "running_users": len(_RUNNING_USERS),
            "max_concurrent": settings.MANUS_MAX_CONCURRENT,
            "max_queue": settings.MANUS_QUEUE_MAX,
        }


def queue_status_text() -> str:
    st = queue_stats()
    return (
        f"🎨 صف Manus\n"
        f"• فعال: {st['active']} / {st['max_concurrent']}\n"
        f"• در انتظار: {st['queued']} / {st['max_queue']}"
    )


def force_unlock(user_id: Optional[int] = None) -> int:
    """Admin / recovery helper."""
    with _LOCK:
        count = 0
        if user_id is not None:
            if user_id in _RUNNING_USERS:
                _RUNNING_USERS.discard(user_id)
                _JOB_META.pop(user_id, None)
                global _ACTIVE
                _ACTIVE = max(0, _ACTIVE - 1)
                count += 1
            _QUEUED_USERS.discard(user_id)
            # remove from queue
            new_q = deque(j for j in _QUEUE if j.get("user_id") != user_id)
            removed = len(_QUEUE) - len(new_q)
            _QUEUE.clear()
            _QUEUE.extend(new_q)
            count += removed
        else:
            count = len(_RUNNING_USERS) + len(_QUEUE)
            _RUNNING_USERS.clear()
            _QUEUED_USERS.clear()
            _JOB_META.clear()
            _QUEUE.clear()
            _ACTIVE = 0
        return count


def reap_stale_locks() -> int:
    now = time.time()
    stale = []
    with _LOCK:
        for uid, start in list(_JOB_META.items()):
            if now - start > settings.MANUS_STALE_LOCK_SEC:
                stale.append(uid)
        for uid in stale:
            _RUNNING_USERS.discard(uid)
            _JOB_META.pop(uid, None)
            global _ACTIVE
            _ACTIVE = max(0, _ACTIVE - 1)
    return len(stale)


# ---------------------------------------------------------------------------
# Low-level API
# ---------------------------------------------------------------------------

def create_task(
    api_key: str,
    prompt: str,
    *,
    profile: str = None,
    attachments: Optional[List[dict]] = None,
    locale: str = None,
) -> dict:
    """
    Create a new Manus task.
    attachments: list of {"url": ..., "name": ...} or raw bytes handled upstream.
    """
    profile = profile or settings.MANUS_DEFAULT_PROFILE
    payload: Dict[str, Any] = {
        "prompt": prompt,
        "profile": profile,
    }
    loc = locale or settings.MANUS_LOCALE
    if loc:
        payload["locale"] = loc
    if attachments:
        payload["attachments"] = attachments

    url = f"{settings.MANUS_BASE_URL}/v2/tasks"
    with httpx.Client(timeout=30.0) as client:
        r = client.post(url, headers=manus_headers(api_key), json=payload)
        r.raise_for_status()
        return r.json()


def send_task_message(api_key: str, task_id: str, message: str) -> dict:
    url = f"{settings.MANUS_BASE_URL}/v2/tasks/{task_id}/messages"
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            url,
            headers=manus_headers(api_key),
            json={"message": message},
        )
        r.raise_for_status()
        return r.json()


def task_detail(api_key: str, task_id: str) -> dict:
    url = f"{settings.MANUS_BASE_URL}/v2/tasks/{task_id}"
    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, headers=manus_headers(api_key))
        r.raise_for_status()
        return r.json()


def list_messages(api_key: str, task_id: str, limit: int = 50) -> list:
    url = f"{settings.MANUS_BASE_URL}/v2/tasks/{task_id}/messages?limit={limit}"
    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, headers=manus_headers(api_key))
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("messages") or data.get("data") or []


# ---------------------------------------------------------------------------
# Result extraction (simplified & robust)
# ---------------------------------------------------------------------------

def _walk(obj, files: List[dict], texts: List[str], depth: int = 0) -> None:
    if depth > 8 or obj is None:
        return
    if isinstance(obj, str):
        if obj.startswith("http") and any(
            ext in obj.lower() for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm")
        ):
            files.append({"url": obj, "name": obj.split("/")[-1].split("?")[0]})
        return
    if isinstance(obj, dict):
        if "url" in obj and isinstance(obj["url"], str):
            files.append(obj)
        if "content" in obj and isinstance(obj["content"], str):
            texts.append(obj["content"])
        if "text" in obj and isinstance(obj["text"], str):
            texts.append(obj["text"])
        for v in obj.values():
            _walk(v, files, texts, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk(item, files, texts, depth + 1)


def extract_from_messages(messages: List[dict]) -> dict:
    files: List[dict] = []
    texts: List[str] = []
    for m in messages:
        role = (m.get("role") or "").lower()
        if role in ("assistant", "agent", "tool"):
            _walk(m, files, texts)
    # dedupe by url
    seen = set()
    unique = []
    for f in files:
        u = f.get("url")
        if u and u not in seen:
            seen.add(u)
            unique.append(f)
    return {"files": unique, "texts": texts}


def wait_for_task(
    api_key: str,
    task_id: str,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
    timeout: int = None,
) -> dict:
    timeout = timeout or settings.MANUS_TIMEOUT_SEC
    deadline = time.time() + timeout
    last_state = ""

    while time.time() < deadline:
        try:
            detail = task_detail(api_key, task_id)
            state = (detail.get("status") or detail.get("state") or "").lower()
            if state != last_state and on_progress:
                on_progress(state)
                last_state = state
            if state in ("completed", "success", "done", "finished"):
                msgs = list_messages(api_key, task_id)
                extracted = extract_from_messages(msgs)
                return {
                    "ok": True,
                    "state": state,
                    "detail": detail,
                    "files": extracted["files"],
                    "texts": extracted["texts"],
                }
            if state in ("failed", "error", "cancelled"):
                return {"ok": False, "state": state, "detail": detail, "files": [], "texts": []}
        except Exception as e:
            logger.warning("poll error: %s", e)
        time.sleep(settings.MANUS_POLL_INTERVAL)

    return {"ok": False, "state": "timeout", "detail": {}, "files": [], "texts": []}


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def _try_start(job: dict) -> bool:
    global _ACTIVE
    uid = job["user_id"]
    with _LOCK:
        if _ACTIVE >= settings.MANUS_MAX_CONCURRENT:
            return False
        if uid in _RUNNING_USERS:
            return False
        _ACTIVE += 1
        _RUNNING_USERS.add(uid)
        _QUEUED_USERS.discard(uid)
        _JOB_META[uid] = time.time()
        return True


def _finish(uid: int) -> None:
    global _ACTIVE
    with _LOCK:
        _RUNNING_USERS.discard(uid)
        _JOB_META.pop(uid, None)
        _ACTIVE = max(0, _ACTIVE - 1)
        # start next
        while _QUEUE:
            next_job = _QUEUE.popleft()
            _QUEUED_USERS.discard(next_job["user_id"])
            if _try_start(next_job):
                t = threading.Thread(target=_execute, args=(next_job,), daemon=True)
                t.start()
                break


def _execute(job: dict) -> None:
    uid = job["user_id"]
    chat_id = job["chat_id"]
    bot = job["bot"]
    api_key = job["api_key"]
    prompt = job["prompt"]
    is_followup = job.get("is_followup", False)
    task_id = job.get("task_id")
    status_msg_id = job.get("status_msg_id")

    def edit_status(text: str):
        if status_msg_id:
            try:
                bot.edit_message_text(text, chat_id, status_msg_id)
            except Exception:
                pass

    try:
        edit_status("🎨 در حال ارسال به Manus…")

        if is_followup and task_id:
            send_task_message(api_key, task_id, prompt)
            result = wait_for_task(
                api_key,
                task_id,
                on_progress=lambda s: edit_status(f"🎨 وضعیت: {s}"),
            )
        else:
            created = create_task(api_key, prompt, attachments=job.get("attachments"))
            task_id = created.get("id") or created.get("task_id")
            task_url = created.get("url") or ""
            if not task_id:
                edit_status("❌ ایجاد تسک ناموفق بود.")
                return
            save_session(uid, task_id, task_url, title=prompt[:40])
            result = wait_for_task(
                api_key,
                task_id,
                on_progress=lambda s: edit_status(f"🎨 وضعیت: {s}"),
            )

        if not result["ok"]:
            edit_status(f"❌ تسک به پایان نرسید ({result['state']})")
            return

        # Deliver text
        for t in result.get("texts") or []:
            if t.strip():
                try:
                    bot.send_message(chat_id, t[:4000])
                except Exception:
                    pass

        # Deliver files (simple – download & send)
        delivered = _DELIVERED.setdefault(uid, set())
        for f in result.get("files") or []:
            url = f.get("url")
            if not url or url in delivered:
                continue
            try:
                with httpx.Client(timeout=60.0) as client:
                    r = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
                    r.raise_for_status()
                    content = r.content
                name = f.get("name") or "manus-output.png"
                if any(x in name.lower() for x in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    bot.send_photo(chat_id, content, caption=name[:200])
                else:
                    bot.send_document(chat_id, content, visible_file_name=name[:100])
                delivered.add(url)
            except Exception as e:
                logger.warning("deliver file failed: %s", e)

        edit_status("✅ انجام شد")
        # keep session for follow-up
        if task_id:
            save_session(uid, task_id, title=prompt[:40])

    except Exception as e:
        logger.exception("Manus job failed")
        edit_status(f"❌ خطا: {e}")
    finally:
        _finish(uid)


def submit_job(
    bot,
    user_id: int,
    chat_id: int,
    prompt: str,
    *,
    api_key: str,
    status_msg_id: Optional[int] = None,
    attachments: Optional[List[dict]] = None,
    is_followup: bool = False,
    task_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Enqueue or start a Manus job.
    Returns (accepted, message)
    """
    reap_stale_locks()

    job = {
        "bot": bot,
        "user_id": user_id,
        "chat_id": chat_id,
        "prompt": prompt,
        "api_key": api_key,
        "status_msg_id": status_msg_id,
        "attachments": attachments,
        "is_followup": is_followup,
        "task_id": task_id,
    }

    with _LOCK:
        if user_id in _RUNNING_USERS or user_id in _QUEUED_USERS:
            return False, "شما الان یک کار Manus در حال اجرا یا صف دارید."
        if len(_QUEUE) >= settings.MANUS_QUEUE_MAX and _ACTIVE >= settings.MANUS_MAX_CONCURRENT:
            return False, "صف Manus پر است. لطفاً کمی بعد تلاش کنید."

        if _try_start(job):
            t = threading.Thread(target=_execute, args=(job,), daemon=True)
            t.start()
            return True, "شروع شد"
        # queue it
        _QUEUE.append(job)
        _QUEUED_USERS.add(user_id)
        pos = len(_QUEUE)
        return True, f"در صف قرار گرفت (موقعیت {pos})"