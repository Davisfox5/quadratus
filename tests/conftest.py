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


@pytest.fixture(autouse=True, scope="session")
def _harness_scratch_stays_in_the_test_tree(tmp_path_factory):
    """Source copies, prompt files and provider scratch folders go under the
    private scratch root (``providers.scratch_root``), which by default is
    in the developer's home. Tests keep theirs in pytest's own temp tree, so
    a provider a test never cleaned up leaves nothing behind there."""
    import os

    root = tmp_path_factory.mktemp("quadratus-scratch")
    os.chmod(root, 0o700)
    previous = os.environ.get("QUADRATUS_SCRATCH_DIR")
    os.environ["QUADRATUS_SCRATCH_DIR"] = str(root)
    yield
    if previous is None:
        os.environ.pop("QUADRATUS_SCRATCH_DIR", None)
    else:
        os.environ["QUADRATUS_SCRATCH_DIR"] = previous


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


@pytest.fixture(autouse=True)
def _scripted_orchestrators_predate_the_requirements_ledger(monkeypatch, request):
    """Most tests script an orchestrator that never lists requirements. The
    ledger is on in the product; its own tests switch it back on explicitly."""
    if "requirements_ledger" not in request.keywords:
        monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "0")
