"""The model roster and capability-based role resolution.

Why a registry rather than a tier table
---------------------------------------
An earlier design addressed models as ``{provider: {high, low}}``. That stops
working once a fleet contains genuine role specialists -- a dedicated
reasoning model, a 2M-context model, a multi-agent variant -- because such a
model is not "high" or "low", it has a shape. Here each model declares a
capability profile, and roles request capabilities. Adding next month's model
is a new row; no role definition changes.

What is and is not trustworthy here
-----------------------------------
* ``context`` and ``latency`` come from vendor documentation. Anthropic's
  figures were verified first-hand against platform.claude.com; the OpenAI,
  Google and xAI figures are second-hand, because those vendors' domains were
  unreachable when this was compiled.
* ``alias`` is what the vendor CLI is expected to accept, and is the least
  reliable field in the table. Only the Claude aliases have been checked
  against a live binary. Run ``quadratus probe`` to replace these with what
  your CLIs actually resolve.
* Capability tags are deliberately coarse and describe *shape*, not a quality
  ranking. Where a tag encodes a measured claim, the source is named in
  ``notes``. Quality ordering within a capability is what the run scoreboard
  is for -- it should not be hardcoded from benchmarks, which are vendor-run,
  churn monthly, and in the review case do not exist at all.

A caution that keeps being relevant: a large ``context`` value is a claim
about capacity, not evidence of recall at depth. Independent testing found
every frontier model except Gemini 3 Deep Think losing 30-60 points of
retrieval quality between 200K and 1M. Treat LONG_CONTEXT as a shortlist to
measure, never as an assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence

__all__ = [
    "Capability",
    "ModelSpec",
    "ROSTER",
    "models_for",
    "best_for",
    "REFUSAL_CHAIN",
    "MODE_ROSTERS",
]


class Capability:
    """Capability tags. Shapes of work, not quality rankings."""

    #: Writing new code from a spec.
    CODE = "code"
    #: Finding unknown defects in someone else's code.
    REVIEW = "review"
    #: Root-causing a *known* defect. Distinct from REVIEW: the evidence says a
    #: model can lead at one and sit mid-pack at the other.
    DEBUG = "debug"
    #: Open-ended architectural judgement.
    REASON = "reason"
    #: Candidate for holding a large codebase. A shortlist, not a verdict.
    LONG_CONTEXT = "long-context"
    #: Cheap enough for control-plane work run on every round.
    CHEAP = "cheap"
    #: Carries no request-declining safety classifier.
    NO_CLASSIFIER = "no-classifier"
    #: Real-time web / social retrieval.
    RETRIEVAL = "retrieval"
    #: Sustained autonomous tool loops.
    AGENTIC = "agentic"
    #: Native multi-agent / step-by-step decomposition variants.
    MULTI_AGENT = "multi-agent"
    #: Fit to run the orchestrator: interview the operator, author the run
    #: policy, and hold the end goal across a long, messy debate. This is a
    #: different axis from per-call judgement quality -- it is about coherence
    #: over a whole session, which is why the strongest reviewer is not
    #: automatically the strongest orchestrator.
    ORCHESTRATE = "orchestrate"


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    alias: str
    label: str
    context: int
    latency: str  # fastest | fast | moderate | slow
    caps: FrozenSet[str]
    notes: str = ""
    #: True when the alias has been checked against a live CLI.
    verified: bool = False

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.alias}"


def _spec(provider, alias, label, context, latency, caps, notes="", verified=False):
    return ModelSpec(provider, alias, label, context, latency, frozenset(caps), notes, verified)


K = 1024

ROSTER: List[ModelSpec] = [
    # -- Anthropic. Specs verified against platform.claude.com; aliases
    #    verified against claude 2.1.228 (opus/sonnet/haiku resolved live).
    _spec(
        "claude", "fable", "Claude Fable 5.1", 1000 * K, "slow",
        [Capability.REASON, Capability.CODE, Capability.AGENTIC,
         Capability.ORCHESTRATE],
        "The CLI alias resolves to the current Fable, 5.1 since 2026-09-01. "
        "Three API rules arrived with 5.1 that any future direct call must "
        "respect: forced tool_choice (any/tool) is a 400; thinking blocks are "
        "bound to the producing model; and editing earlier turns invalidates "
        "every later thinking block. The harness is single-turn and replays "
        "no thinking blocks, so none of them bite today. "
        "Anthropic's most capable widely-released model, positioned by the "
        "vendor for long-running agents -- which is the orchestrator's job "
        "description, and why it leads ORCHESTRATOR_CHAIN. Its 'Slower' "
        "latency rating does not bite there: the orchestrator interviews at "
        "human pace, authors policy once, and reviews at checkpoints already "
        "gated behind a multi-minute debater run. Three real hazards: it "
        "ships the strictest safety classifiers, and Anthropic's own refusal "
        "docs say benign cybersecurity work can trigger the cyber category; "
        "it costs 2x Opus 5 against the same subscription window, so it is "
        "the model most likely to exhaust first; and it was withdrawn for 19 "
        "days in June 2026 under export controls. All three are why the "
        "orchestrator role needs a fallback rather than a single assignment. "
        "Prompt it less prescriptively than prior models -- Anthropic reports "
        "over-specified prompts reduce its output quality.",
    ),
    _spec(
        "claude", "opus", "Claude Opus 5", 1000 * K, "moderate",
        [Capability.CODE, Capability.REVIEW, Capability.DEBUG,
         Capability.REASON, Capability.AGENTIC, Capability.ORCHESTRATE],
        "Freshest knowledge cutoff in the fleet (May 2026). Review is "
        "precision-leaning: ~39% precision / ~55% recall with 4x the nitpicks "
        "(CodeRabbit). Reportedly leads at debugging a known defect. Cyber "
        "classifier fires ~85% less than Fable's. Follows review instructions "
        "literally -- a 'only report high-severity' prompt suppresses its own "
        "recall, so collect everything and filter downstream. Do not add "
        "'double-check your work': it over-verifies unprompted.",
        verified=True,
    ),
    _spec(
        "claude", "sonnet", "Claude Sonnet 5", 1000 * K, "fast",
        [Capability.CODE, Capability.CHEAP, Capability.NO_CLASSIFIER],
        "No safety classifier layer at all, which makes it the safe default "
        "for security-adjacent work that trips Fable. Interprets instructions "
        "literally, especially at low effort.",
        verified=True,
    ),
    _spec(
        "claude", "haiku", "Claude Haiku 4.5", 200 * K, "fastest",
        [Capability.CHEAP, Capability.NO_CLASSIFIER],
        "The fleet's odd one out: an Oct 2025 model with a 200K window and "
        "Feb 2025 knowledge, no adaptive thinking. Verified working for "
        "control-plane checklist work; unsuited to anything needing current "
        "library knowledge or large context.",
        verified=True,
    ),

    # -- OpenAI. Second-hand specs; aliases unverified.
    _spec(
        "openai", "gpt-5.6-sol", "GPT-5.6 Sol", 1100 * K, "moderate",
        [Capability.CODE, Capability.REVIEW, Capability.REASON,
         Capability.LONG_CONTEXT, Capability.AGENTIC],
        "Review is recall-leaning: ~70% of known issues caught but only ~32% "
        "of comments worth keeping (CodeRabbit) -- the mirror image of Opus 5, "
        "which is why the two are paired rather than chosen between. Strongest "
        "published long-context retrieval figure in the fleet, but measured on "
        "OpenAI's own benchmark. System-card coverage reports over-agency "
        "(deleting infrastructure, fabricating results) and presenting "
        "unverified work as complete in a way CoT monitoring misses: never "
        "grant it writes outside a sandbox.",
    ),
    _spec(
        "openai", "gpt-5.6-terra", "GPT-5.6 Terra", 1100 * K, "moderate",
        [Capability.CODE, Capability.AGENTIC],
        "The everyday mid-tier. Sits ~1 point behind Sol on SWE-bench Pro.",
    ),
    _spec(
        "openai", "gpt-5.6-luna", "GPT-5.6 Luna", 1100 * K, "fast",
        [Capability.CODE, Capability.CHEAP],
        "The tier spread across 5.6 is small on coding (~1.9 pts SWE-bench Pro "
        "Sol->Luna) for roughly 5x the price, so Luna is a defensible default "
        "for routine coding, not merely a cheap fallback.",
    ),

    # -- Google. Second-hand specs; aliases unverified.
    _spec(
        "gemini", "gemini-3.6-flash", "Gemini 3.6 Flash", 1000 * K, "fast",
        [Capability.CODE, Capability.CHEAP],
        "GA 2026-07-21. Near-Pro intelligence at Flash cost; the current "
        "standard workhorse.",
    ),
    _spec(
        "gemini", "gemini-3.6-thinking", "Gemini 3.6 Thinking", 1000 * K, "slow",
        [Capability.REASON],
        "Dedicated reasoning model using extended thinking. Supplied by the "
        "operator; not independently confirmed here, and some sources describe "
        "thinking as a mode of 3.6 Flash rather than a separate model. The "
        "probe will settle whether this alias resolves.",
    ),
    _spec(
        "gemini", "gemini-3.1-pro-preview", "Gemini 3.1 Pro", 1000 * K, "moderate",
        [Capability.CODE, Capability.REASON, Capability.LONG_CONTEXT],
        "Still the current Pro; no 3.5 or 3.6 Pro shipped. Maintained for "
        "advanced math, code and large-context work.",
    ),

    # -- xAI. Second-hand specs; aliases unverified.
    _spec(
        "grok", "grok-4.6", "Grok 4.6", 500 * K, "moderate",
        [Capability.CODE, Capability.AGENTIC, Capability.RETRIEVAL],
        "Released 2026-08-12, superseding 4.5. Note its retrieval advantage is "
        "reported to collapse ~15x with web search disabled, implying the value "
        "sits in the retrieval pipeline rather than the weights -- which is why "
        "RETRIEVAL is also exposed as a consultant function.",
    ),
    _spec(
        "grok", "grok-4-1-fast", "Grok 4.1 Fast", 2000 * K, "fastest",
        [Capability.CHEAP, Capability.RETRIEVAL, Capability.AGENTIC],
        "The worker tier's scout: xAI's best tool-calling model, tuned for "
        "live web lookup, at the lowest prices in the fleet. Mid-pack at "
        "reasoning and code -- not what it is hired for. Its advertised 2M "
        "window follows the same rule as Grok 4.20's: unverified, so no "
        "LONG_CONTEXT tag until it passes the probe.",
    ),
    _spec(
        "grok", "grok-4.3", "Grok 4.3", 1000 * K, "moderate",
        [Capability.LONG_CONTEXT, Capability.REASON],
        "Kept in the roster for its 1M window against 4.6's 500K.",
    ),
    _spec(
        "grok", "grok-4.20", "Grok 4.20", 2000 * K, "moderate",
        [Capability.MULTI_AGENT],
        "Advertises the largest window in the fleet at 2M, and deliberately "
        "does not carry LONG_CONTEXT. The figure is vendor marketing with no "
        "independent corroboration: the model is absent from the retrieval "
        "benchmarks that would test it, and the same vendor's own flagship "
        "shipped with a 500K window, which is hard to reconcile with a 2M "
        "capability sitting elsewhere in the lineup. Carrying the tag would "
        "make it the default pick for exactly the work least able to survive "
        "the claim being wrong. Restore the tag if it passes the long-context "
        "probe at depth, and not before.",
    ),
]

_BY_KEY: Dict[str, ModelSpec] = {m.key: m for m in ROSTER}


def models_for(
    capability: str,
    *,
    providers: Optional[Sequence[str]] = None,
    min_context: int = 0,
) -> List[ModelSpec]:
    """Every roster model offering ``capability``, optionally filtered.

    Ordering is roster order, which is deliberately not a quality ranking:
    callers that want the strongest candidate should consult scoreboard data
    rather than trusting position here.
    """
    out = [m for m in ROSTER if capability in m.caps and m.context >= min_context]
    if providers is not None:
        allowed = set(providers)
        out = [m for m in out if m.provider in allowed]
    return out


def best_for(capability: str, **kwargs) -> Optional[ModelSpec]:
    """First roster model offering ``capability``, or None."""
    found = models_for(capability, **kwargs)
    return found[0] if found else None


#: Ordered fallback when a model declines work the operator believes is
#: legitimate. Ordered by decreasing classifier aggressiveness, which is the
#: only principled ordering available: Fable ships the strictest classifiers,
#: Opus 5's fire far less often, and Sonnet 5 and Haiku 4.5 carry none.
#: Cross-vendor targets come last because their refusal behaviour on security
#: work is undocumented -- an unknown, not a clean bill of health.
#:
#: Exploit generation and penetration testing are gated regardless of routing
#: and need Anthropic's Cyber Verification Program; rerouting will not open
#: them, and this chain is not a mechanism for evading a correct refusal.
REFUSAL_CHAIN: List[str] = [
    "claude:opus",
    "claude:sonnet",
    "openai:gpt-5.6-sol",
    "grok:grok-4.6",
]


#: Which models participate per mode. Kept explicit because an
#: all-models-participate default across a 13-model roster is ruinous: every
#: added peer costs another review pass and fattens every synthesis prompt.
MODE_ROSTERS: Dict[str, Dict[str, List[str]]] = {
    "adversarial": {
        # The brain trust: one model per vendor, which is the point. Four
        # independent reads is the whole reason this system exists rather than
        # a single strong model in a loop, and dropping to three would have
        # meant the cheapest voice to cut was also the only one from its
        # vendor. Grok 4.6 earns its seat on independence and on turn
        # efficiency -- roughly 53 turns and 0.5B tokens against ~103 and 2.0B
        # for the strongest coder on comparable work -- not on peak quality,
        # where it is the weakest of the four.
        "peers": ["claude:opus", "openai:gpt-5.6-sol",
                  "gemini:gemini-3.1-pro-preview", "grok:grok-4.6"],
        # A subset, not a shortfall. Review is the one role with measured
        # data, and it pins the two models that fail in opposite directions:
        # ~70% recall / ~32% precision against ~39% precision / ~55% recall.
        # Adding a third reviewer with no measured profile would cost an
        # invocation to dilute a pairing chosen precisely for its shape.
        "reviewers": ["openai:gpt-5.6-sol", "claude:opus"],
        # Planning runs once per run, so it is the cheapest place to be
        # inclusive. It happens to equal the brain trust today; that is a
        # coincidence of the roster size, not a constraint worth enforcing.
        "planners": ["claude:opus", "openai:gpt-5.6-sol",
                     "gemini:gemini-3.1-pro-preview", "grok:grok-4.6"],
    },
    "collaborative": {
        "peers": ["claude:opus", "openai:gpt-5.6-terra"],
        "reviewers": ["claude:opus"],
        "planners": ["claude:opus", "openai:gpt-5.6-terra"],
    },
    "solo": {
        "peers": ["claude:opus"],
        "reviewers": [],
        "planners": ["claude:opus"],
    },
}

#: Ordered preference for the orchestrator seat.
#:
#: Fable 5 leads because the job is holding a goal coherent across a long
#: session, which is what the vendor built it for -- a different axis from
#: per-call judgement, where Opus 5 is stronger. Opus 5 backs it up because
#: the orchestrator is a single point of coordination and Fable is, in
#: practice, the model most likely to become unavailable: it costs twice as
#: much against the same subscription window, and it has already been
#: withdrawn once for 19 days. A stalled orchestrator stalls everything.
#:
#: Fall back on any of: the subscription window for Fable being exhausted,
#: the model being unavailable, or the project being classified
#: security-adjacent -- Anthropic documents that benign cybersecurity work
#: can trip Fable's classifiers, and an orchestrator that declines to discuss
#: the project it is supervising is worse than a slightly weaker one.
ORCHESTRATOR_CHAIN: List[str] = [
    "claude:fable",
    "claude:opus",
]

def peers_for(mode: str, orchestrator: str) -> List[str]:
    """Brain-trust peers for ``mode``.

    The brain trust is fixed. An earlier design recused whoever held the
    orchestrator seat and promoted a substitute off a bench, on the reasoning
    that a supervisor should not compete in the debate it supervises. That was
    the wrong trade twice over: it cost the brain trust its second-strongest
    member exactly when the primary orchestrator was already unavailable, and
    the only available substitutes were a tier below the seats they filled.
    A weaker brain trust is a worse failure than a supervisor with a stake.

    The conflict it was solving is handled where it actually arises instead.
    Security work is peeled into a bounded excursion (see
    :mod:`quadratus.routing`) rather than displacing anyone, and the one place
    self-preference would concretely change the artifact -- choosing what
    survives into the final answer -- is closed off by
    :func:`synthesizer_for`.
    """
    return list(MODE_ROSTERS[mode]["peers"])


def synthesizer_for(mode: str, orchestrator: str) -> Optional[str]:
    """Which peer merges the debate into the final artifact.

    Synthesis is the one step where a model holding two roles could quietly
    rewrite the outcome in its own favour: it decides what survives. So a
    peer that is also holding the orchestrator seat is passed over here, even
    though it keeps its seat in the debate itself.

    Returns None when the mode has no other peer to fall back on, which is the
    caller's signal that double-hatting is unavoidable and should be surfaced
    rather than hidden.
    """
    candidates = [p for p in MODE_ROSTERS[mode]["peers"] if p != orchestrator]
    return candidates[0] if candidates else None


#: Control-plane roles. Cheap by task shape, not by importance: convergence
#: judging is load-bearing and still checklist work. Haiku was verified
#: returning a correct, well-calibrated structured verdict on a seeded defect.
CONTROL_PLANE: Dict[str, str] = {
    "interview": "claude:sonnet",
    "spec_extraction": "claude:haiku",
    "ledger_extraction": "claude:haiku",
    "convergence": "claude:haiku",
}


def resolve(key: str) -> Optional[ModelSpec]:
    """Look up a roster entry by ``provider:alias``."""
    return _BY_KEY.get(key)
