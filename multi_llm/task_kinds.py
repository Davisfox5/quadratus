"""Which kind of work goes to which model, and how sure we are.

This is separate from :mod:`multi_llm.registry` on purpose. The registry says
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
GEMINI_PRO = "gemini:gemini-3.1-pro"
GROK = "grok:grok-4.6"
FABLE = "claude:fable"


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
        prefer=(OPUS,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "Terminal-Bench 3.0 43.5 vs 34.6 for the nearest rival. A single "
            "agentic benchmark, so medium rather than high."
        ),
    ),
    TaskKind.FRONTEND: KindPolicy(
        prefer=(OPUS,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "Freshest knowledge cutoff in the fleet (May 2026), which matters "
            "more here than elsewhere because framework APIs churn fast. But "
            "the harness dominates: a screenshot-verification loop changes "
            "outcomes more than the model does."
        ),
        tool_first=(
            "a rendered-page check (multi_llm.browser.render_page: screenshot, "
            "console errors, failed requests from a real headless browser)"
        ),
        gate=(
            "Verify the rendered result visually before claiming it works; a "
            "frontend change that compiles is not a frontend change that works. "
            "Late console errors count: frameworks routinely throw after load."
        ),
    ),
    TaskKind.MOBILE: KindPolicy(
        prefer=(OPUS,),
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
        prefer=(GROK,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "Roughly 53 turns and 0.5B tokens against ~103 turns and 2.0B for "
            "the strongest coder on comparable work -- a ~4x efficiency gap "
            "that only pays off where per-task judgement is not the constraint."
        ),
    ),
    TaskKind.GLUE: KindPolicy(
        prefer=(LUNA,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "The 5.6 tiers differ by ~1.9 points on SWE-bench Pro for roughly "
            "5x the cost, so the cheap tier is a defensible default for work "
            "with little judgement in it, not merely a fallback."
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
        prefer=(OPUS,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "Vendor-stated strength at root-causing a known defect. A different "
            "job from REVIEW, and the same model does not lead at both -- which "
            "is why the two are separate kinds."
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
        prefer=(OPUS,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "Best measured agent performance sits at 22-41% against ~87% for a "
            "competent human. The preference is weak and the gap is the point."
        ),
        human_check=True,
        gate=(
            "A refactor must not change behaviour. Show the existing tests "
            "passing unchanged, and say plainly if none cover the touched code."
        ),
    ),
    TaskKind.TEST: KindPolicy(
        prefer=(SOL,),
        confidence=Confidence.LOW,
        evidence=(
            "Recall-leaning reviewer, which is the right bias for enumerating "
            "cases. But generated suites average ~40% mutation score, so the "
            "coverage number they produce overstates what they actually check."
        ),
        gate=(
            "Judge the suite by whether it fails when the code is wrong, not "
            "by line coverage. Break the implementation deliberately and "
            "confirm a test catches it."
        ),
    ),
    TaskKind.COMPREHEND: KindPolicy(
        prefer=(OPUS,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "Comprehension degrades from around 32K regardless of the "
            "advertised window, so the chunking matters more than the window "
            "size. Feed it in pieces rather than trusting a 1M context claim."
        ),
        gate=(
            "Read in bounded chunks and record findings as you go. Do not rely "
            "on recall across a very large single context."
        ),
    ),
    TaskKind.IAC: KindPolicy(
        prefer=(SOL,),
        confidence=Confidence.MEDIUM,
        evidence=(
            "Models improved at generating infrastructure code without "
            "improving at securing it, so the generated artifact needs a "
            "deterministic check rather than a second opinion."
        ),
        gate=(
            "Show the plan output and any policy-scan result before applying "
            "anything. Never apply from a model's assurance alone."
        ),
    ),
    TaskKind.DOCS: KindPolicy(
        prefer=(SONNET,),
        confidence=Confidence.LOW,
        evidence=(
            "Cheap, fast, carries no classifier, and adequate. No comparative "
            "evidence -- this is a cost decision, not a quality one."
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
    candidates: Optional[Sequence[str]] = None,
    available: Callable[[str], bool] = _available_always,
) -> str:
    """Pick the lead for a task of this ``kind``.

    Args:
        kind: A :class:`TaskKind` value.
        default: Whoever rotation already selected. Used when the policy
            expresses no preference, which is the common case by design.
        candidates: The pool to fall back into when ``default`` is excluded.
            Normally the brain trust.
        available: Liveness predicate. A pinned model that is down does not
            block the task; the policy degrades to rotation.

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
