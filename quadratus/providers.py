"""Unified provider abstraction for Claude, ChatGPT and Grok.

Each provider sends the request shape its vendor documents *today* -- the
endpoint, the token-cap parameter and the model ID format were each checked
against the vendor reference on 2026-09-06 and the finding is recorded in
the class docstring, so the next audit knows what was verified and when.

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


class ProviderRefusal(ProviderError):
    """The model's safety classifiers declined the request.

    Anthropic returns this as a *successful* HTTP 200 with
    ``stop_reason: "refusal"`` and an empty or partial ``content`` -- the
    Claude API since Opus 4.7, and every Fable / Mythos model. Reading
    ``content[0]`` unguarded turns it into a misleading "empty response".
    Raised instead so the cause is visible, and so :meth:`LLMProvider.generate`
    can re-send the same request once to a configured fallback model.

    Never retried on the same model: the classifiers are deterministic for a
    given request, so a verbatim retry is a known-bad action.
    """

    def __init__(
        self,
        message: str,
        *,
        model: str = "",
        category: Optional[str] = None,
        explanation: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        #: ``stop_details.category`` when the API supplied one -- e.g. "cyber",
        #: "bio", "reasoning_extraction". None is a valid, permanent state.
        self.category = category
        self.explanation = explanation


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
        refusal_fallback_model: Optional[str] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.retry_base_delay = retry_base_delay
        #: Where a request goes when this model's classifiers decline it.
        #: None or empty means the refusal surfaces as :class:`ProviderRefusal`.
        #: Not sticky: the next call goes back to the primary model, because
        #: history here is plain text -- no thinking blocks are replayed, so
        #: there is no reasoning continuity to protect by staying switched.
        self.refusal_fallback_model = refusal_fallback_model or None
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

    #: How much reasoning the seat asks for, where the transport exposes a
    #: dial. Empty means "whatever the CLI does by default".
    effort: str = ""
    #: Whether this seat gets a bounded call instead of a full agent loop.
    #: Meaningless to an HTTP API, which is a completion already -- the
    #: attribute lives on the base so routing can set it without knowing
    #: which transport is underneath.
    restricted: bool = False

    def for_seat(
        self,
        model: Optional[str],
        *,
        effort: str = "",
        restricted: bool = False,
    ) -> "LLMProvider":
        """A view of this provider bound to one seat's whole invocation.

        A seat is not only a model. Two seats can address the same model and
        still want different calls -- a brain-trust member exploring a
        repository and a worker answering one question are the same weights
        run very differently, and on a subscription the difference is most of
        the cost. So the identity that matters here is (model, effort,
        restricted), and a clone is made whenever any of the three differs.
        """
        same = (
            (model is None or model == self.model)
            and effort == self.effort
            and restricted == self.restricted
        )
        if same:
            return self
        clone = copy.copy(self)
        if model is not None:
            clone.model = model
        clone.effort = effort
        clone.restricted = restricted
        return clone

    def for_model(self, model: Optional[str]) -> "LLMProvider":
        """Return a view of this provider bound to a different model.

        Model routing sends cheap phases (convergence judging, classification,
        summarisation) to a smaller model on the same subscription. Copying the
        provider keeps the already-built client and avoids re-resolving a CLI
        binary or re-constructing an SDK client on every routed call.

        ``None`` means "no change". The empty string is different and load
        bearing: it means *name no model*, so the CLI applies its own current
        default. Collapsing the two would silently send whichever model this
        provider happened to be constructed with.
        """
        if model is None or model == self.model:
            return self
        clone = copy.copy(self)
        clone.model = model
        return clone

    @property
    def status(self) -> str:
        if not self.available():
            return f"{self.label} unavailable ({self._init_error})"
        # An empty model is a value here, not a gap: it means the CLI picks,
        # which is how a seat stays on whatever the vendor currently ships.
        return f"{self.label} ({self.model or 'CLI default'})"

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
        try:
            return self._generate_once(prompt, system, turns)
        except ProviderRefusal as refusal:
            fallback = getattr(self, "refusal_fallback_model", None)
            if not fallback or fallback == self.model:
                raise
            log.warning(
                "%s (%s) declined the request%s; re-sending once on %s.",
                self.label,
                self.model,
                f" [{refusal.category}]" if refusal.category else "",
                fallback,
            )
            try:
                return self.for_model(fallback)._generate_once(prompt, system, turns)
            except ProviderRefusal as second:
                raise ProviderRefusal(
                    f"{self.label}: both {self.model} and the fallback {fallback} "
                    f"declined the request ({second}).",
                    model=fallback,
                    category=second.category,
                    explanation=second.explanation,
                ) from second

    def _generate_once(self, prompt: str, system: str, turns: List[Turn]) -> str:
        """One model's attempt, with transport retries. Refusals pass through."""
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


def _field(obj, name: str):
    """Read ``name`` off an SDK model or a plain dict; None when absent."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class ClaudeProvider(LLMProvider):
    """Claude over the Messages API.

    Verified against Anthropic's reference on 2026-09-06: ``messages.create``
    with ``system`` and ``max_tokens``; no sampling parameters (``temperature``
    and friends return 400 on Opus 4.7 and later); omitting ``thinking`` runs
    adaptive thinking on Claude Opus 5; a classifier decline is an HTTP 200 with
    ``stop_reason: "refusal"`` and a ``stop_details`` object, which is why
    :meth:`_call` branches on the stop reason before reading content.
    """

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
        # Branch on stop_reason before touching content: a classifier decline
        # is an HTTP 200 whose content is empty (pre-output) or partial
        # (mid-stream), and a partial must not be mistaken for an answer.
        if _field(resp, "stop_reason") == "refusal":
            details = _field(resp, "stop_details")
            category = _field(details, "category")
            explanation = _field(details, "explanation")
            raise ProviderRefusal(
                f"{self.label} ({self.model}) declined the request"
                + (f" [{category}]" if category else "")
                + (f": {explanation}" if explanation else "")
                + ".",
                model=self.model,
                category=category,
                explanation=explanation,
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


class _OpenAISDKProvider(LLMProvider):
    """Shared client construction and retry rules for the OpenAI Python SDK.

    Two vendors speak this SDK -- OpenAI itself and xAI -- but they do not
    share an endpoint (see the subclasses), so only the client and the error
    classes live here.
    """

    #: Override to point the same SDK at a compatible host.
    base_url: Optional[str] = None

    def _build_client(self):
        import openai

        self._sdk = openai
        kwargs = {"api_key": self.api_key, "timeout": self.timeout}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return openai.OpenAI(**kwargs)

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


class OpenAIProvider(_OpenAISDKProvider):
    """ChatGPT over the Responses API (``POST /v1/responses``).

    Verified against OpenAI's API reference on 2026-09-06: "While Chat
    Completions remains supported, Responses is recommended for all new
    projects", and the ``-pro`` models (``gpt-5.5-pro`` among them) list Chat
    Completions as *not supported* -- they exist only on Responses and Batch.
    Responses therefore covers every current model; Chat Completions does not.

    ``instructions`` carries the system prompt, ``input`` the turn list, and
    ``max_output_tokens`` the cap. ``temperature`` is deliberately not sent:
    the reasoning models reject it.
    """

    name = "openai"
    label = "ChatGPT"

    def _call(self, prompt, system, history):
        turns = [{"role": t.role, "content": t.content} for t in history]
        turns.append({"role": "user", "content": prompt})
        resp = self._client.responses.create(
            model=self.model,
            instructions=system,
            input=turns,
            max_output_tokens=self.max_tokens,
        )
        return resp.output_text or ""


#: xAI serves Grok over an OpenAI-compatible API on its own host.
XAI_BASE_URL = "https://api.x.ai/v1"


class GrokProvider(_OpenAISDKProvider):
    """Grok over xAI's billed API (``POST /v1/chat/completions``).

    Verified against docs.x.ai on 2026-09-06: the documented integration is
    the OpenAI Python SDK with ``base_url="https://api.x.ai/v1"`` against Chat
    Completions, with ``max_completion_tokens`` as the token cap (``max_tokens``
    is marked deprecated there) and the ``system`` role accepted in
    ``messages``. xAI also exposes ``/v1/responses``, but Chat Completions is
    what its reference documents for every current text model, so that is
    what this sends.
    """

    name = "grok"
    label = "Grok"
    base_url = XAI_BASE_URL

    def _call(self, prompt, system, history):
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.content} for t in history]
        messages.append({"role": "user", "content": prompt})
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_completion_tokens=self.max_tokens,
        )
        return resp.choices[0].message.content or ""


#: Registry mapping provider names to (class, key-attr, model-attr).
_REGISTRY = {
    "claude": (ClaudeProvider, "anthropic_api_key", "claude_model"),
    "openai": (OpenAIProvider, "openai_api_key", "openai_model"),
    "grok": (GrokProvider, "xai_api_key", "grok_model"),
}


def build_provider(name: str, settings, *, allow_writes: bool = False, workdir=None) -> Optional[LLMProvider]:
    """Build a single provider by name from settings, or ``None`` if unknown.

    The transport is chosen per provider by ``settings.backend_for(name)``:
    ``"cli"`` drives the vendor's subscription-authenticated coding-agent CLI,
    ``"api"`` uses the billed HTTP SDK. Mixing is supported, so a provider
    whose CLI is not installed can fall back to an API key without forcing the
    whole run onto billed transport -- but only when the operator asks for it
    in so many words. A missing CLI reports itself unavailable rather than
    silently moving that provider onto billed transport, because the whole
    point of the default is that a run costs nothing per token.

    ``allow_writes`` reaches the CLI backends only. It is off by default and
    should stay off for anything that is reviewing rather than building: a
    coding agent asked merely to critique will edit the working tree, and
    several of them at once is write-thrash. The API backends have no file
    access to grant.
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
                allow_writes=allow_writes,
                workdir=workdir,
                max_tokens=settings.max_tokens,
                timeout=settings.cli_timeout,
                max_retries=settings.max_retries,
                retry_base_delay=settings.retry_base_delay,
                refusal_fallback_model=settings.refusal_fallback_for(name),
            )

    return cls(
        model=getattr(settings, model_attr),
        api_key=getattr(settings, key_attr),
        max_tokens=settings.max_tokens,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
        refusal_fallback_model=settings.refusal_fallback_for(name),
    )


def build_providers(settings) -> List[LLMProvider]:
    """Build all providers in the configured order (unknown names skipped)."""
    providers = []
    for name in settings.provider_order:
        p = build_provider(name, settings)
        if p is not None:
            providers.append(p)
    return providers
