"""Quadratus -- collaborative multi-model coding workflow.

Orchestrates several frontier models (Claude, ChatGPT, Gemini) so they
collaborate on a single coding task: a lead model drafts a solution, the
remaining models review and refine it over one or more rounds, and a
synthesizer merges everything into one definitive answer.
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
