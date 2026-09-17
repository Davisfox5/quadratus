# Scored attempt 4 — stopped on the orchestrator's exhausted window

Run **bb61698d**, frozen engine **a0f16ec**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 to 3 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run.
See [the pre-launch freeze](../scored-attempt-4-freeze.json).

Davis authorised this attempt and stated before the launch that the Fable
window was exhausted and the Astra fallback was close to its limit, with the
standing instruction that a stop there is recorded and nothing is changed.

## Result

**Incomplete after 3.5 seconds and one provider call.** Claude Fable, the
orchestrator seat, returned a vendor limit envelope: *"You've reached your
Fable limit."* The seat was recomputed to GPT-6 Astra, as designed, and the
run then stopped at `RunBudgetExceeded: unknown_usage` **before Astra was
invoked**. Nothing was written; the saved source is byte-identical to the
frozen input, all 30 hashes equal.

Private **0 of 9**, public examiner **0 of 13**, unchanged GameTape baseline
**100 tests passing** in a fresh network-disabled credential-free container
with hashes verified before and unchanged after. Those numbers are determined
by the empty diff rather than measured against delivered behaviour; they are
run for comparability with attempts 1 to 3.

## Why it stopped where it did

Two safety rules met, and the order matters.

The exhaustion path worked. `Session._ask_seat` treats a window limit as the
liveness check arriving late: the transport records it, the seat is recomputed,
the question is asked once more. `progress.log` shows exactly that —
`claude:fable is out of window; the seat falls to openai:gpt-6-astra`.

The accounting rule then refused the re-ask. The vendor's limit envelope
carries `is_error: true`, an all-zero `usage` and **an empty `modelUsage` map**.
Under the repair batch a present-but-empty `modelUsage` cannot establish
complete per-model usage, so the attempt is recorded as unknown usage, and an
unknown-usage attempt stops the run. `budget.json` shows
`unknown_usage_attempts: 1`, `reported_tokens: 0`, and the ledger's second row
is Astra with `invoked: false`.

So the run did not stop because both seats were gone. It stopped because a
call that failed for a vendor limit, and therefore spent nothing, was counted
as spend the harness could not measure. Whether that is the wanted behaviour
is an operator question, not a defect to patch mid-lane: the rule exists so a
*successful* call with unreadable usage cannot let a run continue on a partial
count, and a rejected request is a different case. **Astra's own window state
remains unknown, because Astra was never called.**

## Not changed

Per Davis's standing instruction, nothing was adjusted in response to this
stop. No retry was made. No budget, role, tolerance or private-case change.
The container and the credential seed were removed.
