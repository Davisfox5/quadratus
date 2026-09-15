# Bounded closeout verification — 2026-09-15

Codex executed two unscored subscription-CLI calls using engine `c3a47c2` and
[the probe harness](../../../tools/acceptance/closeout_probe.py). The harness
hash and every mounted runtime file are committed in
[evidence/closeout-live-1/freeze.json](evidence/closeout-live-1/freeze.json).

| Call | Outcome | Reported tokens | Time | Attempts |
| --- | --- | ---: | ---: | ---: |
| Grok closeout | Returned a usable summary | 6,112 | 11.23s | 1 |
| Claude Fable closeout | Vendor safeguard refusal | 5,103 | 2.25s | 1 |

Total: **11,215 reported tokens**, including cached input once. No retries or
model substitutions. Both ran in an empty CWD, with a 60-second call timeout,
70-second run deadline, 80-second whole-container watchdog, one-attempt limit
and the existing 50,000-token probe threshold per call. Both containers were
removed and their temporary credential seeds deleted. No application source,
examiner cases, host home, past sessions or Git history were mounted.

## Evidence and limits

The current Session closeout builder generated the shared 11,456-byte prompt
from the saved task description, its assistant transcript, the two changed
files' before/after diff and the recorded 102-test result. The before source
came from GameTape a8772ab and the after source from the published scored
archive. Source reconstruction happened on the host solely to prepare evidence;
the containers received the resulting prompt, not the application. The saved
test result is explicitly labelled historical. This is a closeout replay,
not another blind task or independent application test.

Grok reports one model call and `end_turn`. Its reply identifies unfinished
players/duplicates/UI/docs and distinguishes the old test result from a new
check. The old closeout reported 258,413 tokens; the replay reports 6,112,
**97.6% fewer**. This is a single before/after observation with different
context/cache conditions, not a controlled benchmark or general savings rate.
The recorded argv requested one turn and low effort. There is no complete
tool-call trace in its returned envelope, so this does not prove universal
absence of hidden tool activity or native children.

Claude's argv requested no tools and one turn. The vendor returned a refusal
with label `reasoning_extraction`, no output tokens, one turn, and no reported
subagents. We did not retry, rephrase around the safeguard or substitute a
model. The provider correctly preserved `ProviderRefusal` and accounted for
2 input + 5,101 cache-creation tokens. No auxiliary row appeared in this live
call, so the earlier archived replay (68,382 including 2,817 Haiku once) remains
the auxiliary-counting proof. Successful Claude closeout remains unverified;
the label alone does not establish which prompt text triggered it.

Raw stdout stays private; the published extracts omit vendor thought fields,
credentials and other unnecessary envelope fields. Hashes bind the raw stdout
for an exact follow-up. Prompt, Grok reply, invocation records, usage fields,
summary flags and supervisor outcomes are in
[evidence/closeout-live-1](evidence/closeout-live-1/sha256.json).

## Handoff

Claude owns independent review of this saved evidence and the comparison claims.
Codex owns any resulting disposition. No further live execution is queued.
The safeguard refusal is a recorded limitation, not authorization to bypass it.
Full-worker routing coverage remains unvalidated; this probe never tested it.

Validation: 45 focused closeout/summary/accounting tests passed; Ruff on the
new harness passed. Source/document diff checks passed; the exact hashed
prompt retains two whitespace-only diff context lines, which Git flags as
trailing whitespace. Those evidence bytes were deliberately preserved. Production code is unchanged from
c3a47c2. The two host setup errors (missing PYTHONPATH, source/ archive prefix)
were corrected before the first provider call and consumed no model attempts.
