# O-NEXT-11: repeated contract mismatch lines

Measured at 7403511. No engine edits. Replays in
`tests/lifecycle/test_mismatch_repeats.py`.

## TL;DR

- One persistent drift is written to `TaskOutcome.mismatches` once per read,
  so it shows up 2 to 4 times in the three replays below.
- Every decision reads mismatches for presence only. Blocked is blocked
  whether the line is there once or four times.
- Count and order do show in three places: the `missing` list in
  `result.json`, the `blockers` count in a progress note, and the
  600-character `CompletionUnproven` stop text, where repeats can push a
  later blocker (a terminal fact) out of the message.
- Recommendation: dedupe at append, the same `if note not in` guard five
  other writers of this list already use.

## Call sites and how often each runs per task

| Site | Requirement | Runs per task | Driven by |
|---|---|---|---|
| `session.py:2280` `_run_task` | `security_verification` | 1 | task entry |
| `session.py:1391` `collaborators_for` via `_collaboration_applicable` (2153) | `design_collaboration_applicable` | 1, plus 1 on lead recovery | dispatch (2290), recovery (2371) |
| `session.py:3307` `_brief_design_reviewers` | `design_collaboration_applicable` | 1 | after draft (2388) |
| `session.py:4806` `_collaborator_prompt` | `design_collaboration_applicable` | 1 per collaborator | review loop (2413-2415) |
| `session.py:4241` `_design_instruction` from `_lead_prompt` (4698) | `design_instruction` | 1 per lead prompt build | `build()` (1746): each FETCH round (1449, 1461), each consult/worker round of `_drafting_loop` (1762), again on lead recovery (2380) |
| `session.py:4249` (direct append) | `capture_page` | same as the line above, harness tasks only | same |
| `session.py:3340` `_check_design` | `design_evidence` | 1 | end of task (2502) |
| `session.py:3449` `_check_design` | `design_review` | 1 | end of task |
| `session.py:4910` `_run_integration_gate` (full gate only) | `checks` | 1, plus 1 after a design-fix | end of task (2501), design-fix (3387 or 3422, at most one). Gate-fix rounds loop inside one call (4927), so they add nothing. Security path 2647/2738; merge gate 3750. |
| `session.py:2166` `_required`, contract missing | any | same as its caller | same |

Revision (4809), fix (4883), gate-fix (4929) and design-fix (3381, 3418)
prompts do not rebuild the lead prompt or reread any requirement.

## Replays (whole controller, drift set when t1's draft starts and kept)

| Replay | Drift | Recovery round | Lines on t1 |
|---|---|---|---|
| R1 | `integration_gate=None` | one design-fix, then the full gate again | `checks` x2 |
| R2 | `design_cross_check=False` | one design-fix; 1 collaborator | `design_collaboration_applicable` x2, `design_review` x1 (3 lines, 2 distinct) |
| R3 | `design_cross_check=False` | lead recovery (grok cancels, second lead redrafts), then design-fix; 1 collaborator | `design_collaboration_applicable` x3, `design_review` x1 (4 lines, 2 distinct) |

In every replay `parity.missing` in `result.json` carries each copy, one
`t1.contract mismatch: ...` entry per line.

## Consumers

| Consumer | file:line | Depends on |
|---|---|---|
| `missing_facts` | `outcome.py:259` | maps 1:1, copies kept |
| `parity` `complete` | `outcome.py:497-498` | presence (`not missing`) |
| `parity` `missing` in result.json | `outcome.py:497-498`, `project_run.py:456` | count and order (raw list) |
| `workflow.tasks[].mismatches` in result.json | `outcome.py:215` (`asdict`), `project_run.py:454` | count and order (raw list) |
| `discharged_by_continuation` | `outcome.py:431` | presence (truthiness) |
| `completion_blockers` | `outcome.py:446` | copies kept, placed first for the task |
| guard decision | `completion_decision.py:434` | presence (`if blockers`) |
| guard stop text | `completion_decision.py:443` | count and order: joined then cut at 600 chars |
| progress note | `session.py:3297` | count (`len(decision.blockers)`) |
| `partial_mismatches` | `session.py:3280-3283` | separate channel, already deduped at append |
| report.md | `project_run.py:342` | only through the stop text in `Error:` |

The stop-text case is pinned by
`test_repeats_take_room_in_the_guards_600_character_stop_text`: six copies
of one line (two `collaborators_for`, one brief, three collaborators) push a
t1 integrity fact out of the text; deduped, it fits. R3's own four copies
still fit.

## Recommendation: dedupe at append

Add `if note not in outcome.mismatches` at `session.py:2148`, 2166 and 4249,
matching 1852, 1880, 3235, 4415 and 4572. Every decision stays the same and
the stop text and result.json stop carrying noise. The cost is losing how
many times a drifted value was read, which nothing consumes today. Dedupe at
render would touch three consumers and leave the raw record noisy.
