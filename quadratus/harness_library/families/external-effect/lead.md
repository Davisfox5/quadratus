# external-effect: lead

You are writing or changing something that sends, publishes, seeds, loads or
spends against a real system. Done means the default run sends nothing, an
apply run needs a named authorization, production is refused unless the
policy allows it, failure exits nonzero, and the done output says what was
not sent. The gate runner declares done.

Inputs you must have: the effect and where it lands, the target environment,
and the reversal path. An apply run also needs a named authorization with
scope and cap. Missing one means stop and return status blocked naming it.

Procedure

1. Read the adapter's apply flag, allowed targets and authorization format.
   Read the closest existing script and match its flags and output.
2. Default mode composes, diffs or screenshots and sends nothing. The apply
   flag is explicit and named.
3. Check the target against the allowed list before any connection. Refuse
   production unless listed.
4. Require the authorization line for apply; record it in the run output.
5. Make apply idempotent: dedupe key, upsert, or a marker the second run
   reads.
6. Exit nonzero on partial failure and list the failed items.
7. Apply the cost or row cap and stop at it.
8. Tests exercise the dry-run path with fixtures and mocked transports.
9. Run the gates: scope, dry-run-tests, unit-tests, lint, bash-syntax. Paste
   the tails. Your own verification never uses apply.

Do not

- Run apply, execute, send or publish as a verification step.
- Install, load or enable a scheduler.
- Point at a production URL, account or DSN the policy does not list.
- Report a dry run as if it applied, or the reverse.

Return: effect and target, mode run with the authorization line for apply,
what was not sent with counts, reversal path, and the cap with where it
stopped.
