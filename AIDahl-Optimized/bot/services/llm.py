"""
OpenAI-compatible chat completions (streaming + non-streaming).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

import httpx

from bot.config import settings
from bot.services.users import list_user_keys, get_user_model
from bot.utils.helpers import parse_model_ref

logger = logging.getLogger(__name__)


def resolve_inference_credentials(user_id: int) -> Optional[dict]:
    """
    Returns {api_key, base_url, provider, model_id, source}
    source = 'own' | 'shared'
    """
    model_ref = get_user_model(user_id)
    provider, model_id = parse_model_ref(model_ref)
    keys = list_user_keys(user_id)

    # Prefer personal key for that provider
    if provider in keys and keys[provider].get("api_key"):
        k = keys[provider]
        return {
            "api_key": k["api_key"],
            "base_url": (k.get("base_url") or _default_base(provider)).rstrip("/"),
            "provider": provider,
            "model_id": model_id,
            "source": "own",
        }

    # Fallback to shared Dahl key
    if settings.ALLOW_SHARED_KEY and settings.DAHL_API_KEY:
        return {
            "api_key": settings.DAHL_API_KEY,
            "base_url": settings.DAHL_BASE_URL,
            "provider": "dahl",
            "model_id": model_id if provider == "dahl" else model_id,
            "source": "shared",
        }

    return None


def _default_base(provider: str) -> str:
    defaults = {
        "dahl": "https://inference.dahl.global/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "cerebras": "https://api.cerebras.ai/v1",
        "siliconflow": "https://api.siliconflow.cn/v1",
        "mistral": "https://api.mistral.ai/v1",
        "github": "https://models.inference.ai.azure.com",
    }
    return defaults.get(provider, settings.DAHL_BASE_URL)


def call_llm(
    messages: List[Dict[str, str]],
    credentials: dict,
    *,
    stream: bool = False,
    on_partial: Optional[Callable[[str], None]] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> Dict[str, Any]:
    """
    Call chat completions.
    Returns {"content": str, "tokens": int, "error": optional str}
    """
    url = f"{credentials['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {credentials['api_key']}",
        "Content-Type": "application/json",
    }
    # Some providers need extra headers
    if credentials["provider"] == "openrouter":
        headers["HTTP-Referer"] = "https://t.me"
        headers["X-Title"] = "AIDahl"

    body = {
        "model": credentials["model_id"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }

    try:
        if stream and on_partial:
            return _stream_call(url, headers, body, on_partial)
        return _sync_call(url, headers, body)
    except Exception as e:
        logger.exception("LLM call failed")
        return {"content": "", "tokens": 0, "error": str(e)}


def _sync_call(url: str, headers: dict, body: dict) -> dict:
    with httpx.Client(timeout=90.0) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        return {"content": content, "tokens": tokens, "error": None}


def _stream_call(
    url: str,
    headers: dict,
    body: dict,
    on_partial: Callable[[str], None],
) -> dict:
    full = ""
    tokens = 0
    with httpx.Client(timeout=120.0) as client:
        with client.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            full += piece
                            on_partial(full)
                        usage = chunk.get("usage")
                        if usage:
                            tokens = int(usage.get("total_tokens") or tokens)
                    except json.JSONDecodeError:
                        continue
    return {"content": full, "tokens": tokens, "error": None}