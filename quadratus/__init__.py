"""Quadratus -- frontier models from rival vendors on one coding task.

Claude, ChatGPT and Grok work a task together instead of one model working
alone. Each provider runs on either backend: the consumer subscription you
already pay for, driven through that vendor's coding-agent CLI so a run draws
on rate-limit windows rather than per-token billing (the default), or a billed
API key.

The shipping entry point is the four-phase collaboration pipeline: every
available model plans independently, a coordinator merges the plans into one
with explicit per-model roles, a lead drafts while the others adversarially
review and refine over one or more rounds, and a synthesizer merges every
contribution into a single answer.

An orchestrated session engine -- difficulty-ladder routing, cross-vendor
review, disposable worker models, an append-only ledger and deterministic
gates -- lives alongside the pipeline in this package and runs from the
command line as ``quadratus --session "<goal>"``, driven by
:class:`quadratus.runtime.Fleet` over the subscription CLIs. A bare
``quadratus "<goal>"`` still runs the pipeline.
"""

from .config import Settings
from .orchestrator import (
    CollaborationResult,
    Orchestrator,
    StageResult,
    build_providers,
)
from .providers import LLMProvider, ProviderError, Turn
from .runtime import Fleet

__all__ = [
    "Settings",
    "Fleet",
    "Orchestrator",
    "CollaborationResult",
    "StageResult",
    "build_providers",
    "LLMProvider",
    "ProviderError",
    "Turn",
]

__version__ = "1.0.0"
