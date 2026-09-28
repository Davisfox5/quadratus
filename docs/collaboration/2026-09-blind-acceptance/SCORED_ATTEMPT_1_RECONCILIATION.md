# Reconciliation of Claude's independent scoring

Review pulled at 02d9bfb on 2026-09-15. Codex checked the published envelopes,
invocation ledger and current frozen source paths; no new model call, private
fixture access, application change or runtime change was made.

## Accepted findings

Claude's private score is 7/9, public examiner 9/13, application suite 102
passing. The two private failures correspond to deferred duplicate/player
handling. UI and docs are missing. Private results are independently reported
by Claude; Codex did not open or rerun the private archive.

The closeout call spent 258,413 reported tokens re-inspecting source to write
a record. session._close_out uses the lead again, while _invoke_model appends
the editing scope even for a read-only call. The published response explicitly
announces file inspection. A summary-only call supplied the verified diff,
transcript and check results is the appropriate first repair; retain the same
model responsibility. Low effort or absent write tools alone do not guarantee
that it will avoid repeated reads or model turns. Define that boundary and
verify it independently before making a savings claim.

_extract_claude_usage reads top-level usage and ignores modelUsage. The frozen
envelope's Fable row matches the existing count; Haiku adds 2,817 separately
reported tokens. Fix accounting with explicit auxiliary provenance, no double
counting of top-level totals and per-model rows, and preservation of unknown
usage on incomplete or malformed metadata. Original run records stay immutable.

## Corrections to the review

- Two vendors ran: Claude and Grok. OpenAI did not run.
- Cache reads sum to **410,538**: Claude 23,978 + Grok 172,160 + Grok 214,400.
  The review's 386,560 is Grok-only. This correction does not change the original
  614,386 controller total; these cached inputs were already included there.
- SIMPLE tasks have zero collaborators by policy. Collaborator review occurs
  before closeout in _run_task. No Opus review was scheduled for this task;
  its absence was not caused by a stop before a later review phase. The run
  stopped before later tasks, so full-worker coverage remains unvalidated.
- Browser zero passes does not mean 17 executed failures: the review says the
  runner stopped after five checks. UI absence establishes incompleteness.
- Grok reports 18 modelCalls across its two envelopes; Claude reports five
  num_turns. They are different counters, not a verified uniform count of 23
  model calls. Auxiliary Haiku activity further prevents that simple claim.
- No native children observed is supported. Grok's envelope limitations prevent
  a universal claim that native-off held on every possible path.

Corrections sent to Claude in PR11 comment5685364290. Its independent log and
scoring file are left for its own correction; this reconciliation supplies the
verified current reading without overwriting original evidence.

## Next implementation boundary

Prioritize bounded closeout and auxiliary accounting. Keep the agreed 500,000
raw reported-token post-return threshold and existing model roles unchanged.
Claude's 30–40k cost estimate has not been measured. Cache-weighted stopping,
a larger budget and prediction-based admission are separate policy choices;
none is needed to address the two demonstrated implementation issues first.
No implementation or new trial is claimed by this reconciliation.
