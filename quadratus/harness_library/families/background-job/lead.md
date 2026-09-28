# background-job: lead

You are adding or changing one scheduled or queued job. Done means the job is
registered in every list the runtime reads, has a consumer, is idempotent
under overlap, states what it wakes, and the gates pass. The gate runner
declares done, not you.

Inputs you must have: the job and where it lives, the trigger, the overlap
behavior, and the cost posture. Missing one means stop and return status
blocked naming it.

Procedure

1. Read the adapter's registries and the closest existing job. Match its
   registration exactly.
2. Write the job. Database and tenant work goes through the sanctioned runner
   named in the adapter; never open your own loop or session.
3. Make overlap safe: a dedupe key, a claim row, or an operation that is safe
   to repeat. Say which in the code comment.
4. Register the job in every registry: imports tuple, task_routes, cron case
   table and all list, plist. Confirm a consumer exists for the queue or
   schedule; if none does, stop.
5. For an HTTP trigger: verify the shared secret first and fail closed when
   it is unset; return JSON on every path, including errors.
6. If a step spends money, claim the ledger row before the spend.
7. State the cost posture: what the schedule wakes and how often. A new beat
   entry outside the minimal list needs an operator ruling; stop and ask.
8. Run the gates in order: scope, registry-check, posture-tests, unit-tests,
   lint. Paste the tails.

Do not

- Add a schedule that wakes the database more often than the minimal list
  allows.
- Return HTML or an empty body from an HTTP-triggered job.
- Assume a queue has a worker; prove it from the consumer map.
- Run the job against a real system to test it; use the fixture path.

Return: registration proof for every registry, the consumer, the overlap
mechanism quoted, the cost posture line, and the trigger auth line for HTTP
jobs.
