# Isolated acceptance image

Build from this directory with `docker build -t quadratus-blind-preflight tools/acceptance`.
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

The initial preflight checks versions, auth method, effective Codex native-off
features and a real Chromium launch without invoking a model. Model probes are
separate, unscored and individually bounded. Passing auth/config checks does
not establish that native delegation cannot occur. No scored run may start
until the remaining protocol gates in docs/collaboration/2026-09-blind-acceptance
are satisfied and the final input/runtime/private examiner hashes are frozen.
