# Scored attempt 6 — the containment fix reached the wrong half of the fleet

Run **be04e367**, frozen engine **ad8fc76**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 to 5 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run.
See [the pre-launch freeze](../scored-attempt-6-freeze.json).

## Result

**Incomplete after 18.2 seconds and two calls**, 57,183 reported tokens.
Nothing was written; the saved source is byte-identical to the frozen input.
Private **0 of 9** (every case HTTP 405: the endpoint does not exist), public
examiner **0 of 13**, unchanged baseline **100 tests passing** offline — all
determined by the empty diff.

| Call | Seat | Outcome | Tokens | Seconds |
| --- | --- | --- | ---: | ---: |
| 1 | Claude Fable | vendor window limit | 0 | 1.5 |
| 2 | GPT-6 Astra | ok, asked the operator a question | 57,183 | 16.6 |

It stopped at the same `bwrap: No permissions to create a new namespace`
failure as attempt 5, at the same point, in the same way — with the fix for
that failure in place and asserted.

## Why the fix did not fire

`QUADRATUS_CONTAINED=1` substitutes `--sandbox danger-full-access` for Codex's
own sandbox, because inside our container that sandbox cannot start and the
container is already the boundary. Attempt 6 carried that change. The launcher
set the flag, `contained()` returned true, and the substitution was consulted
**only on the restricted-seat branch of the argument builder**.

Senior seats are never restricted. That is a deliberate design rule — bounding
an orchestrator, lead, reviewer or consultant is not a saving but a different
job — and it means a substitution that skips the agentic branch cannot reach
any of them. Every seat that had ever gone blind was a senior seat: Astra as
orchestrator in attempts 5 and 6, Sol as lead in attempt 3. The fix covered the
one class of seat that had never had the problem.

Demonstrated offline against the frozen engine, with containment asserted:

| Seat | Sandbox it was given |
| --- | --- |
| Orchestrator or lead, reading a fresh source copy | `read-only` |
| Lead holding the project, writes granted | `workspace-write` |
| Restricted worker | `danger-full-access` |

Scoping the change narrowly read as the cautious choice and was the opposite:
it produced a run that looked configured and was not.

## What the container actually does with each mode

Measured inside the acceptance image, model-free, reading one real file from
the mounted work tree:

| `codex sandbox -c sandbox_mode=` | Exit | Result |
| --- | ---: | --- |
| `read-only` | 1 | `bwrap: No permissions to create a new namespace` |
| `workspace-write` | 1 | `bwrap: No permissions to create a new namespace` |
| `danger-full-access` | 0 | returned the file's contents |

Both permission modes are the same bubblewrap mechanism, and `-s` accepts only
these three values, so inside this container the choice is the sandbox or the
permission flag — never both.

This also corrects something recorded before the run. The engine and the
preflight each carried a note saying no model-free check could settle which
mode `codex exec` would get, because the `sandbox` subcommand sandboxes
whatever it is handed. That was wrong: the subcommand honours
`-c sandbox_mode=` like any other entry point, and the table above is the
check that was said to be impossible.

## Standing the sandbox down does not hand the permission axis away

The vendor flag was not what held it. A call without a write grant runs in a
fresh source copy that is deleted when the call returns
(`runtime.Fleet._invoke`), and a restricted editing seat returns a text patch
the harness applies. What is given up is narrower, and is stated rather than
implied: inside this container a Codex seat can now write into its own
disposable copy, and a seat that already holds a write grant can write to the
tmpfs HOME as well as to `/work`. Both vanish with the container, and a seat
that could read that HOME could already read it under `read-only`.

## Repairs made after this run

- The substitution applies to **every rank of Codex seat**, bounded and agentic
  alike. `CLISpec.contained_restricted_args` is renamed
  `contained_sandbox_args`, since it no longer covers only one branch.
- The preflight now exercises each vendor's sandbox **in the mode this run
  would really use**, and against a real file from the mounted tree rather than
  `true` — a command that touches nothing can pass without answering the
  question. A failure is now always a blocker; there is no longer a "recorded,
  not fatal" reading, because the mode under test is the mode that matters.

Verified against the real container afterwards: contained, the preflight exits
0 and reports the OpenAI seat reading `/work/TASK.md` under
`danger-full-access`; with containment unasserted it exits 1 and names
`bwrap: No permissions to create a new namespace` as the cause.

Engine suite **1011 passed, 1 skipped** (gradio absent) with Docker and
installed-CLI checks enabled; Ruff and diff-check clean. No model calls.

## Still unexercised

The worker tool-fit check (`a0f16ec`) has never run. It has now been present
and unreached for three attempts, because no run has got as far as a lead
commissioning an errand.

## Reproduce

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` (`source/` prefix)
and verify `source-after-sha256.json`. Raw stdout and vendor sessions stay
private outside every repository; `vendor-usage-extract.json` names each call's
private file and its SHA-256. Container and credential seed were removed.
