"""Unified provider abstraction for Claude, ChatGPT, and Gemini.

Each provider exposes the same ``generate()`` interface, hides SDK-specific
details, and shares retry/backoff handling. Providers degrade gracefully: if
an SDK is not installed or an API key is missing, the provider reports itself
as unavailable instead of crashing the whole workflow.
"""

from __future__ import annotations

import copy
import logging
import random
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A provider call failed permanently (after retries or non-retryable)."""


@dataclass
class Turn:
    """A single prior message handed to a provider as conversation context."""

    role: str  # "user" or "assistant"
    content: str


class LLMProvider:
    """Base class implementing shared retry/backoff and availability logic.

    Subclasses implement :meth:`_build_client`, :meth:`_call`, and
    :meth:`_retryable`.
    """

    #: short machine name, e.g. "claude"
    name = "provider"
    #: human label, e.g. "Claude"
    label = "Provider"

    def __init__(
        self,
        model: str,
        api_key: Optional[str],
        *,
        max_tokens: int = 8000,
        timeout: float = 120.0,
        max_retries: int = 4,
        retry_base_delay: float = 2.0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.retry_base_delay = retry_base_delay
        self._client = None
        self._init_error: Optional[str] = None
        if api_key:
            try:
                self._client = self._build_client()
            except Exception as exc:  # ImportError or bad config
                self._init_error = str(exc)
                log.warning("Could not initialise %s: %s", self.label, exc)
        else:
            self._init_error = "no API key configured"

    # -- subclass hooks ------------------------------------------------------
    def _build_client(self):
        raise NotImplementedError

    def _call(self, prompt: str, system: str, history: Sequence[Turn]) -> str:
        raise NotImplementedError

    def _retryable(self, exc: Exception) -> bool:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _exc_tuple(module, names: Sequence[str]) -> tuple:
        """Collect exception classes that exist on ``module`` into a tuple."""
        found = []
        for n in names:
            cls = getattr(module, n, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                found.append(cls)
        return tuple(found)

    # -- public API ----------------------------------------------------------
    def available(self) -> bool:
        return self._client is not None

    def for_model(self, model: Optional[str]) -> "LLMProvider":
        """Return a view of this provider bound to a different model.

        Model routing sends cheap phases (convergence judging, classification,
        summarisation) to a smaller model on the same subscription. Copying the
        provider keeps the already-built client and avoids re-resolving a CLI
        binary or re-constructing an SDK client on every routed call.
        """
        if not model or model == self.model:
            return self
        clone = copy.copy(self)
        clone.model = model
        return clone

    @property
    def status(self) -> str:
        if self.available():
            return f"{self.label} ({self.model})"
        return f"{self.label} unavailable ({self._init_error})"

    def generate(
        self,
        prompt: str,
        *,
        system: str = "You are a helpful assistant.",
        history: Optional[Sequence[Turn]] = None,
    ) -> str:
        if not self.available():
            raise ProviderError(f"{self.label} is not available: {self._init_error}.")
        turns = list(history or [])
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                text = self._call(prompt, system, turns)
                if not text:
                    raise ProviderError(f"{self.label} returned an empty response.")
                return text
            except ProviderError:
                raise
            except Exception as exc:
                last_exc = exc
                if self._retryable(exc) and attempt < self.max_retries - 1:
                    delay = self.retry_base_delay * (2 ** attempt) + random.uniform(0, 1)
                    log.warning(
                        "%s call failed (%s); retrying in %.1fs (attempt %d/%d).",
                        self.label,
                        exc,
                        delay,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                break
        raise ProviderError(f"{self.label} call failed: {last_exc}") from last_exc


class ClaudeProvider(LLMProvider):
    name = "claude"
    label = "Claude"

    def _build_client(self):
        import anthropic

        self._sdk = anthropic
        return anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)

    def _call(self, prompt, system, history):
        messages = [{"role": t.role, "content": t.content} for t in history]
        messages.append({"role": "user", "content": prompt})
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
        )
        return "".join(
            block.text
            for block in resp.content
            if getattr(block, "type", None) == "text"
        )

    def _retryable(self, exc):
        retry = self._exc_tuple(
            self._sdk,
            (
                "RateLimitError",
                "APITimeoutError",
                "APIConnectionError",
                "InternalServerError",
            ),
        )
        return bool(retry) and isinstance(exc, retry)


class OpenAIProvider(LLMProvider):
    name = "openai"
    label = "ChatGPT"

    def _build_client(self):
        import openai

        self._sdk = openai
        return openai.OpenAI(api_key=self.api_key, timeout=self.timeout)

    def _call(self, prompt, system, history):
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.content} for t in history]
        messages.append({"role": "user", "content": prompt})
        # Newer reasoning models (o-series, gpt-5+) use ``max_completion_tokens``
        # and reject a custom temperature, while older models use ``max_tokens``.
        # Try the modern parameter first and fall back on a bad-request error.
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=self.max_tokens,
            )
        except getattr(self._sdk, "BadRequestError", Exception):
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
            )
        return resp.choices[0].message.content or ""

    def _retryable(self, exc):
        retry = self._exc_tuple(
            self._sdk,
            (
                "RateLimitError",
                "APITimeoutError",
                "APIConnectionError",
                "InternalServerError",
            ),
        )
        return bool(retry) and isinstance(exc, retry)


class GeminiProvider(LLMProvider):
    name = "gemini"
    label = "Gemini"

    def _build_client(self):
        from google import genai

        self._genai = genai
        return genai.Client(api_key=self.api_key)

    def _call(self, prompt, system, history):
        from google.genai import types

        contents = []
        for t in history:
            role = "model" if t.role == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=t.content)]))
        contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=self.max_tokens,
            ),
        )
        return resp.text or ""

    def _retryable(self, exc):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in (408, 429, 500, 502, 503, 504):
            return True
        name = exc.__class__.__name__.lower()
        return any(tok in name for tok in ("timeout", "connection", "servererror", "unavailable"))


#: Registry mapping provider names to (class, key-attr, model-attr).
_REGISTRY = {
    "claude": (ClaudeProvider, "anthropic_api_key", "claude_model"),
    "openai": (OpenAIProvider, "openai_api_key", "openai_model"),
    "gemini": (GeminiProvider, "google_api_key", "gemini_model"),
}


def build_provider(name: str, settings) -> Optional[LLMProvider]:
    """Build a single provider by name from settings, or ``None`` if unknown.

    The transport is chosen per provider by ``settings.backend_for(name)``:
    ``"cli"`` drives the vendor's subscription-authenticated coding-agent CLI,
    ``"api"`` uses the billed HTTP SDK. Mixing is supported, so a provider
    whose CLI is not installed can fall back to an API key without forcing the
    whole run onto billed transport.
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        log.warning("Unknown provider %r; skipping.", name)
        return None
    cls, key_attr, model_attr = entry

    backend = settings.backend_for(name)
    if backend == "cli":
        # Imported lazily: cli_providers depends on this module.
        from .cli_providers import cli_provider_classes

        cli_cls = cli_provider_classes().get(name)
        if cli_cls is None:
            log.warning("No CLI backend for %r; falling back to the API backend.", name)
        else:
            return cli_cls(
                model=settings.model_for(name),
                max_tokens=settings.max_tokens,
                timeout=settings.cli_timeout,
                max_retries=settings.max_retries,
                retry_base_delay=settings.retry_base_delay,
            )

    return cls(
        model=getattr(settings, model_attr),
        api_key=getattr(settings, key_attr),
        max_tokens=settings.max_tokens,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
    )


def build_providers(settings) -> List[LLMProvider]:
    """Build all providers in the configured order (unknown names skipped)."""
    providers = []
    for name in settings.provider_order:
        p = build_provider(name, settings)
        if p is not None:
            providers.append(p)
    return providers
