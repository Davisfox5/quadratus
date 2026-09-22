# webhook-contract: lead

You are adding or changing one webhook handler or emitter. Done means the
signature is verified first with a replay window, every handler is wrapped in
claim, mark, release, the tenant is bound before any write, the contract doc
changed in the same PR, and the gates pass. The gate runner declares done.

Inputs you must have: the direction, the provider or consumer with its
signature scheme, the event types, and how the tenant is resolved from a
delivery. Missing one means stop and return status blocked naming it.

Procedure

1. Read the adapter's signature scheme, idempotency functions and proxy
   exclusion. Read the closest existing handler and match it.
2. Inbound: verify the signature against every configured secret before
   parsing the body; enforce the timestamp window; fail closed when a secret
   is unset.
3. Wrap the handler in claim, mark, release on the delivery id. A redelivery
   is a no-op; a crash releases the claim.
4. Resolve the tenant from the delivery's trusted identifiers using only the
   bootstrap lookups, bind it, then write.
5. Unknown event types are acknowledged and logged, never handled by a
   default branch.
6. Outbound: register the event with the standard envelope and a dedupe key;
   update the contract doc in this PR.
7. Confirm the path is excluded from tenant-routing middleware and session
   guards.
8. Fixtures sign with a test secret from the environment.
9. Run the gates in order: scope, contract-doc-test, unit-tests, import-smoke,
   lint. Paste the tails.

Do not

- Parse the body before verifying the signature.
- Write anything before the tenant is bound.
- Change a billing handler's verification or idempotency without the grant;
  that is a sensitive path, stop and report.
- Rename an envelope field without the contract doc and the consumer note.

Return: the verification line with its window, the claim, mark, release calls,
the tenant binding line, the event types with registry entries, and the
consumer-impact note.
