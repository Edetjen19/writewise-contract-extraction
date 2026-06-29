"""Central configuration. Loads .env and exposes settings with clear errors.

The LLM provider is OpenAI: the assessment supplies an OpenAI key restricted to
gpt-4o-mini / gpt-4.1-mini / gpt-5-mini. Each entry point requires only what it
needs (applying the schema needs the DB but not the key, and vice versa).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load the .env that sits next to this file, so the CLIs work from any directory.
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH if _ENV_PATH.exists() else None)

# Allowed by the assessment key: gpt-4o-mini, gpt-4.1-mini, gpt-5-mini.
DEFAULT_MODEL = "gpt-4.1-mini"


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_model: str
    database_url: str


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(
            f"Missing required environment variable {name!r}.\n"
            f"Copy .env.example to .env and fill it in."
        )
    return val


def load_settings(*, require_api_key: bool = True, require_db: bool = True) -> Settings:
    return Settings(
        openai_api_key=_require("OPENAI_API_KEY") if require_api_key
        else os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        database_url=_require("DATABASE_URL") if require_db
        else os.environ.get("DATABASE_URL", ""),
    )
