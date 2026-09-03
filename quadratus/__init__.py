"""Quadratus -- four frontier models collaborating on one coding task.

Claude, ChatGPT, Gemini and Grok work a task together instead of one model
working alone. Each provider runs on either backend: a billed API key, or the
consumer subscription you already pay for, driven through that vendor's
coding-agent CLI so a run draws on rate-limit windows rather than per-token
billing.

The shipping entry point is the four-phase collaboration pipeline: every
available model plans independently, a coordinator merges the plans into one
with explicit per-model roles, a lead drafts while the others adversarially
review and refine over one or more rounds, and a synthesizer merges every
contribution into a single answer.

An orchestrated session engine -- difficulty-ladder routing, cross-vendor
review, disposable worker models, an append-only ledger and deterministic
gates -- lives alongside the pipeline in this package and is fully tested, but
is not yet wired to the CLI entry points.
"""

from .config import Settings
from .orchestrator import (
    CollaborationResult,
    Orchestrator,
    StageResult,
    build_providers,
)
from .providers import LLMProvider, ProviderError, Turn

__all__ = [
    "Settings",
    "Orchestrator",
    "CollaborationResult",
    "StageResult",
    "build_providers",
    "LLMProvider",
    "ProviderError",
    "Turn",
]

__version__ = "1.0.0"
