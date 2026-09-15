# Isolated acceptance image

Build from this directory with `docker build -t quadratus-blind-preflight tools/acceptance`.
This recipe is Linux arm64-specific (the Grok artifact is aarch64).
The image contains no repository source, credentials or examiner inputs. Vendor
CLI versions and Node are pinned; Python/OS dependencies resolve at build time.
Before a trial, record `docker image inspect`'s immutable image ID, installed
versions and `pip freeze`. The recipe alone is not a frozen dependency set.

Pass the inspected `sha256:...` image ID to `quadratus.isolated_run.run_isolated`,
with a disposable source export and separate runtime containing only required
Quadratus source. Never mount the entire development repository: it contains
public examiner tests and collaboration history. Set `network=True` for vendor
calls, and supply an explicitly prepared private credential seed if needed.

The optional credential tree accepts only `.codex/auth.json`,
`.claude/.credentials.json`, and `.grok/auth.json`, with owner-only permissions
on its files/directories. Stage subscription authentication only; no API keys,
host settings, history or plugins. The runner copies this read-only seed into
an ephemeral HOME so the vendors can refresh credentials without changing the
host seed. The caller must remove the staged seed after use. Never include
credentials in an image layer, build context, source export, log or commit.

The initial preflight checks versions, auth method, Codex feature readouts
and a real Chromium launch without invoking a model. Model probes are
separate, unscored and individually bounded. The feature readouts do not establish native-off enforcement: model metadata
can outrank them. The additional agents.enabled=false control requires live
validation. Passing auth/config checks does not establish that native
delegation cannot occur. No scored run may start
until the remaining protocol gates in docs/collaboration/2026-09-blind-acceptance
are satisfied and the final input/runtime/private examiner hashes are frozen.

`native_probe.py` is a deliberately unscored one-call check. Invoke it only
inside `run_isolated`, with an 80-second outer watchdog, for example
`python /opt/quadratus/tools/acceptance/native_probe.py codex --output /work/probe-codex`.
Include this script explicitly in the probe runtime mount; omit it from the
application solver input. Its internal limits are one attempt, 70 seconds and
50,000 reported tokens (post-return threshold, not a prepaid cap). It preserves
private stdout/stderr, the answer, and this Codex parent's explicitly linked
raw rollouts before tmpfs cleanup. Inspect and sanitize evidence before sharing.
A host invocation is refused. Do not run another probe until the native-control
fix has been reviewed; the first live Sol probe already disproved the two-flag
guarantee, and an identical retry would not add useful evidence.
Use `--mode tools` first for the reviewed fix: it requests only tool names, uses
a read-only seat and calls no tools intentionally. Its answer is a model report,
not a definitive tool schema. Only after inspecting it, run `--mode spawn`
once to challenge enforcement. Both modes retain usage and private rollouts.

Use `--mode tools --writable-tools` when checking that an off-mode denial
preserves ordinary read/write/exec availability. It still requests no tool calls.
