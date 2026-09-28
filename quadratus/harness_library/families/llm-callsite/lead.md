# llm-callsite: lead

You are adding or changing one call to a language model. Done means the call
names a tier, goes through the catalog's failover helper, has a refusal branch
before parsing, fits its volume, and the gates pass. The gate runner declares
done, not you.

Inputs you must have: the call site, the volume (per request, per row, per
webhook, per set, per day, on demand), the tier with one line on why it
clears the quality bar, and whether the output reaches a customer. Missing one
means stop and return status blocked naming it. The adapter's
runtime_model_policy decides which rules below apply: product (Haiku, Sonnet,
Opus through a catalog) or multi-vendor (the roster is the catalog, its seats
and vendors are permitted, the routing audit is exempt). Unset means stop.

Procedure

1. Read the catalog module and the helper function the adapter names. Read
   the closest existing call site and match its shape.
2. Declare the tier as a name. Never write a claude-* id outside the catalog.
   Multi-vendor: the roster key is the name and the roster is the catalog.
3. Send the request through the helper, or under multi-vendor through the
   provider layer named in permitted_calls. Do not pass temperature, top_p, a
   forced tool_choice, or thinking disabled. Steer with the prompt and a
   strict schema.
4. Handle refusal before parsing: the helper raises or returns a marker; take
   the deterministic path and say what the caller gets.
5. If the path fires per row, per recruit, per import, per set or per
   webhook, the tier is below the top of the adapter's ladder (haiku or
   sonnet under product). Failover goes down a tier only.
6. If the step runs inside a queue or pipeline and costs money, claim the
   ledger row before the call.
7. If the output is customer-facing prose, load the writing rules and label
   the output as model-produced with what it was derived from.
8. If you add a model id to the catalog, add its pricing row in the same
   commit.
9. Run the gates in order: scope, model-id-grep, raw-call-grep,
   catalog-guard-tests, model-routing-skill, unit-tests, lint. Paste the tails.
   Multi-vendor: quote the routing_audit_exemption in place of the skill tail.

Do not

- Call Fable from app code under a product adapter, ever. Under multi-vendor
  the roster decides seats; do not edit the roster to reach a model.
- Read content[0] without checking stop_reason and length.
- Add a retry that moves up a tier.
- Widen the change into the catalog's own logic; that is a sensitive path.

Return: the call site, tier and helper quoted; volume and why the tier fits;
the refusal branch quoted with its return; pricing row added or none needed;
the df-model-routing tail or the quoted exemption.
