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

Three vendors, not four
-----------------------
The lineup is Anthropic, OpenAI and xAI (:data:`VENDORS`). Google left on
2026-09-12 -- an operator decision about which subscriptions are being paid
for, not a judgement about the models -- so its rows moved to
:data:`RETIRED_ROSTER`, where their notes survive without any lookup being
able to return a model nothing can invoke. Restoring Google is moving those
rows back and re-adding a rung to the difficulty ladder; nothing else in the
system names a vendor count.

Pinned aliases and floating ones
--------------------------------
Two kinds of ``alias`` live here and the difference is load-bearing:

* **Floating** (``floating=True``) names a model *line* -- ``opus``,
  ``fable`` -- and the vendor CLI resolves it to whatever the current release
  of that line is. Its ``label`` therefore carries no version number, because
  the number would be a claim about today that nobody would come back and
  correct.
* **Pinned** names one iteration, ``grok-4.6``, and stops being right the
  day 4.7 ships.

Every model in :data:`ORCHESTRATOR_CHAIN` must be floating, checked at import
by :func:`assert_floating_orchestrators`. The orchestrator is the one seat
that persists for a whole session and the one nobody re-picks mid-run; a
pinned alias there is how a run quietly keeps using last quarter's model
months after its successor shipped. :mod:`quadratus.latest` does the actual
resolution -- an operator override, then what ``quadratus probe`` last saw a
CLI accept, then the seed below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

__all__ = [
    "Capability",
    "ModelSpec",
    "ROSTER",
    "RETIRED_ROSTER",
    "VENDORS",
    "models_for",
    "best_for",
    "REFUSAL_CHAIN",
    "MODE_ROSTERS",
    "ORCHESTRATOR_CHAIN",
    "CONTROL_PLANE",
    "ControlPlaneRole",
    "control_plane_model",
    "assert_floating_orchestrators",
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
    #: True when ``alias`` names a model line the vendor CLI resolves to its
    #: current release, rather than one frozen iteration. Required of every
    #: orchestrator candidate; see the module docstring.
    floating: bool = False
    #: Further aliases to try, newest first, when the first does not resolve.
    #: Only meaningful for floating entries whose vendor may not accept a bare
    #: line name -- ``quadratus probe`` walks this chain and caches the winner.
    alias_fallbacks: Tuple[str, ...] = ()
    #: True when the right way to address this model is to name no model at
    #: all and take whatever the CLI currently defaults to. The strongest form
    #: of floating there is: the vendor moves its own default and the run
    #: follows with no table to edit, no alias to guess, and nothing to probe.
    #: Available only where a CLI *has* a default, which is why it is a flag
    #: rather than the rule. ``alias`` is then an identity for the ledger and
    #: the routing tables, never a string sent on the wire.
    vendor_default: bool = False
    #: Reasoning depth this seat asks for, where the vendor exposes a dial.
    #: Empty means the CLI's own default.
    effort: str = ""
    #: Whether the seat gets a bounded, read-only call rather than a full
    #: agent loop. The same weights serve both; on a subscription the
    #: difference between them is most of what a run spends.
    restricted: bool = False

    @property
    def key(self) -> str:
        """Stable identity, independent of which release the alias resolves to.

        Deliberately built from ``alias`` rather than from a version: the key
        appears in the ledger, the price sheet and the routing tables, and a
        key that moved every time a vendor shipped a point release would break
        every one of them for no gain.
        """
        return f"{self.provider}:{self.alias}"

    @property
    def alias_chain(self) -> Tuple[str, ...]:
        """Aliases to try against the CLI, newest first.

        Empty for a vendor-default row: there is no alias to try, because the
        point is to send none.
        """
        if self.vendor_default:
            return ()
        return (self.alias, *self.alias_fallbacks)


def _spec(provider, alias, label, context, latency, caps, notes="", verified=False,
          floating=False, alias_fallbacks=(), vendor_default=False, effort="",
          restricted=False):
    return ModelSpec(
        provider, alias, label, context, latency, frozenset(caps), notes, verified,
        floating, tuple(alias_fallbacks), vendor_default, effort, restricted,
    )


#: The vendors in today's lineup. One subscription each: Claude Max, ChatGPT
#: Pro, SuperGrok. Google was dropped on 2026-09-12 -- see the module
#: docstring and :data:`RETIRED_ROSTER`.
VENDORS: Tuple[str, ...] = ("claude", "openai", "grok")


K = 1024

ROSTER: List[ModelSpec] = [
    # -- Anthropic. Specs verified against platform.claude.com; aliases
    #    verified against claude 2.1.228 (opus/sonnet/haiku resolved live).
    _spec(
        "claude", "fable", "Claude Fable", 1000 * K, "slow",
        [Capability.REASON, Capability.CODE, Capability.AGENTIC,
         Capability.ORCHESTRATE],
        "The CLI alias resolves to the current Fable, 5.1 since 2026-09-01, "
        "and that is exactly why the seat addresses it by line rather than by "
        "release: the orchestrator is never re-picked mid-run, so a pinned "
        "alias here would outlive its successor by months. "
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
        floating=True,
    ),
    _spec(
        "claude", "opus", "Claude Opus", 1000 * K, "moderate",
        [Capability.CODE, Capability.REVIEW, Capability.DEBUG,
         Capability.REASON, Capability.AGENTIC, Capability.ORCHESTRATE],
        "Everything measured below was measured on Opus 5; the alias floats, "
        "so re-read these figures as claims about the line, not guarantees "
        "about whatever it resolves to next. Carries ORCHESTRATE because it "
        "is capable of the job, and is deliberately absent from "
        "ORCHESTRATOR_CHAIN anyway: it is a brain-trust peer and half the "
        "pinned reviewer pair, and the seat is worth less to the run than "
        "those two roles are. Do not add it back as a third link without "
        "reading that chain's note. "
        "Freshest knowledge cutoff in the fleet (May 2026). Review is "
        "precision-leaning: ~39% precision / ~55% recall with 4x the nitpicks "
        "(CodeRabbit). Reportedly leads at debugging a known defect. Cyber "
        "classifier fires ~85% less than Fable's. Follows review instructions "
        "literally -- a 'only report high-severity' prompt suppresses its own "
        "recall, so collect everything and filter downstream. Do not add "
        "'double-check your work': it over-verifies unprompted.",
        verified=True,
        floating=True,
    ),
    _spec(
        "claude", "sonnet", "Claude Sonnet", 1000 * K, "fast",
        [Capability.CODE, Capability.CHEAP, Capability.NO_CLASSIFIER],
        "No safety classifier layer at all, which makes it the safe default "
        "for security-adjacent work that trips Fable. Interprets instructions "
        "literally, especially at low effort. Measured on Sonnet 5.",
        verified=True,
        floating=True,
        # Bounded worker call, not an agent loop: an escalation target and the supervised interviewer, never a base worker.
        effort="high",
        restricted=True,
    ),
    _spec(
        "claude", "haiku", "Claude Haiku", 200 * K, "fastest",
        [Capability.CHEAP, Capability.NO_CLASSIFIER],
        "The fleet's odd one out: on Haiku 4.5 today, an Oct 2025 model with "
        "a 200K window and Feb 2025 knowledge, no adaptive thinking. Verified "
        "working for control-plane checklist work; unsuited to anything "
        "needing current library knowledge or large context. The context "
        "figure is the one most likely to be wrong after the line moves -- "
        "re-probe it rather than trusting it.",
        verified=True,
        floating=True,
        # Bounded worker call, not an agent loop: the worker tier's base for visual, check and code errands, and the whole control plane.
        effort="low",
        restricted=True,
    ),

    # -- OpenAI. Second-hand specs; aliases unverified.
    _spec(
        "openai", "gpt-6-astra", "GPT-6 Astra", 1100 * K, "slow",
        [Capability.REASON, Capability.CODE, Capability.AGENTIC,
         Capability.ORCHESTRATE],
        "The orchestrator's only fallback, by operator directive, and the "
        "only model other than Fable that ever holds the seat -- including "
        "for a security segment. It is not the seat's understudy on capability "
        "grounds: Opus 5 would be the stronger deputy per call. It is the "
        "deputy because it is the one that is there when the Anthropic side is "
        "not, and because it holds no other role -- seating a brain-trust peer "
        "would make the supervisor a competitor in the debate it supervises "
        "and put the fleet's strongest reviewer on bookkeeping. When both "
        "seats are gone the run stops rather than finding a third. "
        "Deliberately carries no LONG_CONTEXT tag despite the advertised 1.1M "
        "window: the same rule Grok 4.20 is held to, and this row has less "
        "corroboration than that one -- no codex binary was available to "
        "check even the alias. Run `quadratus --probe` before a run depends "
        "on this seat: a fallback that has never been exercised is a guess.",
        floating=True,
        alias_fallbacks=("gpt-6",),
    ),
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
        # Bounded worker call, not an agent loop: an escalation target only.
        effort="high",
        restricted=True,
    ),
    _spec(
        "openai", "gpt-5.6-luna", "GPT-5.6 Luna", 1100 * K, "fast",
        [Capability.CODE, Capability.CHEAP],
        "The tier spread across 5.6 is small on coding (~1.9 pts SWE-bench Pro "
        "Sol->Luna) for roughly 5x the price, so Luna is a defensible default "
        "for routine coding, not merely a cheap fallback.",
        # Bounded worker call, not an agent loop: the worker tier's base for read, format and draft errands.
        effort="low",
        restricted=True,
    ),

    # -- xAI. Second-hand specs; aliases unverified.
    _spec(
        "grok", "default", "Grok", 500 * K, "moderate",
        [Capability.CODE, Capability.AGENTIC, Capability.RETRIEVAL],
        "xAI's current flagship, addressed by sending no model flag at all so "
        "the CLI's own default applies. Operator directive, 2026-09-12: this "
        "seat is not tied to an iteration. `grok models` on Grok Build 1.0.30 "
        "reports 'grok-4.6 (default)' with 4.5 also available, and no alias "
        "that names the line -- so taking the default is the only way to "
        "follow it forward without editing a table, and it is a better way "
        "than an alias would be: xAI moves the pointer, not us. "
        "The 500K window and the notes below describe 4.6, which is what the "
        "default resolves to today; re-read them as claims about the line. "
        "Its retrieval advantage is reported to collapse ~15x with web search "
        "disabled, implying the value sits in the retrieval pipeline rather "
        "than the weights -- which is why RETRIEVAL is also exposed as a "
        "consultant function.",
        vendor_default=True,
        # The brain-trust seat is the one place the agent loop earns its
        # keep: a peer that can read the tree forms an opinion worth having.
        effort="high",
    ),
    _spec(
        "grok", "worker", "Grok (worker)", 500 * K, "fast",
        [Capability.CODE, Capability.CHEAP, Capability.RETRIEVAL],
        "The same model line as the seat above, called a different way. The "
        "CLI is a coding agent: left to itself it explores the tree, writes "
        "files, spawns subagents and re-sends the whole conversation each "
        "turn, which cost 120K tokens for a one-line function while the "
        "orchestrator supervising it spent 22K. A worker does not need any of "
        "that. This row is ``restricted``, so the write tools are absent "
        "rather than denied and the model answers inline, at ``low`` "
        "reasoning effort: measured at 3 turns and ~6K fresh tokens for the "
        "same task. Nothing here names a release, so the worker follows "
        "xAI's default forward exactly as the flagship seat does.",
        # Same reason as the flagship seat: no model flag is sent at all, so
        # the worker rides xAI's own pointer. The key names the seat, not a
        # release -- there is no "worker" model, there is a worker call.
        vendor_default=True,
        effort="low",
        restricted=True,
    ),
    _spec(
        "grok", "expert", "Grok (expert)", 500 * K, "moderate",
        [Capability.CODE, Capability.REASON, Capability.RETRIEVAL],
        "The escalation target above the worker: same model line, same "
        "bounded read-only call, ``high`` reasoning effort instead of low. "
        "Escalating by effort rather than by model is what xAI's lineup "
        "actually supports -- there is no second model to bump to on a "
        "consumer subscription -- and it is the honest version of what the "
        "old Grok Fast -> Grok 4.20 bump was pretending to do with two IDs "
        "that this transport rejects.",
        vendor_default=True,
        effort="high",
        restricted=True,
    ),
]

#: Rows kept for their notes, not for use. Nothing resolves to them, nothing
#: routes to them, and no lookup returns them: a roster entry the harness
#: cannot invoke is worse than a missing one, because it will be picked.
#:
#: Two different things land here.
#:
#: **A vendor left.** Google left the lineup on 2026-09-12: a decision about
#: which subscriptions are being paid for, not a finding about the models.
#: The notes were true when written and are what a future operator would
#: otherwise have to re-derive. Restoring Google means moving those rows back
#: into ROSTER, adding "gemini" to VENDORS, and giving the difficulty ladder
#: back its fourth rung (see :mod:`quadratus.task_kinds`).
#:
#: **A model exists, but not on this transport.** Grok 4.1 Fast, 4.3 and 4.20
#: are real xAI models with real API pricing, and all three were in ROSTER
#: until a probe on 2026-09-12 had the Grok Build CLI reject each one with
#: "unknown model id". A consumer subscription reaches a narrower catalogue
#: than the billed API does -- grok-4.6 and grok-4.5, nothing else -- and this
#: repo drives the subscription. That is the distinction the original rows
#: missed, and it is worth stating once: a vendor's published lineup is not a
#: statement about what your transport can invoke. Restoring any of these
#: means either moving that vendor to the "api" backend or a future release
#: widening what the CLI accepts; re-run `quadratus --probe` before believing
#: either. Their notes stay because the reasoning is still cited live -- the
#: Astra row holds itself to "the same rule Grok 4.20 is held to", and that
#: rule is written below.
RETIRED_ROSTER: List[ModelSpec] = [
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

    # -- xAI models the billed API serves and the subscription CLI does not.
    _spec(
        "grok", "grok-4-1-fast", "Grok 4.1 Fast", 2000 * K, "fastest",
        [Capability.CHEAP, Capability.RETRIEVAL, Capability.AGENTIC],
        "Was the worker tier's scout and the ROTE rung: xAI's best "
        "tool-calling model, tuned for live web lookup, at the lowest prices "
        "in the fleet. Mid-pack at reasoning and code -- not what it was "
        "hired for. Rejected by the Grok Build CLI as an unknown model id on "
        "2026-09-12; the lookup errand and the ROTE rung moved to Grok 4.5, "
        "which is neither cheap nor fast and is simply what the transport "
        "will accept. Its advertised 2M window followed the same rule as "
        "Grok 4.20's: unverified, so no LONG_CONTEXT tag.",
    ),
    _spec(
        "grok", "grok-4.3", "Grok 4.3", 1000 * K, "moderate",
        [Capability.LONG_CONTEXT, Capability.REASON],
        "Was kept in the roster for its 1M window against 4.6's 500K, and "
        "deliberately never made a worker. Rejected by the CLI 2026-09-12.",
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
        "probe at depth, and not before. This row is the one the rule is "
        "written on, and the Astra row cites it; rejected by the CLI "
        "2026-09-12 but kept for that reasoning.",
    ),
]

_BY_KEY: Dict[str, ModelSpec] = {m.key: m for m in ROSTER}

_unknown_vendors = {m.provider for m in ROSTER} - set(VENDORS)
if _unknown_vendors:  # pragma: no cover - a table edit, not a runtime path
    raise ValueError(
        f"roster carries models from vendors not in the lineup: "
        f"{sorted(_unknown_vendors)}. Add the vendor to VENDORS (and a "
        f"subscription to pay for it) or move the row to RETIRED_ROSTER."
    )


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
#: legitimate. Opus 5 leads: its classifiers fire far less often than Fable's
#: and it is a model this fleet already trusts with the work. Cross-vendor
#: targets come after it, because their refusal behaviour on security work is
#: undocumented -- an unknown, not a clean bill of health.
#:
#: Sonnet sat second until 2026-09-12, ordered there by decreasing classifier
#: aggressiveness -- it carries none at all, so it cannot false-positive.
#: Removed by operator directive, for the same reason it left
#: SECURITY_WORK_CHAIN: a model that will not refuse is not thereby a model to
#: hand the work to, and re-routing a declined request is the moment that
#: distinction matters most. The ordering principle is now trust first,
#: classifier behaviour second.
#:
#: Exploit generation and penetration testing are gated regardless of routing
#: and need Anthropic's Cyber Verification Program; rerouting will not open
#: them, and this chain is not a mechanism for evading a correct refusal.
REFUSAL_CHAIN: List[str] = [
    "claude:opus",
    "openai:gpt-5.6-sol",
    "grok:default",
]


#: Which models participate per mode. Kept explicit because an
#: all-models-participate default across a twelve-model roster is ruinous:
#: every added peer costs another review pass and fattens every synthesis
#: prompt.
MODE_ROSTERS: Dict[str, Dict[str, List[str]]] = {
    "adversarial": {
        # The brain trust: one model per vendor, which is the point.
        # Independent reads are the whole reason this system exists rather
        # than a single strong model in a loop, so the rule is one seat per
        # subscription and the size of the trust follows from how many
        # vendors are in the lineup -- three, since Google left. Nobody was
        # displaced to get here: the seat went with the vendor. Grok 4.6
        # earns its seat on independence and on turn efficiency -- roughly 53
        # turns and 0.5B tokens against ~103 and 2.0B for the strongest coder
        # on comparable work -- not on peak quality, where it is the weakest
        # of the three.
        "peers": ["claude:opus", "openai:gpt-5.6-sol", "grok:default"],
        # A subset, not a shortfall. Review is the one role with measured
        # data, and it pins the two models that fail in opposite directions:
        # ~70% recall / ~32% precision against ~39% precision / ~55% recall.
        # Adding a third reviewer with no measured profile would cost an
        # invocation to dilute a pairing chosen precisely for its shape.
        "reviewers": ["openai:gpt-5.6-sol", "claude:opus"],
        # Planning runs once per run, so it is the cheapest place to be
        # inclusive. It happens to equal the brain trust today; that is a
        # coincidence of the roster size, not a constraint worth enforcing.
        "planners": ["claude:opus", "openai:gpt-5.6-sol", "grok:default"],
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
#: Who may hold the orchestrator seat, in order. Exactly two models, and
#: adding a third is a decision, not a tidy-up.
#:
#: **Fable primary, Astra the only fallback. Operator directive.** The
#: supporting reasons, so a later reader can judge whether they still hold:
#:
#: * A fallback exists for the case the primary cannot serve, and the failures
#:   worth insuring against are the ones a same-vendor deputy shares -- the
#:   Anthropic side unreachable as a whole, a CLI signed out, a vendor
#:   outage, a classifier that will not discuss the project. Per-model limits
#:   mean an Anthropic deputy covers the *common* failure, but covering it
#:   costs something the cross-vendor seat does not.
#: * That cost is double-hatting. Opus 5 is a brain-trust peer and half the
#:   pinned reviewer pair; seating it makes the supervisor a competitor in
#:   the debate it supervises and puts the fleet's strongest reviewer on
#:   bookkeeping. Astra holds no other role, so seating it keeps supervision
#:   and debate cleanly separate.
#: * A longer chain is not a safer one. Every extra link is another way for a
#:   run to quietly change hands, and the third link only ever fires in a
#:   state where the operator would rather be told than served.
#:
#: So two links, and then :class:`quadratus.routing.OrchestratorUnavailable`.
#: A run with no orchestrator stops; it does not degrade into one.
#:
#: A security-classified segment yields the seat for a different reason --
#: Fable's classifiers would refuse to discuss the subject -- and lands on the
#: same deputy. Astra's own refusal behaviour on security work is
#: undocumented, which is an unknown rather than a clean bill of health; what
#: makes the excursion safe is not the deputy's classifier but its structure,
#: which defers the work and has it checked across vendor lines
#: (:func:`quadratus.routing.open_security_excursion`).
#:
#: Every entry is a floating alias, enforced below. The seat is chosen once
#: and held for a whole session, so it is the single worst place in the
#: system to pin an iteration -- nothing would revisit the choice until
#: someone noticed the run was still using last quarter's model.
ORCHESTRATOR_CHAIN: List[str] = [
    "claude:fable",
    "openai:gpt-6-astra",
]


def assert_floating_orchestrators(chain: Optional[Sequence[str]] = None) -> None:
    """Fail loudly if any orchestrator seat is pinned to one release.

    Called at import. A pinned orchestrator alias is not a runtime error --
    everything works, on a model that is quietly a generation behind -- which
    is precisely why it needs to be caught the moment someone edits the table
    rather than by whoever eventually wonders about the output.
    """
    for key in chain if chain is not None else ORCHESTRATOR_CHAIN:
        spec = _BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"orchestrator chain names an unknown model: {key}")
        # Floating first: it is what this function is named for, and it is the
        # failure that hides. A missing capability tag shows up the moment the
        # seat is used; a pinned alias never shows up at all.
        if not spec.floating:
            raise ValueError(
                f"{key} is pinned to one release; orchestrator seats must "
                f"address a model line so the run follows it forward"
            )
        if Capability.ORCHESTRATE not in spec.caps:
            raise ValueError(f"{key} is in the orchestrator chain without ORCHESTRATE")


assert_floating_orchestrators()

def peers_for(mode: str, orchestrator: str) -> List[str]:
    """Brain-trust peers for ``mode``.

    The brain trust is fixed. An earlier design recused whoever held the
    orchestrator seat and promoted a substitute off a bench, on the reasoning
    that a supervisor should not compete in the debate it supervises. That was
    the wrong trade twice over: it cost the brain trust its second-strongest
    member exactly when the primary orchestrator was already unavailable, and
    the only available substitutes were a tier below the seats they filled.
    A weaker brain trust is a worse failure than a supervisor with a stake.
    With three vendors in the lineup there is no bench left to promote from
    at all, which turns the old design from a bad trade into an impossible
    one.

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


@dataclass(frozen=True)
class ControlPlaneRole:
    """A cheap model doing control-plane work, and the terms it does it under.

    ``supervised_by`` is the load-bearing field. Some control-plane work is
    genuinely self-contained -- pull the fields out of this text, judge this
    checklist -- and some only looks that way. An interview is the second
    kind: the questions asked determine what the whole run is aimed at, a
    thin answer needs a follow-up nobody scripted, and the operator's replies
    have to be read for what they imply rather than transcribed. A model is
    cheap enough to conduct it and not trusted to *conduct* it, which is not
    a contradiction: the seat supplies the script, reads the answers, and
    decides the follow-ups, and the cheap model is the voice.
    """

    model: str
    #: Seat that must supply this role's inputs and act on its outputs. None
    #: means the role stands alone, which is a claim about the work being
    #: genuinely self-contained -- not a default to reach for.
    supervised_by: Optional[str] = None
    #: What the supervisor owes the role, in one line, so an implementer
    #: cannot wire the model up and call it done.
    mandate: str = ""


#: Control-plane roles. Cheap by task shape, not by importance: convergence
#: judging is load-bearing and still checklist work. Haiku was verified
#: returning a correct, well-calibrated structured verdict on a seeded defect.
CONTROL_PLANE: Dict[str, ControlPlaneRole] = {
    # Operator directive, 2026-09-12: Sonnet may hold the interview, never on
    # its own. It is worker-tier, and the interview is the one control-plane
    # role whose output shapes everything downstream.
    "interview": ControlPlaneRole(
        "claude:sonnet",
        supervised_by="orchestrator",
        mandate=(
            "The orchestrator writes the questions, reads every answer, and "
            "decides each follow-up. The interviewer asks what it is given "
            "and reports back verbatim; it does not improvise questions, "
            "judge answers, or decide the interview is finished."
        ),
    ),
    "spec_extraction": ControlPlaneRole("claude:haiku"),
    "ledger_extraction": ControlPlaneRole("claude:haiku"),
    "convergence": ControlPlaneRole("claude:haiku"),
}


def control_plane_model(role: str) -> Optional[str]:
    """The model key for a control-plane role, or None if there is no such role.

    Callers that only need to dispatch should use this; callers that are
    *implementing* a role must read the entry itself, because ``supervised_by``
    changes what the implementation has to do.
    """
    entry = CONTROL_PLANE.get(role)
    return entry.model if entry else None


def resolve(key: str) -> Optional[ModelSpec]:
    """Look up a roster entry by ``provider:alias``."""
    return _BY_KEY.get(key)
