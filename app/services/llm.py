"""The one helper all LLM calls go through.

- Requests JSON output from the provider.
- Validates it with a Pydantic model; retries once on invalid JSON / schema errors.
- Caches responses on disk so demo cases run with no API calls.
- In "offline" mode (or on any failure) returns None and the caller uses its deterministic fallback.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings

log = logging.getLogger("routeexception.llm")
T = TypeVar("T", bound=BaseModel)


class LLMResult(BaseModel):
    data: dict
    engine: str  # "llm" | "cache"


def _cache_path(task: str, key_material: str) -> Path:
    digest = hashlib.sha256(f"{task}\n{key_material}".encode()).hexdigest()[:24]
    d = get_settings().llm_cache_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task}_{digest}.json"


def _call_gemini(prompt: str, image: bytes | None, mime: str) -> str:
    s = get_settings()
    parts: list[dict] = [{"text": prompt}]
    if image:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}:generateContent"
    r = httpx.post(
        url,
        params={"key": s.gemini_api_key},
        json={
            "contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
        },
        timeout=s.llm_timeout_seconds,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai(prompt: str, image: bytes | None, mime: str) -> str:
    s = get_settings()
    content: list[dict] = [{"type": "text", "text": prompt}]
    if image:
        b64 = base64.b64encode(image).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {s.openai_api_key}"},
        json={
            "model": s.openai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Reply with a single JSON object only."},
                {"role": "user", "content": content},
            ],
        },
        timeout=s.llm_timeout_seconds,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def provider_available() -> bool:
    s = get_settings()
    return (s.llm_provider == "gemini" and bool(s.gemini_api_key)) or (
        s.llm_provider == "openai" and bool(s.openai_api_key)
    )


def call_json(
    task: str,
    prompt: str,
    schema: type[T],
    *,
    image: bytes | None = None,
    image_mime: str = "image/jpeg",
    cache_key: str | None = None,
) -> tuple[T, str] | None:
    """Return (validated model, engine) or None if no LLM result is available."""
    key_material = cache_key if cache_key is not None else prompt + (
        hashlib.sha256(image).hexdigest() if image else ""
    )
    cpath = _cache_path(task, key_material)
    if cpath.exists():
        try:
            return schema.model_validate_json(cpath.read_text(encoding="utf-8")), "cache"
        except ValidationError:
            log.warning("stale cache entry %s ignored", cpath.name)

    if not provider_available():
        return None

    s = get_settings()
    call = _call_gemini if s.llm_provider == "gemini" else _call_openai
    schema_hint = json.dumps(schema.model_json_schema())
    full_prompt = f"{prompt}\n\nReturn JSON matching this JSON Schema exactly:\n{schema_hint}"

    last_err: Exception | None = None
    for attempt in range(2):  # first try + one retry on invalid JSON
        try:
            raw = call(full_prompt if attempt == 0 else full_prompt + "\n\nYour previous reply was "
                       f"invalid ({last_err}). Return only valid JSON.", image, image_mime)
            obj = schema.model_validate_json(raw)
            cpath.write_text(obj.model_dump_json(indent=2), encoding="utf-8")
            return obj, "llm"
        except (ValidationError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("LLM %s invalid JSON (attempt %d): %s", task, attempt + 1, e)
        except (httpx.HTTPError, KeyError, IndexError) as e:
            log.error("LLM %s call failed: %s", task, e)
            return None
    return None


def seed_cache(task: str, cache_key: str, obj: BaseModel) -> None:
    """Pre-populate the cache (used by the synthetic data generator for demo cases)."""
    _cache_path(task, cache_key).write_text(obj.model_dump_json(indent=2), encoding="utf-8")
