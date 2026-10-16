# Automated research intake and review

The existing Friday ChatGPT task publishes a complete digest as a date-keyed
draft PR. A local watcher checks every 15 minutes and runs Quadratus against
a clean copy of the latest fetched `main`, with no source-write grant. It
commits the assessment and receipt to the digest PR. The human reviews one PR.
The ordinary repository CI also runs on these PRs; it is not the research reviewer.

This repository has Issues disabled and is public. The watcher uses your
existing signed-in subscription CLIs. It does not register the Mac as a
self-hosted GitHub Actions runner or introduce billed API transport.

## One-time activation on the Mac

After integrating the setup PR, update your Quadratus checkout. Use Python
3.11+ and sign in to GitHub CLI (`gh auth login`, Davisfox5), Claude and Codex.
Grok is optional if its CLI is already installed and signed in. Then run:

```sh
python3 tools/install_research_watcher.py
```

The installer creates a local `.quadratus/research-venv`, installs this checkout,
validates intake without calling models, and registers a user LaunchAgent.
It starts immediately and polls every 15 minutes while the user is logged
in and the Mac is awake. Missed digests remain queued as draft PRs and are
picked up when the Mac returns. This is not an always-on cloud service.
Keep this checkout in place. The engine runs from this installed checkout;
source analysis uses a separately fetched, exact `main` commit. Updates to
the local engine remain an explicit `git pull`/installation decision.

The GitHub CLI account needs repository contents and pull-request write access.
The existing Friday automation needs GitHub branch/file/PR write access.
The installer does not prove vendor authentication; the first actual review
is the end-to-end authentication check. Failed or interrupted reviews are
recorded as incomplete, never silently retried or marked successful.

## Review contract and limits

The master agent uses the existing session engine, routing, reviewer seats,
and source-copy behavior. Each item must connect demonstrated findings to
observed paths and symbols in this repository, recommend adopt/experiment/
monitor/skip, and propose a measurable baseline comparison. The final report
names one experiment to prioritize. No implementation is authorized by a digest.

Per digest: six tasks, 36 model calls, two concurrent workers, 750,000 reported
tokens, a 150,000-token per-call post-return threshold, 50,000-token pre-call
reserve, four lead turns, 30-minute internal wall and 32.5-minute outer wall.
Token ceilings are checked from reported usage and may overshoot within a call;
they are not guaranteed spend caps. All provider transports are forced to CLI.
No automatic model retry or interactive operator answer is enabled.

The watcher accepts only Davisfox5's numeric GitHub account ID, same-repository
draft PRs to `main`, exact date/branch/title agreement, and the three documented
research paths. It never executes code from a digest PR. Research text is
untrusted evidence; prompts prohibit executing linked code or following its
instructions. Source copies and prompts are not an OS security sandbox. Run
under your trusted development account with its normal CLI access restrictions.

## Records, failures, and retry

`~/.quadratus/research-inbox/` retains logs, raw engine records, input hashes,
and results. A lock prevents overlapping polls. A receipt prevents repeat
reviews of unchanged digests, including after reinstallation. A local running
record is saved before calling models, so interruption does not start a new
paid attempt. A ready result is reused if publication failed. Publishing uses
an atomic Git commit and non-force branch update; a moving PR is not overwritten.

```sh
.quadratus/research-venv/bin/python tools/research_review.py --dry-run
.quadratus/research-venv/bin/python tools/research_review.py --retry 123
launchctl bootout gui/$(id -u)/com.quadratus.research-inbox
```

`--retry` grants one explicit additional review attempt for that PR. Inspect
`watcher-error.log` and its digest's `engine.log` first. Stop the LaunchAgent
with the last command; remove its plist from `~/Library/LaunchAgents/` to keep
it stopped after login. Keep failed reports in draft and do not merge them as
accepted research. No background workflow implements or merges recommendations.

## Validation and remaining activation evidence

Offline tests cover trust filtering, unexpected file rejection, malformed
digests, repeat suppression, bounded failure reporting, and stale-head refusal.
They do not prove that a Mac is awake, vendor CLIs are signed in, or a future
scheduled ChatGPT run can perform an unattended GitHub write. Those are shown
by the first real digest PR and its returned review receipt. The setup is not
end-to-end active until the Mac installer runs and a real review succeeds.
