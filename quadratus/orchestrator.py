"""Collaboration orchestrator.

Coordinates the providers so they reach consensus, self-assign roles by their
strengths, then build and refine a solution together:

1. **Plan** — every available model independently proposes an approach and a
   division of labour (run in parallel).
2. **Consensus** — a coordinator merges the proposals into one agreed plan with
   explicit per-model role assignments.
3. **Build & debate** — the models implement and then adversarially refine the
   solution according to their assigned roles, converging over
   ``settings.rounds`` rounds.
4. **Synthesis** — a coordinator merges every contribution into one answer.

The workflow degrades gracefully: with a single available provider it produces
a high-quality solo answer; with none it raises a clear error.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from . import prompts
from .providers import LLMProvider, ProviderError, ProviderRefusal, Turn, build_providers

log = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass
class StageResult:
    """The output of a single stage of the collaboration."""

    provider: str
    label: str
    role: str
    content: str


@dataclass
class CollaborationResult:
    """The complete result of a collaboration run."""

    task: str
    plan: str = ""
    stages: List[StageResult] = field(default_factory=list)
    final: str = ""
    final_provider: str = ""

    @property
    def participants(self) -> List[str]:
        seen: List[str] = []
        for s in self.stages:
            if s.label not in seen:
                seen.append(s.label)
        return seen


class Orchestrator:
    """Runs the multi-model collaboration."""

    def __init__(self, settings, providers: Optional[Sequence[LLMProvider]] = None):
        self.settings = settings
        self.providers = list(providers) if providers is not None else build_providers(settings)
        self.available = [p for p in self.providers if p.available()]

    # -- introspection -------------------------------------------------------
    def status_lines(self) -> List[str]:
        return [p.status for p in self.providers]

    def _coordinator(self) -> LLMProvider:
        choice = (self.settings.synthesizer or "lead").lower()
        if choice not in ("lead", ""):
            for p in self.available:
                if p.name == choice:
                    return p
        return self.available[0]

    @staticmethod
    def _to_turns(history) -> List[Turn]:
        turns: List[Turn] = []
        for item in history or []:
            if isinstance(item, Turn):
                turns.append(item)
            elif isinstance(item, dict):
                turns.append(Turn(item.get("role", "user"), item.get("content", "")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                turns.append(Turn(item[0], item[1]))
        return turns

    # -- phases --------------------------------------------------------------
    def _gather_proposals(
        self, task: str, participants: Sequence[str], turns: Sequence[Turn]
    ) -> Dict[str, str]:
        """Ask every available model for a plan proposal, in parallel."""

        def propose(provider: LLMProvider):
            return provider.label, provider.generate(
                prompts.planning_prompt(task, participants),
                system=prompts.PLANNING_SYSTEM,
                history=turns,
            )

        proposals: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(self.available)) as pool:
            futures = [pool.submit(propose, p) for p in self.available]
            for fut in futures:
                try:
                    label, text = fut.result()
                    proposals[label] = text
                except ProviderError as exc:
                    log.warning("Planning proposal failed: %s", exc)
        return proposals

    # -- main entry point ----------------------------------------------------
    def run(
        self,
        task: str,
        *,
        history=None,
        progress: ProgressFn = _noop,
    ) -> CollaborationResult:
        if not self.available:
            raise ProviderError(
                "No LLM providers are available. Set at least one of "
                "ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY."
            )

        turns = self._to_turns(history)
        result = CollaborationResult(task=task)

        # Single-provider fast path: a solid solo answer, no planning overhead.
        if len(self.available) == 1:
            p = self.available[0]
            progress(f"{p.label} is solving the task...")
            out = p.generate(prompts.solo_prompt(task), system=prompts.SOLO_SYSTEM, history=turns)
            result.stages.append(StageResult(p.name, p.label, "Sole author", out))
            result.final = out
            result.final_provider = p.name
            return result

        participants = [p.label for p in self.available]
        coordinator = self._coordinator()

        # Phase 1 — parallel planning proposals.
        progress("Phase 1 — models analysing the task and proposing roles...")
        proposals = self._gather_proposals(task, participants, turns)
        proposals_text = "\n\n".join(
            f"--- {label} proposes ---\n{text}" for label, text in proposals.items()
        ) or "(no proposals were produced)"

        # Phase 2 — consensus plan with explicit role assignments.
        progress(f"Phase 2 — {coordinator.label} forming the consensus plan & roles...")
        plan = coordinator.generate(
            prompts.consensus_prompt(task, participants, proposals_text),
            system=prompts.CONSENSUS_SYSTEM,
            history=turns,
        )
        result.plan = plan
        result.stages.append(
            StageResult(coordinator.name, coordinator.label, "Consensus plan & role assignment", plan)
        )

        # Phase 3 — build & debate, each model acting in its assigned role.
        lead = self.available[0]
        reviewers = self.available[1:]

        progress(f"Phase 3 — {lead.label} drafting the initial solution...")
        current = lead.generate(
            prompts.lead_prompt(task, plan, f"{lead.label} (lead implementer per the plan)"),
            system=prompts.LEAD_SYSTEM,
            history=turns,
        )
        result.stages.append(StageResult(lead.name, lead.label, "Lead implementer", current))

        contributors = [lead.label]
        for round_idx in range(self.settings.rounds):
            for reviewer in reviewers:
                progress(
                    f"Phase 3 — {reviewer.label} reviewing & refining "
                    f"(round {round_idx + 1}/{self.settings.rounds})..."
                )
                role = f"{reviewer.label} (reviewer/refiner per the plan)"
                try:
                    current = reviewer.generate(
                        prompts.review_prompt(task, plan, role, current, ", ".join(contributors)),
                        system=prompts.REVIEW_SYSTEM,
                        history=turns,
                    )
                except ProviderRefusal as exc:
                    # A declined review round is a missing voice, not a lost
                    # run: the current best solution stands and the decline is
                    # recorded where the operator will see it. The lead draft
                    # and the synthesis have no such stand-in and propagate.
                    log.warning("Skipping %s this round: %s", reviewer.label, exc)
                    result.stages.append(
                        StageResult(
                            reviewer.name,
                            reviewer.label,
                            f"Reviewer/refiner (round {round_idx + 1}, declined)",
                            f"[{reviewer.label} declined this round: {exc}]",
                        )
                    )
                    continue
                result.stages.append(
                    StageResult(
                        reviewer.name,
                        reviewer.label,
                        f"Reviewer/refiner (round {round_idx + 1})",
                        current,
                    )
                )
                if reviewer.label not in contributors:
                    contributors.append(reviewer.label)

        # Phase 4 — synthesis.
        synth = coordinator
        progress(f"Phase 4 — {synth.label} synthesising the definitive solution...")
        contributions = "\n\n".join(
            f"--- {s.label} ({s.role}) ---\n{s.content}"
            for s in result.stages
            if s.role != "Consensus plan & role assignment"
        )
        final = synth.generate(
            prompts.synthesis_prompt(task, plan, contributions),
            system=prompts.SYNTHESIS_SYSTEM,
            history=turns,
        )
        result.stages.append(StageResult(synth.name, synth.label, "Synthesizer", final))
        result.final = final
        result.final_provider = synth.name
        return result
