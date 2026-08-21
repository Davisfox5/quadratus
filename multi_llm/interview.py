"""The interview: a cheap model turns a wish into a buildable goal.

The first participant the operator meets is not the orchestrator and not a
coder -- it is the control plane's interview model (see
:data:`multi_llm.registry.CONTROL_PLANE`), whose only job is to ask plain
questions until the wish becomes one concrete paragraph. The expensive
models' windows are saved for work that needs them; this step is
conversation, not judgement.

For an existing codebase, the interview receives the repository scan first,
so it does not ask what the code already answers -- the questions shift from
"what are we building?" to "what should change, and for whom?".

The output contract is a single paragraph beginning ``GOAL:``. That paragraph
is stored verbatim as the session goal and re-emitted at the top of every
orchestrator prompt for the rest of the run, which is why getting its wording
right here -- once, with the operator in the loop -- is worth a few rounds of
questions.
"""

from __future__ import annotations

from typing import Callable, Optional

from .registry import CONTROL_PLANE

__all__ = ["conduct_interview", "InterviewAborted"]

_GOAL_MARKER = "GOAL:"

#: Interviews that run longer than this are scoping meetings, not interviews.
#: The forced close keeps a chatty interviewer from burning the operator's
#: patience: with the budget spent it must write the goal from what it has.
_MAX_ROUNDS = 8


class InterviewAborted(RuntimeError):
    """The operator ended the interview without a goal."""


def _interview_prompt(wish: str, context: str, transcript: str) -> str:
    parts = [
        "You are the intake interviewer for a software-building system. An "
        "operator has expressed a wish; your only job is to make it concrete "
        "enough to build, by asking plain questions -- one at a time, no "
        "jargon.",
    ]
    if context:
        parts.append(
            "The system has already scanned the codebase this work concerns. "
            "Do not ask anything this scan answers; ask about intent, "
            "priorities, and constraints instead:\n\n" + context
        )
    parts.append(f"The operator's wish, verbatim:\n{wish}")
    if transcript:
        parts.append(f"The interview so far:\n{transcript}")
    parts.append(
        "If anything essential is still unknown -- what it is, who uses it, "
        "what it must do first, what can wait, anything it must run on -- ask "
        "exactly one short question and nothing else. When you know enough, "
        f"reply with one paragraph beginning '{_GOAL_MARKER}' that states all "
        "of it plainly. That paragraph is stored word-for-word as the "
        "project's goal, so write it to be read a hundred times."
    )
    return "\n\n".join(parts)


def conduct_interview(
    wish: str,
    *,
    invoke: Callable[..., str],
    ask: Callable[[str], str],
    context: str = "",
    model: Optional[str] = None,
    max_rounds: int = _MAX_ROUNDS,
) -> str:
    """Run the interview and return the goal paragraph (marker stripped).

    ``invoke`` is the usual ``(model_key, prompt) -> reply`` callable;
    ``ask`` puts one question to the operator and returns their answer -- an
    empty answer (or EOF at a terminal) aborts rather than letting the goal
    be built on silence.
    """
    interviewer = model or CONTROL_PLANE["interview"]
    transcript = ""
    for _round in range(max_rounds):
        reply = invoke(interviewer, _interview_prompt(wish, context, transcript)).strip()
        if _GOAL_MARKER in reply:
            goal = reply.split(_GOAL_MARKER, 1)[1].strip()
            if goal:
                return goal
        question = reply.splitlines()[0].strip() if reply else ""
        if not question:
            break
        try:
            answer = ask(question)
        except EOFError as exc:
            raise InterviewAborted("the operator ended the interview") from exc
        if not (answer or "").strip():
            raise InterviewAborted(
                f"no answer to the interview question: {question!r}"
            )
        transcript += f"\nQ: {question}\nA: {answer.strip()}"

    # Budget spent: the interviewer must commit to a goal from what it has.
    final = invoke(
        interviewer,
        _interview_prompt(wish, context, transcript)
        + f"\n\nThe question budget is spent. Reply now with the "
        f"'{_GOAL_MARKER}' paragraph using what you have; note any "
        f"assumption you had to make inside the paragraph itself.",
    ).strip()
    if _GOAL_MARKER in final:
        goal = final.split(_GOAL_MARKER, 1)[1].strip()
        if goal:
            return goal
    # A malformed final reply still becomes a usable goal rather than a lost
    # interview: the wish plus the transcript is what the interviewer knew.
    return (wish.strip() + ("\n" + transcript.strip() if transcript else "")).strip()
