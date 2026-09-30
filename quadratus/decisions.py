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

from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Sequence, Tuple


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


def delegable() -> Tuple[str, ...]:
    return tuple(k for k, row in DECISIONS.items() if row["delegable"])
