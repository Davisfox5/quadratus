"""Shared test fixtures and fakes (no network access required)."""

from __future__ import annotations

from typing import List, Sequence

import pytest

from quadratus.config import Settings
from quadratus.providers import LLMProvider, ProviderRefusal, Turn


class FakeProvider(LLMProvider):
    """An in-memory provider used to exercise the orchestrator without network.

    It records every call and returns a deterministic, inspectable response.
    Set ``fail_times`` to simulate transient failures before success, and
    ``unavailable=True`` to simulate a missing key/SDK.
    """

    def __init__(
        self,
        name: str,
        label: str,
        *,
        unavailable: bool = False,
        fail_times: int = 0,
        retryable: bool = True,
        max_retries: int = 4,
        refuse: bool = False,
        refusal_fallback_model: str = "",
    ):
        self.name = name
        self.label = label
        self.calls: List[dict] = []
        self._fail_times = fail_times
        self._is_retryable = retryable
        #: Simulate a classifier decline (stop_reason "refusal") on every call.
        self._refuse = refuse
        self.refusal_fallback_model = refusal_fallback_model or None
        self._attempts = 0
        # Bypass the network-bound base __init__.
        self.model = f"{name}-test"
        self.api_key = None if unavailable else "test-key"
        self.max_tokens = 8000
        self.timeout = 1.0
        self.max_retries = max_retries
        self.retry_base_delay = 0.0
        self._client = None if unavailable else object()
        self._init_error = "no API key configured" if unavailable else None

    def _call(self, prompt: str, system: str, history: Sequence[Turn]) -> str:
        self._attempts += 1
        if self._attempts <= self._fail_times:
            raise RuntimeError(f"transient failure {self._attempts}")
        if self._refuse:
            raise ProviderRefusal(
                f"{self.label} ({self.model}) declined the request [cyber].",
                model=self.model, category="cyber",
            )
        self.calls.append({"prompt": prompt, "system": system, "history": list(history)})
        return f"[{self.label}] response to: {prompt[:40]}"

    def _retryable(self, exc: Exception) -> bool:  # type: ignore[override]
        return self._is_retryable


@pytest.fixture
def settings() -> Settings:
    s = Settings(
        openai_api_key="x",
        anthropic_api_key="x",
        xai_api_key="x",
        backend="api",
    )
    s.retry_base_delay = 0.0
    return s


#: The installed codex client's catalogue as the Mac held it on 2026-10-24
#: (models_cache.json, client 0.163.0): the families this engine routes to,
#: with the efforts each lists. Tests point the resolver at a fresh copy so
#: no test reads the developer's own ~/.codex; a test about a stale,
#: missing or malformed catalogue writes its own and sets the variable.
CODEX_CATALOG_MODELS = [
    dict(slug="gpt-6.1-sol", display_name="GPT-6.1 Sol", visibility="list", default_reasoning_level="low",
         supported_reasoning_levels=[dict(effort=e) for e in ("low", "medium", "high", "xhigh", "max", "ultra")]),
    dict(slug="gpt-6-astra", display_name="GPT-6 Astra", visibility="list", default_reasoning_level="low",
         supported_reasoning_levels=[dict(effort=e) for e in ("low", "medium", "high", "xhigh", "max", "ultra")]),
    dict(slug="gpt-6-luna", display_name="GPT-6 Luna", visibility="list", default_reasoning_level="low",
         supported_reasoning_levels=[dict(effort=e) for e in ("low", "medium", "high", "xhigh", "max")]),
    dict(slug="gpt-5.6-terra", display_name="GPT-5.6 Terra", visibility="list", default_reasoning_level="low",
         supported_reasoning_levels=[dict(effort=e) for e in ("low", "medium", "high", "xhigh", "max", "ultra")]),
    dict(slug="gpt-6-sol", display_name="GPT-6 Sol", visibility="list",
         supported_reasoning_levels=[dict(effort=e) for e in ("low", "medium", "high", "xhigh")]),
    dict(slug="gpt-5.6-sol", display_name="GPT-5.6 Sol", visibility="list",
         supported_reasoning_levels=[dict(effort=e) for e in ("low", "medium", "high", "xhigh")]),
    dict(slug="gpt-5.6-luna", display_name="GPT-5.6 Luna", visibility="list",
         supported_reasoning_levels=[dict(effort=e) for e in ("low", "medium", "high")]),
]


def write_codex_catalog(path, models=None, *, client_version="0.163.0", fetched_at=None):
    """Write a catalogue file in the client's shape; returns the path."""
    import json
    import time
    from datetime import datetime, timezone
    fetched = fetched_at or datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(dict(fetched_at=fetched, client_version=client_version,
                                    models=list(CODEX_CATALOG_MODELS if models is None else models))))
    return path


@pytest.fixture(scope="session")
def _codex_catalog_file(tmp_path_factory):
    return write_codex_catalog(tmp_path_factory.mktemp("codex") / "models_cache.json")


@pytest.fixture(autouse=True)
def _the_installed_codex_catalogue(monkeypatch, _codex_catalog_file):
    """Every test resolves OpenAI families against the fixture catalogue
    unless it sets QUADRATUS_CODEX_CATALOG itself."""
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(_codex_catalog_file))
    # The grok listing is a subprocess of the real binary; tests with a fake
    # CLI script must not see it in their argv logs. The dedicated tests
    # turn it on with a scripted runner.
    monkeypatch.setenv("QUADRATUS_GROK_MODELS", "0")
    for name in ("QUADRATUS_ALIAS_OPENAI_SOL", "QUADRATUS_ALIAS_OPENAI_ASTRA", "QUADRATUS_ALIAS_OPENAI_LUNA",
                 "QUADRATUS_ALIAS_OPENAI_TERRA", "QUADRATUS_CATALOG_MAX_AGE_HOURS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _scripted_orchestrators_predate_the_requirements_ledger(monkeypatch, request):
    """Most tests script an orchestrator that never lists requirements. The
    ledger is on in the product; its own tests switch it back on explicitly."""
    if "requirements_ledger" not in request.keywords:
        monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "0")
