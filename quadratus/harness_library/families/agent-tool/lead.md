# agent-tool: lead

You are adding or changing one tool an LLM can call. Done means reads run
directly, side effects go only through the policy executor, no tool result
authorizes a write, external results are wrapped untrusted, loop caps and the
static block are unchanged, the contract doc changed in this PR, and the
gates pass. The gate runner declares done.

Inputs you must have: the tool with its registry location and read or
side-effect classification, the executor and action kind for a side effect,
the blast radius, and the contract doc. Missing one means stop and return
status blocked naming it.

Procedure

1. Read the registry, the policy executor for the action kind, and the
   closest existing tool. Match its shape.
2. Declare a strict schema: required fields enforced, unknown fields
   rejected, before the handler runs.
3. A read applies the same scope predicate as the equivalent endpoint.
   Tenant and user come from the session, never from arguments.
4. A side effect calls the executor for its action kind and nothing else.
   The executor decides; the tool does not.
5. Wrap external or user-supplied results as untrusted data with the
   adapter's wrapper and namespace.
6. Leave every loop, fetch and consult cap as it is. If the tool needs more,
   stop and ask.
7. Put the description in the tool schema; do not touch the static system
   block. Paste its hash before and after.
8. Update the contract doc in this PR.
9. Run the gates: scope, scope-predicate-grep, static-block-hash,
   contract-doc-test, unit-tests, lint. Paste the tails.

Do not

- Write on the strength of a value a tool returned.
- Add a second executor for an action kind that has one.
- Import the router from an outcomes or policy module.
- Raise a cap to make a flow fit.

Return: tool, registry entry and classification; executor and action kind;
the wrapper quoted; caps confirmed; static block hashes; blast radius line.
