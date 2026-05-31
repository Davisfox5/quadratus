"""Configuration for the multi-LLM workflow.

All settings are read from environment variables (optionally loaded from a
``.env`` file) and can be overridden programmatically. Sensible, top-tier
defaults are chosen for each provider; override the ``*_MODEL`` variables to
match the exact model IDs your accounts have access to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:  # Loading .env is best-effort; missing python-dotenv must not crash.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - optional dependency
    pass


# Top-tier defaults. These are intentionally the strongest coding models from
# each provider as of this writing; override via the environment as new models
# ship or to match your account's access.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-5.5-pro"
DEFAULT_GEMINI_MODEL = "gemini-3.1-pro"

DEFAULT_PROVIDER_ORDER = "claude,openai,gemini"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """Runtime configuration for the workflow."""

    # API keys
    openai_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY")
    )
    anthropic_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY")
    )
    google_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    )

    # Model IDs
    claude_model: str = field(
        default_factory=lambda: os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
    )
    openai_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    )
    gemini_model: str = field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    )

    # Collaboration behaviour
    #: Order in which providers take roles (lead first, then reviewers).
    provider_order: List[str] = field(
        default_factory=lambda: [
            p.strip().lower()
            for p in os.getenv("PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER).split(",")
            if p.strip()
        ]
    )
    #: Number of review/refinement rounds each reviewer performs.
    rounds: int = field(default_factory=lambda: max(1, _env_int("ROUNDS", 1)))
    #: Provider name to use for the final synthesis ("lead" = use the lead).
    synthesizer: str = field(
        default_factory=lambda: os.getenv("SYNTHESIZER", "lead").strip().lower()
    )

    # Request tuning
    max_tokens: int = field(default_factory=lambda: _env_int("MAX_TOKENS", 8000))
    timeout: float = field(default_factory=lambda: _env_float("REQUEST_TIMEOUT", 120.0))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 4))
    retry_base_delay: float = field(
        default_factory=lambda: _env_float("RETRY_BASE_DELAY", 2.0)
    )

    # GUI / memory / file handling
    max_memory_turns: int = field(
        default_factory=lambda: _env_int("MAX_MEMORY_ENTRIES", 10)
    )
    max_file_bytes: int = field(
        default_factory=lambda: _env_int("MAX_FILE_BYTES", 1024 * 1024)
    )
    max_file_chars: int = field(
        default_factory=lambda: _env_int("MAX_FILE_CHARS", 20000)
    )

    @classmethod
    def from_env(cls) -> "Settings":
        """Construct settings from the current environment."""
        return cls()
