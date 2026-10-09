"""One definition per routing label, for the decider and the docs alike
(2026-10-01, after the first live Jev run).

The run `20261001T022457Z-6602cd28` sent Jev bare label names with no
criteria text and got ``backend`` for a task that added a route, an SVG, a
template link and a test. Whether that answer is wrong is a question for the
sample set; that the question gave the model nothing to judge by is not.
This module is the single source the Jev ``choice`` question and
``docs/decisions-api.md`` both render from, so the words the model is
judged against are the words the operator can read, and a drift between the
two is a failing test rather than a surprise.

Every label the engine routes on has a one-line definition with an example
drawn from this repository's own work. Difficulty is defined by reasoning
burden, the context a model must hold and what the task depends on; never by
line count, which the task ceiling already bounds. Mixed work gets a stated
tie-break rather than a coin toss. ``LABELS_VERSION`` is recorded on every
decision so a result can name the definitions it was judged against.

Routing policy, stated-label precedence, tier admission, capability checks
and the budget are untouched: this changes what the decider is asked, not
what the engine does with the answer.
"""

from __future__ import annotations

from typing import Dict, Mapping, Sequence

#: Bumped whenever a definition's wording changes. Recorded in the decision
#: evidence (``result.json.decisions[].labels``) so two runs can be told
#: apart by the question they asked, not only the answer they got.
LABELS_VERSION = "kind-v1/difficulty-v1"

KIND_DEFINITIONS: Dict[str, str] = {
    "scope": "Turning the operator's request into a stated goal with constraints, before any task exists. "
             "Example: write the goal and non-goals for 'add import preview'.",
    "decompose": "Breaking a stated goal into bounded tasks with acceptance checks. "
                 "Example: split the import-preview goal into route, parser and page tasks.",
    "architect": "Open-ended structural judgement before code exists: which pieces, which boundaries, which trade-off. "
                 "Example: decide whether previews are computed on upload or on request.",
    "backend": "Server-side logic, routes, services and APIs where no rendered page is part of the acceptance. "
               "Example: add a JSON endpoint that lists projects.",
    "frontend": "Anything a user sees in a browser: templates, markup, styles, static assets, page behaviour; "
                "the acceptance includes how a page renders. Example: add a favicon and its link tag.",
    "mobile": "Native or mobile-platform code (Android, iOS) and its build or device constraints. "
              "Example: fix a Kotlin screen's rotation state.",
    "bulk": "High-volume mechanical edits where the same change repeats across many files and judgement is low. "
            "Example: rename a function across forty call sites.",
    "glue": "Wiring existing pieces together with little new logic: configuration, adapters, plumbing. "
            "Example: register an existing blueprint and pass its settings through.",
    "review": "Finding defects nobody has reported yet in code that already exists. "
              "Example: audit the upload handler for missing validation.",
    "debug": "Root-causing a defect that has already announced itself (a failing test, a traceback, a bug report). "
             "Example: find why the import page returns 500 on empty files.",
    "concurrency": "Work whose failure mode is a race, a deadlock or a lost update: locks, workers, shared state. "
                   "Example: make the job queue safe for two workers.",
    "security": "Authentication, authorisation, secrets, injection, or hardening against hostile input. "
                "Example: stop path traversal in the file download route.",
    "refactor": "Restructuring code without changing behaviour, with existing tests as the net. "
                "Example: extract the parser from the route into its own module.",
    "test": "Adding or repairing tests as the deliverable itself, not as part of another change. "
            "Example: cover the parser's edge cases with unit tests.",
    "comprehend": "Reading the codebase to answer a question about it, producing an explanation, not code. "
                  "Example: explain how uploads reach storage.",
    "iac": "Infrastructure as code: deployment, containers, CI pipelines, environment definitions. "
           "Example: add a CI job that runs the browser tests.",
    "docs": "Documentation as the deliverable: README, guides, docstrings, changelogs. "
            "Example: document the import-preview endpoint.",
    "perf": "Making something measurably faster or lighter, with a measurement as the acceptance. "
            "Example: cut the project-list query from 40 round trips to one.",
    "data": "Schema, query and migration work on stored data. "
            "Example: add a column with a migration and backfill.",
    "general": "Work that fits no other label, or spans several with none dominant. "
               "Example: a small change touching a route, a template and a test equally.",
}

#: Difficulty is about the reasoning a capable coding model must do, the
#: context it must hold, and what the task depends on. It is not line count:
#: a forty-line rote change is rote, a four-line change to a lock is complex.
DIFFICULTY_DEFINITIONS: Dict[str, str] = {
    "rote": "Mechanical with a known recipe: one obvious way to do it, little context beyond the named files, "
            "nothing else depends on the choice. Example: add a link tag to a template.",
    "simple": "One clear idea with a few steps; a model can hold the whole task and its context at once and "
              "verify it locally. Example: add a route plus its test.",
    "standard": "Several interacting steps, or dependencies on code outside the named files, where a wrong "
                "choice early costs rework. Example: a parser that must agree with an existing validator.",
    "complex": "Genuinely hard reasoning: many interacting constraints, unclear specification, or failure modes "
               "that are hard to observe (races, data loss). Example: make a migration safe under live traffic.",
}

KIND_GUIDANCE = (
    "Pick the label whose definition the acceptance checks exercise most. Mixed work: when a page a user "
    "sees changes and that rendering is part of the acceptance, choose frontend over backend; when the "
    "defining risk is a race, data loss or hostile input, choose concurrency, data or security over the "
    "layer it lives in; when several kinds share the work equally and none of those apply, choose general."
)

DIFFICULTY_GUIDANCE = (
    "Judge the reasoning burden, the context a model must hold and what the task depends on. Line count "
    "is not difficulty: a long rote edit is rote, a short change to shared state is complex. Most "
    "well-sized tasks are simple; reserve complex for genuinely hard reasoning."
)

DEFINITIONS: Dict[str, Dict[str, str]] = {"task.kind": KIND_DEFINITIONS, "task.difficulty": DIFFICULTY_DEFINITIONS}
GUIDANCE: Dict[str, str] = {"task.kind": KIND_GUIDANCE, "task.difficulty": DIFFICULTY_GUIDANCE}


def definitions_for(decision_id: str, answers: Sequence[str]) -> Dict[str, str]:
    """The definition of each answer, in the answer order the caller uses.

    Raises ``KeyError`` for an answer with no definition: an undefined label
    would be sent as a bare name again, which is the fault this module exists
    to close.
    """
    table = DEFINITIONS.get(decision_id, {})
    missing = [a for a in answers if a not in table]
    if missing:
        raise KeyError(f"{decision_id}: no definition for {', '.join(missing)}")
    return {a: table[a] for a in answers}


def guidance_for(decision_id: str) -> str:
    return GUIDANCE.get(decision_id, "")


def render_markdown(definitions: Mapping[str, Mapping[str, str]] = DEFINITIONS,
                    guidance: Mapping[str, str] = GUIDANCE) -> str:
    """The definitions as the docs carry them, so a test can hold the two equal."""
    lines = [f"Definitions version `{LABELS_VERSION}`."]
    for decision_id, table in definitions.items():
        lines += ["", f"**{decision_id}** -- {guidance.get(decision_id, '')}", ""]
        lines += [f"- `{label}`: {text}" for label, text in table.items()]
    return "\n".join(lines) + "\n"
