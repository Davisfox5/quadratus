# Scored attempt 5 — the fallback seat was reached, and could not read the source

Run **984ad6ee**, frozen engine **adc774e**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 to 4 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run.
See [the pre-launch freeze](../scored-attempt-5-freeze.json).

## Result

**Incomplete after 158 seconds and two calls**, 56,356 reported tokens.
Nothing was written; the saved source is byte-identical to the frozen input.
Private **0 of 9**, public examiner **0 of 13**, unchanged baseline **100
tests passing** offline, all determined by the empty diff.

| Call | Seat | Outcome | Tokens | Seconds |
| --- | --- | --- | ---: | ---: |
| 1 | Claude Fable | vendor window limit | 0 | 2.2 |
| 2 | GPT-6 Astra | ok, asked the operator a question | 56,356 | 154.3 |

## The fix from attempt 4 worked

Fable's window was still gone and its limit envelope still reports zero tokens.
This time the budget read that as zero rather than as unknown, stayed open, and
the fallback seat was reserved and called: `progress.log` shows the seat
falling to Astra, and the ledger's second row is Astra **invoked, outcome ok**.
That is precisely what attempt 4 could not do.

## Why it stopped

Astra could not read the project. Its own sandbox failed inside ours:

```
bwrap: No permissions to create a new namespace, likely because the kernel
does not allow non-privileged user namespaces.
```

The Codex CLI sandboxes itself with bubblewrap, which needs to create a user
namespace. The acceptance container runs `--cap-drop ALL` with
`--security-opt no-new-privileges`, so that is denied. Astra handled it
correctly: rather than inventing a task from an unread tree, it used the `ASK`
channel — *"Can you restore read-only shell access to this copy? Source
inspection failed ... so I cannot ground the next task in actual files."* A
blind run configures no operator channel, so the harness raised
`OperatorInputNeeded` and stopped, which is the designed behaviour.

## This was not new, and it changes how attempt 3 reads

The same `bwrap` failure appears twice in **attempt 3's** preserved GPT-5.6 Sol
lead output. That lead was blind to the source as well, and it answered by
commissioning a single worker for the entire task after 559 output tokens.
Attempt 3's headline finding stands — a restricted worker's tools were never
checked against its errand — but the lead's decision to hand over everything is
now partly explained by its inability to read anything.

Claude seats are unaffected: no `bwrap` appears in any Fable or Haiku call, in
any attempt. **Every OpenAI seat in every isolated run so far has been unable
to read the project.** No OpenAI seat has yet done grounded work in this
harness.

## The decision this needs

Restoring source access to OpenAI seats means one of: allowing user namespaces
in the acceptance container, which weakens the outer isolation that makes these
runs safe to launch; or relaxing the Codex CLI's own `--sandbox read-only`,
which is a vendor control this project has repeatedly declined to weaken. Both
are operator decisions, not review-lane repairs, and nothing here was changed.

A third, additive option touches neither: the container preflight currently
checks versions, authentication and a Chromium launch, but never asks whether
each installed CLI can actually read a file in the mounted tree. That check
would have caught this before any window was spent.

## Reproduce

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` (`source/` prefix)
and verify `source-after-sha256.json`. Raw stdout and vendor sessions stay
private outside every repository; `vendor-usage-extract.json` names each call's
private file and its SHA-256. Container and credential seed were removed.
