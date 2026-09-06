"""Which kind of work goes to which model, and how sure we are.

This is separate from :mod:`quadratus.registry` on purpose. The registry says
what shape a model is -- wide window, no classifier, cheap. This module says
what to *do* with that, which is a much weaker kind of claim and ages faster.
Keeping them apart means a routing opinion can be revised without touching the
roster, and a new model can join the roster without anyone inventing a routing
opinion for it.

How the confidence field is meant to be used
--------------------------------------------
Every policy carries a confidence and the evidence behind it, and the two
control behaviour rather than merely documenting it:

* **High and medium confidence pin a model.** There is a measured reason to
  prefer one, so rotation is overridden.
* **Low confidence deliberately does not.** The lead rotates as normal, which
  spreads the work across separate subscription windows *and* accumulates the
  head-to-head data that would let this entry be upgraded later. Pinning on a
  hunch would freeze the hunch in place and destroy the only evidence that
  could correct it.

So an empty ``prefer`` is a positive statement -- "no credible basis to choose"
-- and not an oversight.

What the evidence actually supports
-----------------------------------
Three findings drove most of this, and they are worth stating plainly because
they outrank nearly every per-model preference below:

1. **Size dominates model choice for review.** Review F1 measured 0.657 on
   diffs under 10 lines and 0.043 on diffs over 150. That spread is far wider
   than any gap between frontier reviewers, which is why
   :data:`MAX_TASK_LINES` exists and why it is enforced at decomposition time
   rather than left to a model's discretion.
2. **Precision and recall split across vendors.** No single reviewer is good;
   two with opposite failure modes are. This is why REVIEW pins *both* rather
   than choosing.
3. **Some work should not be routed to a model at all.** Performance is the
   clear case: recall on performance defects is near zero across every model
   tested, while a profiler answers the question directly. ``tool_first``
   records that, so the harness can say so instead of quietly assigning it.

Caveats that apply to the whole table: benchmark figures cited by a vendor
about its own model are self-serving often enough to be worth discounting --
one code-review vendor reported 82% on its own test set against 45% when an
independent party re-ran the same repositories. Per-language routing is absent
because no credible per-language data exists for this model generation; the
best-known polyglot benchmark stopped being updated in late 2025 and contains
no 2026 models at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Confidence",
    "TaskKind",
    "KindPolicy",
    "ROUTING",
    "DIFFICULTY_LADDER",
    "MAX_TASK_LINES",
    "policy_for",
    "route",
    "guidance_for",
]


#: The size ceiling handed to the orchestrator at decomposition time.
#:
#: Not a style preference. Review quality collapses with diff size -- roughly
#: an order of magnitude of F1 between a sub-10-line change and a 150-line one
#: -- so a task that produces a large diff has already lost most of the review
#: that was supposed to catch its defects. Splitting the task is the only
#: intervention that recovers it; no reviewer choice comes close.
MAX_TASK_LINES = 100


class Confidence:
    """How much evidence stands behind a routing policy.

    LOW is not a placeholder awaiting a better guess -- see the module
    docstring. It routes by rotation on purpose.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskKind:
    """Kinds of coding work, split where the split changes the routing.

    Deliberately not a tidy taxonomy. ``BACKEND``/``FRONTEND``/``MOBILE`` are
    separate only because the evidence separates them -- one model is measurably
    behind on real mobile work and not on the others. Where no evidence
    separates two kinds, they are one kind here, because a distinction that
    changes nothing is a distinction that will drift out of sync.
    """

    #: Turning an operator's request into a stated goal and constraints.
    SCOPE = "scope"
    #: Breaking a goal into tasks. Where MAX_TASK_LINES is applied.
    DECOMPOSE = "decompose"
    #: Open-ended structural judgement, before code exists.
    ARCHITECT = "architect"

    BACKEND = "backend"
    FRONTEND = "frontend"
    MOBILE = "mobile"
    #: High-volume mechanical coding where turn efficiency is the binding cost.
    BULK = "bulk"
    #: Wiring existing pieces together. Low judgement, high tedium.
    GLUE = "glue"

    #: Finding defects nobody has reported yet.
    REVIEW = "review"
    #: Root-causing a defect that has already announced itself.
    DEBUG = "debug"
    #: Anything whose failure mode is a race, a deadlock, or a lost update.
    CONCURRENCY = "concurrency"
    SECURITY = "security"

    REFACTOR = "refactor"
    TEST = "test"
    #: Reading an existing codebase to answer a question about it.
    COMPREHEND = "comprehend"
    IAC = "iac"
    DOCS = "docs"
    PERF = "perf"
    #: Schema, query, and migration work.
    DATA = "data"

    #: Anything unclassified. Rotates.
    GENERAL = "general"


@dataclass(frozen=True)
class KindPolicy:
    """What to do with one kind of task.

    Attributes:
        prefer: Models to use, in order. Empty means rotate -- see the module
            docstring; this is a deliberate value, not a missing one.
        exclude: Models that must not lead this kind regardless of rotation.
            Reserved for measured deficits, not impressions.
        confidence: How much the ``prefer`` list should be trusted.
        evidence: What ``prefer`` and ``exclude`` rest on, in one line. Present
            so a future reader can judge whether the basis still holds rather
            than inheriting a preference with no provenance.
        gate: A check that must pass before the work is accepted, expressed as
            an instruction to the lead. Used where no model is reliable and a
            deterministic check is, so the failure is caught rather than routed
            around.
        tool_first: Set when a deterministic tool answers the question better
            than any model. The work still gets a lead, but the lead is told to
            reach for the tool before reasoning about it.
        human_check: True where the best measured agent performance is far
            enough below a competent human that the result should not be
            accepted unreviewed.
    """

    prefer: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()
    confidence: str = Confidence.LOW
    evidence: str = ""
    gate: Optional[str] = None
    tool_first: Optional[str] = None
    human_check: bool = False

    @property
    def pins_a_model(self) -> bool:
        return bool(self.prefer) and self.confidence != Confidence.LOW


OPUS = "claude:opus"
SOL = "openai:gpt-5.6-sol"
LUNA = "openai:gpt-5.6-luna"
SONNET = "claude:sonnet"
GEMINI_PRO = "gemini:gemini-3.1-pro-preview"
GROK = "grok:grok-4.6"
FABLE = "claude:fable"


#: Difficulty -> lead. The primary routing axis; kind pins are the exception.
#:
#: An earlier version of this table pinned most kinds to Opus or Sol on
#: benchmark evidence. That was locally right and globally wrong: it
#: concentrated nearly every invocation on two subscriptions while the Grok
#: and Google windows sat idle, which is exactly how one window exhausts early
#: and forces a degraded run while capacity elsewhere goes unspent. Routing by
#: difficulty spreads the load across all four subscriptions *and* still puts
#: the strongest model on the work that actually needs it.
#:
#: The rungs, per the operator's read of current capability:
#: * COMPLEX -- many logical steps, high stakes -> Opus 5, top of the pack.
#: * STANDARD -- real judgement, not the hardest -> GPT-5.6 Sol.
#: * SIMPLE -- the bulk of well-sized (<=100-line) tasks -> Grok 4.6, which is
#:   capable at this grade and cheap against its own window; expected to close
#:   the gap further with 4.7.
#: * ROTE -- mechanical work -> Gemini 3.1 Pro, currently the least proven of
#:   the four; revisit the rung when 3.5 Pro ships.
#:
#: A rung that is unavailable or excluded escalates upward (a stronger model
#: can always do easier work) before it degrades downward.
DIFFICULTY_LADDER: Dict[str, str] = {
    "complex": OPUS,
    "standard": SOL,
    "simple": GROK,
    "rote": GEMINI_PRO,
}

#: Least to most capable, for escalation.
_LADDER_ORDER: Tuple[str, ...] = (GEMINI_PRO, GROK, SOL, OPUS)


#: The routing table. Read the ``evidence`` line before changing a row.
ROUTING: Dict[str, KindPolicy] = {
    TaskKind.SCOPE: KindPolicy(
        prefer=(FABLE,),
        confidence=Confidence.HIGH,
        evidence=(
            "Structural, not comparative: the orchestrator is the only "
            "participant that persists, so it is the only one that can hold "
            "the operator's stated goal across the session."
        ),
    ),
    TaskKind.DECOMPOSE: KindPolicy(
        prefer=(FABLE,),
        confidence=Confidence.HIGH,
        evidence=(
            "Same structural reason as SCOPE. The load-bearing part is not who "
            f"decomposes but the {MAX_TASK_LINES}-line ceiling it must respect."
        ),
        gate=(
            f"No task may be sized to produce more than ~{MAX_TASK_LINES} lines "
            "of diff. Split it instead."
        ),
    ),
    TaskKind.ARCHITECT: KindPolicy(
        confidence=Confidence.MEDIUM,
        evidence=(
            "Independent proposals beat any single model's, and the harness "
            "around the debate mattered more than which models were in it. "
            "Rotation is correct here: the diversity is the product."
        ),
    ),

    TaskKind.BACKEND: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the difficulty ladder. Opus leads agentic benchmarks here "
            "(Terminal-Bench 3.0 43.5 vs 34.6), which is why hard backend work "
            "reaches it via COMPLEX -- but pinning every backend task to one "
            "subscription starves the others and exhausts it first."
        ),
    ),
    TaskKind.FRONTEND: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the difficulty ladder: the harness dominates here -- a "
            "screenshot-verification loop changes outcomes more than the "
            "model does -- so the gate matters and the pin did not."
        ),
        tool_first=(
            "a rendered-page check (quadratus.browser.render_page: screenshot, "
            "console errors, failed requests from a real headless browser)"
        ),
        gate=(
            "Verify the rendered result visually before claiming it works; a "
            "frontend change that compiles is not a frontend change that works. "
            "Late console errors count: frameworks routinely throw after load."
        ),
    ),
    TaskKind.MOBILE: KindPolicy(
        exclude=(GEMINI_PRO,),
        confidence=Confidence.HIGH,
        evidence=(
            "The exclusion is the finding: on a real-world Android/Kotlin "
            "benchmark run by the toolchain vendor, the excluded model landed "
            "roughly 20 points behind. Directly verified, unlike most of this "
            "table."
        ),
    ),
    TaskKind.BULK: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the ladder; bulk work is SIMPLE or ROTE by nature, which "
            "lands it on Grok -- ~4x turn efficiency measured -- or Gemini "
            "without needing a pin."
        ),
    ),
    TaskKind.GLUE: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the ladder as SIMPLE or ROTE. The earlier Luna pin saved "
            "little: Luna spends the same OpenAI window as Sol, so it did not "
            "spread load across subscriptions, which is the point now."
        ),
    ),

    TaskKind.REVIEW: KindPolicy(
        prefer=(SOL, OPUS),
        confidence=Confidence.HIGH,
        evidence=(
            "Measured opposite failure modes: ~70% recall / ~32% precision "
            "against ~39% precision / ~55% recall. Neither is a good reviewer "
            "alone; the pair covers what each misses. Both, never one."
        ),
        gate=(
            "Collect every finding with a severity and confidence. Do not "
            "filter: an instruction to be selective suppresses recall."
        ),
    ),
    TaskKind.DEBUG: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the ladder: a gnarly root-cause is COMPLEX and reaches "
            "Opus (vendor-stated strength) that way; a shallow one does not "
            "need it. Still a separate kind from REVIEW -- different jobs."
        ),
    ),
    TaskKind.CONCURRENCY: KindPolicy(
        confidence=Confidence.HIGH,
        evidence=(
            "The finding is that nobody is competent here, so there is no one "
            "to prefer. Races and lost updates dominate the failures and no "
            "model reliably reasons about interleavings. Gate, do not route."
        ),
        gate=(
            "Concurrent code is not accepted on inspection. Add a test that "
            "actually interleaves -- stress, fuzz, or a deterministic scheduler "
            "-- and show it failing before the fix and passing after."
        ),
        human_check=True,
    ),
    TaskKind.SECURITY: KindPolicy(
        prefer=(SOL,),
        confidence=Confidence.HIGH,
        evidence=(
            "Operator preference from direct experience, which is the strongest "
            "evidence available: no public security-specific capability data "
            "exists for these models. The orchestrator is excluded separately "
            "because its classifiers can refuse benign security work."
        ),
        exclude=(FABLE,),
    ),

    TaskKind.REFACTOR: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the ladder. Best measured agent performance sits at 22-41% "
            "against ~87% for a competent human -- the gap is the point, and "
            "no pin closes it; the human check does."
        ),
        human_check=True,
        gate=(
            "A refactor must not change behaviour. Show the existing tests "
            "passing unchanged, and say plainly if none cover the touched code."
        ),
    ),
    TaskKind.TEST: KindPolicy(
        prefer=(SOL,),
        confidence=Confidence.HIGH,
        evidence=(
            "Operator directive: testing always goes to Sol, alongside "
            "security. The recall-leaning bias fits enumerating cases; the "
            "mutation gate below covers what generated suites overstate."
        ),
        gate=(
            "Judge the suite by whether it fails when the code is wrong, not "
            "by line coverage. Break the implementation deliberately and "
            "confirm a test catches it."
        ),
    ),
    TaskKind.COMPREHEND: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the ladder. Comprehension degrades from ~32K regardless of "
            "advertised window, so the chunking gate matters more than which "
            "model reads."
        ),
        gate=(
            "Read in bounded chunks and record findings as you go. Do not rely "
            "on recall across a very large single context."
        ),
    ),
    TaskKind.IAC: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the ladder. Models improved at generating infrastructure "
            "code without improving at securing it, so the deterministic gate "
            "is what carries this kind, not a pin."
        ),
        gate=(
            "Show the plan output and any policy-scan result before applying "
            "anything. Never apply from a model's assurance alone."
        ),
    ),
    TaskKind.DOCS: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Rides the ladder as ROTE, which lands it on the least-loaded "
            "window. The earlier Sonnet pin spent the same Anthropic window "
            "the orchestrator needs most."
        ),
    ),
    TaskKind.PERF: KindPolicy(
        confidence=Confidence.HIGH,
        evidence=(
            "Near-zero recall on performance defects across every model "
            "measured. The bottleneck is a measurement question and a profiler "
            "answers it directly, so routing this to a model is the mistake."
        ),
        tool_first="a profiler run against the real workload",
        gate=(
            "Bring a measurement before a change. State the baseline, the "
            "hypothesis, and the measured result after."
        ),
    ),
    TaskKind.DATA: KindPolicy(
        confidence=Confidence.LOW,
        evidence=(
            "Deliberately unrouted. The public leaderboards here are not usable "
            "-- one widely-cited text-to-SQL set was found to have a 62.8% "
            "annotation error rate -- so rotation plus the scoreboard is a "
            "better basis than any published ranking."
        ),
    ),

    TaskKind.GENERAL: KindPolicy(
        confidence=Confidence.LOW,
        evidence="Unclassified work rotates, which is also how the scoreboard fills.",
    ),
}


def policy_for(kind: str) -> KindPolicy:
    """The policy for ``kind``, falling back to GENERAL for anything unknown.

    Unknown kinds rotate rather than raise: a task the classifier could not
    label is a normal occurrence, and refusing to run it would be a worse
    answer than running it without a preference.
    """
    return ROUTING.get(kind, ROUTING[TaskKind.GENERAL])


def _available_always(_key: str) -> bool:
    return True


def route(
    kind: str,
    *,
    default: str,
    difficulty: Optional[str] = None,
    candidates: Optional[Sequence[str]] = None,
    available: Callable[[str], bool] = _available_always,
) -> str:
    """Pick the lead for a task of this ``kind`` at this ``difficulty``.

    Two axes, checked in order. A kind pin wins first -- those are the few
    exceptions with evidence or an operator directive behind them (security
    and testing to Sol, review to the pair, scope and decomposition to the
    orchestrator). Everything else routes by the difficulty ladder, which is
    what spreads the load across all four subscriptions. An unavailable or
    excluded rung escalates upward -- a stronger model can always do easier
    work -- before it degrades downward.

    Args:
        kind: A :class:`TaskKind` value.
        difficulty: A :data:`DIFFICULTY_LADDER` key. None skips the ladder.
        default: Whoever rotation already selected; the last resort.
        candidates: The pool to fall back into when ``default`` is excluded.
            Normally the brain trust.
        available: Liveness predicate. A pinned model that is down does not
            block the task; the policy degrades down the checks.

    Returns:
        The model key to lead with. Never raises -- an unroutable task is
        still a task, and stalling on a routing preference would be worse
        than running it with the rotation's choice.
    """
    policy = policy_for(kind)
    excluded = set(policy.exclude)

    for key in policy.prefer:
        if key not in excluded and available(key):
            return key

    if difficulty in DIFFICULTY_LADDER:
        start = _LADDER_ORDER.index(DIFFICULTY_LADDER[difficulty])
        # The rung itself, then upward: capability only increases.
        for key in _LADDER_ORDER[start:]:
            if key not in excluded and available(key):
                return key
        # Downward only when everything stronger is out too.
        for key in reversed(_LADDER_ORDER[:start]):
            if key not in excluded and available(key):
                return key

    if default not in excluded and available(default):
        return default

    for key in candidates or ():
        if key not in excluded and available(key):
            return key

    # Everything is excluded or unavailable. Hand back the rotation's pick
    # rather than inventing one: the caller can see the exclusion in the
    # policy, and a silent substitution would hide it.
    return default


def guidance_for(kind: str) -> List[str]:
    """Instructions the lead must be told for this kind of work.

    Returned as text to be pasted into the lead's prompt rather than enforced
    in code, because these are judgements about the work product that only the
    model doing the work is positioned to apply. What *is* enforced in code is
    the routing above and the size ceiling at decomposition.
    """
    policy = policy_for(kind)
    out: List[str] = []
    if policy.tool_first:
        out.append(
            f"Reach for {policy.tool_first} before reasoning about this. "
            "Measured evidence beats an inference here, and models specifically "
            "do badly at this kind of work."
        )
    if policy.gate:
        out.append(policy.gate)
    if policy.human_check:
        out.append(
            "Flag this for human review when you finish. The best measured "
            "agent performance on this kind of work is well short of a "
            "competent human's, so say what you are least sure about."
        )
    return out
