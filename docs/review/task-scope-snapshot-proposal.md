# Task-scope snapshot proposal — test-only, no engine change

Base: `a761edd5f66b566492beb5c016702f9f0e66e59a`. The selector file is
`tests/lifecycle/test_task_scope_snapshot_proposal.py`. Its three strict
xfails state a proposed dispatch invariant; two controls pass today. All
mutations are injected after the test has built a real `TaskContract` on a
`Session`. They are not observed Run 10–19 production events.

## Evidence and reachability

`TaskScope` and `TaskSpec` are mutable. The contract stores a canonical string
of `spec.scope` at dispatch, while `_measure_scope` and provider prompts read
the live `spec.scope` object. A post-dispatch in-place append of `app.py` and
increase from one to 200 lines makes the changed `app.py` pass the scope check
although the recorded contract still permits only `README.md` and one line.
The prompt likewise names the widened path. When `spec.scope is
config.default_scope`, `_snapshot_outer` correctly returns no *separate*
outer limit, but that leaves the same mutable task-scope reference as the only
enforcer: the same widening passes without even an `operator_limits` mismatch.

Normal writable controller dispatch does not currently supply this alias:
`_read_task` calls `read_scope` to build a task scope from the declaration,
then `_prepare_dispatch` applies `policy.scope`, which returns a separate
`TaskScope`. The fallback `scope = config.default_scope` is for a run without
that writable declaration path; Fleet's run-level write grant still controls
whether a provider can edit. A source search found one engine assignment to
`spec.scope`, in `_prepare_dispatch` before the contract, and no engine
assignment mutating its fields after dispatch. A direct/programmatic
`Session.run_task` caller can supply the same object, but no such production
trigger was observed. These probes establish a controller invariant gap, not
that a normal read-only run gained filesystem write access.

## Minimal correction for engine owner

After `_prepare_dispatch` has applied policy and before `_build_contract`,
hold an independent deep snapshot of the task's scope. Derive the contract's
recorded scope from that snapshot. Use the snapshot as a ceiling at every
scope check and in task/lead/revision instructions; retain the operator's
already-reviewed dispatch outer ceiling and intersect any live operator
tightening. A live task-scope tightening may also restrict, but a later
widening or removal cannot expand the dispatch scope. Record a factual
task-scope mismatch when the live object differs from its snapshot, so a
saved result does not silently imply that the dispatched scope matched the
one consulted later. Do not widen Fleet's write grant, add worker roles,
change stop classes, retries, budgets, or edit authority.

The two red path cases cover a task scope distinct from the operator limit
and the identity alias. The third red case covers the instructions. No-drift
and live operator tightening controls protect the existing behavior. Before
integration, add a whole-controller test if the engine owner finds a real
post-dispatch mutation hook; otherwise keep the evidence labeled synthetic.
