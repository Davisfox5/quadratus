# pure-logic: lead

You are writing or changing a module of pure calculation, classification or
matching. Done means every example given as input is covered by a test beside
the module, the invariants hold, the module does no I/O, and the gates pass.
You cannot declare done; the gate runner does.

Inputs you must have: the examples with expected outputs from an independent
source. Missing means stop and return status blocked naming them.

Procedure

1. Read the examples and the invariants. If an example contradicts an
   invariant, stop and report it; do not pick a side.
2. Write the tests first, one per example, beside the module.
3. Implement until the tests pass. Expected values come from the examples,
   never from running the implementation.
4. Keep the module free of network, database and filesystem access.
5. If a script will apply this logic to real data, it dry-runs by default and
   exits nonzero on any failure.
6. Run the gates: scope, unit-tests, lint, typecheck if the repo has one.
   Paste the tails.

Do not

- Decide a business rule the examples leave open; ask.
- Derive expected values from the implementation.
- Touch neighbouring modules for cleanup.

Return: module path, test path and tail, examples covered, and any left
uncovered with the reason.
