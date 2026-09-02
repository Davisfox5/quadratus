"""Configuration for the multi-LLM workflow.

All settings are read from environment variables (optionally loaded from a
``.env`` file) and can be overridden programmatically. Sensible, top-tier
defaults are chosen for each provider; override the ``*_MODEL`` variables to
match the exact model IDs your accounts have access to.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

try:  # Loading .env is best-effort; missing python-dotenv must not crash.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - optional dependency
    pass


_WARNED_LEGACY_ENV: Set[str] = set()


def env_with_legacy(name: str, legacy: str, default: str = "") -> str:
    """Read ``name``, falling back to its pre-rename ``legacy`` spelling.

    The rename from multi-llm-workflow to Quadratus changed the prefix on every
    variable this project reads. An unmigrated ``.env`` would otherwise fail
    silently -- sharing quietly disabled, a pinned Chromium quietly ignored --
    so the old names keep working and say so once per process.
    """
    value = os.getenv(name)
    if value is not None:
        return value
    value = os.getenv(legacy)
    if value is None:
        return default
    if legacy not in _WARNED_LEGACY_ENV:
        _WARNED_LEGACY_ENV.add(legacy)
        print(f"{legacy} is deprecated; rename it to {name}.", file=sys.stderr)
    return value


# Top-tier defaults. These are intentionally the strongest coding models from
# each provider as of this writing; override via the environment as new models
# ship or to match your account's access.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_OPENAI_MODEL = "gpt-5.5-pro"
DEFAULT_GEMINI_MODEL = "gemini-3.1-pro"

DEFAULT_PROVIDER_ORDER = "claude,openai,gemini"

# CLI backends address models by the vendor CLI's own naming, which is usually
# a short alias rather than a dated API model ID. Two tiers are configured per
# provider: "high" for planning, generation, review and synthesis, "low" for
# the control plane (convergence verdicts, routing, refusal classification,
# summarisation). Running the control plane on the low tier is the single
# largest saving available when every call draws on a subscription window.
# Seed values only -- confirm against `quadratus probe` on your own machine.
# Notes behind these choices:
#   * There is no gpt-5.5-codex model; OpenAI stopped minting Codex-specific
#     variants after 5.3 and routed Codex onto the general GPT-5.x tiers.
#   * GPT-5.6's tiers barely separate on coding (Sol->Luna is ~1.9pts on
#     SWE-bench Pro for a 5x price difference), so Luna is a defensible low
#     tier and arguably a defensible high tier for routine work.
#   * Gemini's current Pro is still 3.1; no 3.5 Pro shipped. Current Flash is
#     3.6 as of ~2026-07-21.
DEFAULT_CLI_MODELS = {
    "claude": {"high": "opus", "low": "haiku"},
    "openai": {"high": "gpt-5.6-sol", "low": "gpt-5.6-luna"},
    "gemini": {"high": "gemini-3.1-pro", "low": "gemini-3.6-flash"},
    "grok": {"high": "grok-4.6", "low": "grok-4-1-fast"},
}

#: Transport for every provider unless overridden per provider.
DEFAULT_BACKEND = "cli"

_VALID_BACKENDS = ("cli", "api")


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
    #: Where a request goes when Claude's safety classifiers decline it
    #: (``stop_reason: "refusal"``, an HTTP 200 with empty content on the
    #: Claude API since Opus 4.7 and on every Fable / Mythos model). Empty
    #: means the refusal surfaces as a ``ProviderRefusal`` error instead of
    #: being re-sent. One value per transport, because the API backend takes a
    #: full model ID while the CLI backend takes the CLI's own alias.
    claude_refusal_fallback_model: str = field(
        default_factory=lambda: os.getenv("CLAUDE_REFUSAL_FALLBACK_MODEL", "").strip()
    )
    claude_cli_refusal_fallback_model: str = field(
        default_factory=lambda: os.getenv("CLAUDE_CLI_REFUSAL_FALLBACK_MODEL", "").strip()
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

    # Transport selection
    #: Default transport for all providers: "cli" (subscription) or "api".
    backend: str = field(
        default_factory=lambda: os.getenv("LLM_BACKEND", DEFAULT_BACKEND).strip().lower()
    )
    #: Per-provider overrides, e.g. ``{"gemini": "api"}`` from GEMINI_BACKEND.
    backend_overrides: Dict[str, str] = field(
        default_factory=lambda: {
            name: value.strip().lower()
            for name in ("claude", "openai", "gemini", "grok")
            for value in (os.getenv(f"{name.upper()}_BACKEND", ""),)
            if value.strip()
        }
    )
    #: Model IDs used by CLI backends, keyed by provider then tier.
    cli_models: Dict[str, Dict[str, str]] = field(
        default_factory=lambda: {
            name: {
                tier: os.getenv(f"{name.upper()}_CLI_MODEL_{tier.upper()}", default)
                for tier, default in tiers.items()
            }
            for name, tiers in DEFAULT_CLI_MODELS.items()
        }
    )

    # Request tuning
    max_tokens: int = field(default_factory=lambda: _env_int("MAX_TOKENS", 8000))
    timeout: float = field(default_factory=lambda: _env_float("REQUEST_TIMEOUT", 120.0))
    #: CLI calls run a full agent loop, not a single completion, so they need a
    #: far more generous ceiling than an HTTP request.
    cli_timeout: float = field(default_factory=lambda: _env_float("CLI_TIMEOUT", 900.0))
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

    # -- transport & routing helpers -----------------------------------------
    def backend_for(self, provider: str) -> str:
        """Return the transport ("cli" or "api") for one provider."""
        choice = self.backend_overrides.get(provider, self.backend)
        if choice not in _VALID_BACKENDS:
            return DEFAULT_BACKEND
        return choice

    def model_for(self, provider: str, tier: str = "high") -> str:
        """Return the model ID for a provider at a given tier.

        API backends keep using their configured dated model IDs; only CLI
        backends consult the tier table, since the CLIs use their own aliases.
        """
        if self.backend_for(provider) == "cli":
            tiers = self.cli_models.get(provider, {})
            return tiers.get(tier) or tiers.get("high") or ""
        return {
            "claude": self.claude_model,
            "openai": self.openai_model,
            "gemini": self.gemini_model,
        }.get(provider, "")

    def refusal_fallback_for(self, provider: str) -> str:
        """Fallback model for a classifier refusal, or "" for none.

        Only Claude carries a request-declining classifier that reports
        itself as a stop reason, so only Claude has a setting. The value is
        picked to match the transport's naming: an API model ID on ``api``,
        a CLI alias on ``cli``.
        """
        if provider != "claude":
            return ""
        if self.backend_for(provider) == "cli":
            return self.claude_cli_refusal_fallback_model
        return self.claude_refusal_fallback_model

    def uses_cli(self) -> bool:
        """True if any configured provider runs on subscription transport.

        Public Gradio sharing must stay off in that case: routing anyone else's
        prompts through your subscription credential violates the consumer
        terms of all three major vendors.
        """
        return any(
            self.backend_for(name) == "cli"
            for name in (*self.provider_order, *self.backend_overrides)
        )

    @classmethod
    def from_env(cls) -> "Settings":
        """Construct settings from the current environment."""
        return cls()
