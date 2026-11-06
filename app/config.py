"""Settings loaded from .env. Never hardcode keys anywhere else."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # "offline" uses cached responses + deterministic fallbacks (demo works with no API calls).
    llm_provider: Literal["offline", "gemini", "openai"] = "offline"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0

    whisper_model: str = "base"
    tts_enabled: bool = True
    tts_lang: str = "en"

    database_url: str = f"sqlite:///{(ROOT / 'routeexception.db').as_posix()}"
    storage_dir: Path = ROOT / "storage"
    cases_dir: Path = ROOT / "data" / "cases"
    llm_cache_dir: Path = ROOT / "data" / "llm_cache"
    rules_path: Path = ROOT / "config" / "rules.yaml"

    api_base_url: str = "http://localhost:8000"
    public_base_url: str = "http://localhost:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_rules() -> dict[str, Any]:
    """Business rules live in config/rules.yaml, not in code."""
    with open(get_settings().rules_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)
