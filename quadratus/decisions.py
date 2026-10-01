"""Fixed-answer routing decisions, and where an external decider would plug in.

OpenAI announced a Decisions API at DevDay on 2026-09-29: GPT-6 Luna picks
one of a developer's predefined answers from text or image context, in about
150 ms. It is the shape of several routing decisions this engine makes with
deterministic rules today. As of 2026-09-30 it is a limited preview with no
published endpoint, schema, model id or pricing, and the openai package
(3.22.1) has no decisions resource; a transport written now would be a guess,
and this repo's rule is that probes outrank tables.

So this module holds the part that is true regardless of the contract: the
inventory of decisions with a finite answer set (``DECISIONS``), the request
shape a decider is handed (``Decision``), the protocol it answers
(``Decider``), and a refusal that names what is missing
(``OpenAIDecisionsDecider``). Every decision keeps its deterministic rule as
the default and as the fallback; an external decider is an opt-in that must
be metered as a billed API call (``usage.py``) and never a silent
substitution for the ladder (CLAUDE.md: subscription transport is the default
and is not silently abandoned).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

JEV_API_KEY_ENV = "TYPESAFE_API_KEY"
JEV_BASE_URL_ENV = "TYPESAFE_BASE_URL"
JEV_DEFAULT_MODEL = "jev-latest"
#: Vercel's AI Gateway fronts Jev behind a TypeSafe-compatible API (its
#: docs: base URL ``/typesafe``, model ``typesafe-ai/jev``, the gateway key
#: as the bearer). A ``vck_`` key is a gateway key, so those become the
#: defaults for it; an explicit base URL or model still wins.
JEV_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/typesafe"
JEV_GATEWAY_MODEL = "typesafe-ai/jev"
JEV_GATEWAY_KEY_PREFIX = "vck_"


@dataclass(frozen=True)
class Decision:
    """One question with a fixed answer set and the context to judge it on."""

    id: str
    question: str
    answers: Tuple[str, ...]
    context: str = ""
    #: Where the same decision is made today, for the record.
    rule: str = ""


@dataclass(frozen=True)
class Verdict:
    decision: str
    answer: str
    #: "rule" for the deterministic default, or the decider's own name.
    source: str
    confidence: Optional[float] = None
    note: str = ""


class Decider(Protocol):
    def decide(self, decision: Decision) -> Verdict: ...


class DecisionsUnavailable(RuntimeError):
    """An external decider was asked for and cannot be called yet."""


#: Every routing decision with a finite answer set the engine makes today, its
#: answers, and the rule that answers it. The deterministic rule stays the
#: default; an external decider may only be consulted where the row says so.
DECISIONS: Dict[str, dict] = {
    "task.difficulty": dict(
        question="How hard is this task for a coding model?",
        answers=("rote", "simple", "standard", "complex"),
        rule="the orchestrator's KIND line; unlabelled tasks default with confidence 'defaulted' "
             "(taskmeta.parse_metadata)",
        delegable=True,
        note="the highest-value candidate: today an unlabelled task silently rides the default rung",
    ),
    "task.kind": dict(
        question="What kind of work is this task?",
        answers=("general", "frontend", "backend", "testing", "security", "docs", "review", "architect"),
        rule="the orchestrator's KIND line; unrecognised kinds degrade to general (taskmeta.parse_metadata)",
        delegable=True,
        note="answers must be read from task_kinds.KINDS at call time, not this seed",
    ),
    "task.tier": dict(
        question="Is this a small, local, reversible change?",
        answers=("direct", "normal"),
        rule="TIER label plus deterministic admission on the contract (session._direct_refusal)",
        delegable=False,
        note="admission reads facts the harness holds (paths, size, checks); a model's opinion is not "
             "one of them, so this stays deterministic",
    ),
    "worker.escalate": dict(
        question="Does this errand need the stronger model in its family?",
        answers=("base", "escalate"),
        rule="the lead's 'demanding' flag (workers.pick_worker)",
        delegable=True,
        note="in-family only; the answer never changes vendor",
    ),
    "reply.control": dict(
        question="Is this reply a task, a control request, or a delivery?",
        answers=("task", "DONE", "ASK", "FETCH", "CONSULT", "WORKER", "delivery"),
        rule="bounded preface scan (taskmeta.parse_control)",
        delegable=False,
        note="a parser the prompt states in words; a probabilistic reading would break prompt parity",
    ),
    "security.route": dict(
        question="Is this task security work that must leave the primary seat?",
        answers=("general", "security"),
        rule="WorkClass on the task (task_kinds; session._run_security_task)",
        delegable=False,
        note="a refusal-avoidance rule; misrouting spends a refusal, so it stays explicit",
    ),
}


class RuleDecider:
    """The deterministic default: answers only from an explicit rule result the
    caller already computed. It exists so call sites have one shape whether
    the answer came from a rule or a decider."""

    name = "rule"

    def decide(self, decision: Decision, *, answer: str) -> Verdict:  # type: ignore[override]
        if answer not in decision.answers:
            raise ValueError(f"{decision.id}: {answer!r} is not one of {decision.answers}")
        return Verdict(decision=decision.id, answer=answer, source=self.name)


class OpenAIDecisionsDecider:
    """Placeholder for the Decisions API transport. Refuses with the reason
    until the contract is published; the refusal is the record that nothing
    was guessed."""

    name = "openai.decisions"
    #: Set to the documented endpoint once published; None means unknown.
    endpoint: Optional[str] = None
    #: What is still missing, kept in one place so the message stays true.
    missing: Sequence[str] = ("endpoint path", "request and response schema", "model id", "pricing",
                              "an openai SDK release with a decisions resource")

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    def available(self) -> str:
        """"" when callable, else why not."""
        if self.endpoint is None:
            return ("the OpenAI Decisions API contract is not published (limited preview, 2026-09-30): "
                    + ", ".join(self.missing) + " are unknown")
        if not self.api_key:
            return "no OpenAI API key: the Decisions API is a billed endpoint, not a subscription call"
        return ""

    def decide(self, decision: Decision) -> Verdict:
        reason = self.available()
        if reason:
            raise DecisionsUnavailable(reason)
        raise DecisionsUnavailable("transport not implemented: write it against the published contract")


class JevDecider:
    """TypeSafe AI's Jev through the ``typesafe-sdk`` package (2026-09-30):
    ``POST /v1/systemone`` with a state and named questions; a ``choice``
    question returns the chosen label, a confidence and per-label
    probabilities, plus billable input tokens (output tokens are free per
    the SDK's own usage schema). Text only: Jev has no image input.

    Every call is a billed API call and is metered when a meter is given.
    The SDK is an optional extra (``quadratus[decisions]``) and the key is
    ``TYPESAFE_API_KEY``; without either the decider refuses with the reason
    and the caller keeps its rule. ``client`` may be injected for tests; the
    duck type is ``system_one(state=, questions=, model=)`` returning an
    object with ``.model``, ``.usage.input_tokens/.output_tokens`` and
    ``.choices[name].choice/.confidence/.probabilities``.
    """

    name = "jev"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 client: Any = None, meter: Any = None, timeout: float = 10.0,
                 base_url: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.environ.get(JEV_API_KEY_ENV, "").strip()
        gateway = self.api_key.startswith(JEV_GATEWAY_KEY_PREFIX)
        self.base_url = (base_url or os.environ.get(JEV_BASE_URL_ENV, "").strip()
                         or (JEV_GATEWAY_BASE_URL if gateway else None))
        self.model = (model or os.environ.get("TYPESAFE_DEFAULT_MODEL", "").strip()
                      or (JEV_GATEWAY_MODEL if gateway else JEV_DEFAULT_MODEL))
        self._client = client
        self.meter = meter
        self.timeout = timeout
        self.calls: List[dict] = []

    def available(self) -> str:
        """"" when callable, else why not. Never calls the network."""
        if self._client is not None:
            return ""
        try:
            import typesafe_sdk  # noqa: F401
        except ImportError:
            return "typesafe-sdk is not installed (pip install 'quadratus[decisions]')"
        if not self.api_key:
            return f"no {JEV_API_KEY_ENV}: Jev is a billed API, not a subscription call"
        return ""

    def _sdk_client(self):
        if self._client is None:
            from typesafe_sdk import TypeSafeClient
            self._client = TypeSafeClient(api_key=self.api_key, model=self.model, timeout=self.timeout,
                                          **({"base_url": self.base_url} if self.base_url else {}))
        return self._client

    @property
    def host(self) -> str:
        """Where calls go, for the record: "typesafe" or "vercel-gateway"."""
        return "vercel-gateway" if (self.base_url or "").startswith(JEV_GATEWAY_BASE_URL) else "typesafe"

    def probe(self) -> List[str]:
        """The model names the account can call: the round trip is the fact
        recorded, as ``quadratus --probe`` does for the CLIs. A gateway may
        not serve the models list; a decision call is then the probe."""
        reason = self.available()
        if reason:
            raise DecisionsUnavailable(reason)
        try:
            listed = self._sdk_client().models.list()
        except Exception as exc:  # noqa: BLE001 -- the SDK's own error classes, reported as text
            raise DecisionsUnavailable(f"Jev probe failed: {type(exc).__name__}: {exc}") from exc
        return [m.name for m in getattr(listed, "models", ())]

    def decide(self, decision: Decision) -> Verdict:
        reason = self.available()
        if reason:
            raise DecisionsUnavailable(reason)
        name = decision.id.replace(".", "_")
        question = {"type": "choice", "instructions": decision.question,
                    "criteria": {answer: None for answer in decision.answers}}
        try:
            response = self._sdk_client().system_one(
                state=decision.context or decision.question, questions={name: question}, model=self.model)
        except Exception as exc:  # noqa: BLE001 -- refused, never guessed; the caller keeps its rule
            raise DecisionsUnavailable(f"Jev call failed: {type(exc).__name__}: {exc}") from exc
        usage = getattr(response, "usage", None)
        model = getattr(response, "model", None) or self.model
        record = dict(decision=decision.id, model=model, host=self.host,
                      input_tokens=getattr(usage, "input_tokens", None),
                      output_tokens=getattr(usage, "output_tokens", None))
        self.calls.append(record)
        if self.meter is not None:
            try:
                self.meter.record(model=f"jev:{model}", prompt=decision.context, reply="",
                                  input_tokens=record["input_tokens"], output_tokens=record["output_tokens"])
            except Exception:  # noqa: BLE001 -- metering is observational only
                pass
        answer = getattr(response, "choices", {}).get(name)
        if answer is None:
            raise DecisionsUnavailable(f"Jev returned no answer for {decision.id}")
        choice = getattr(answer, "choice", None)
        if choice not in decision.answers:
            raise DecisionsUnavailable(f"Jev answered {choice!r}, not one of {decision.answers}")
        probabilities = dict(getattr(answer, "probabilities", {}) or {})
        return Verdict(decision=decision.id, answer=choice, source=f"jev:{model}",
                       confidence=getattr(answer, "confidence", None),
                       note=json.dumps(probabilities, sort_keys=True))


def decider_from_name(name: Optional[str], *, meter: Any = None):
    """The decider an operator named: None or "rule" for the deterministic
    default, "jev" for TypeSafe's Jev. Unknown names are errors, not defaults."""
    if not name or name == "rule":
        return None
    if name == "jev":
        return JevDecider(meter=meter)
    raise ValueError(f"unknown decider {name!r}: expected 'rule' or 'jev'")


def delegable() -> Tuple[str, ...]:
    return tuple(k for k, row in DECISIONS.items() if row["delegable"])
