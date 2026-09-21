<p align="center">
  <img src="brand/mark-large.svg" alt="" width="96" height="96">
</p>

<h1 align="center">Quadratus</h1>

<p align="center"><em>Multi-LLM Workflow — rival frontier models on one coding task</em></p>


A multi-model coding system: frontier models from competing vendors
collaborate on one coding task instead of one model working alone. It is
built for the **consumer subscriptions you already pay for** — Claude
Pro/Max, ChatGPT Plus/Pro, SuperGrok — driving each vendor's coding-agent
CLI so a run draws on subscription rate-limit windows rather than per-token
billing. Billed API keys still work, per provider, for anything you would
rather run that way.

Today's lineup is **Anthropic, OpenAI and xAI**. Google was in it until
2026-09-12; its roster rows are kept in `registry.RETIRED_ROSTER` with their
notes, so restoring it is moving three rows back and adding a ladder rung.

The primary workflow opens a **persistent local project**. The session engine
reads its source, routes sized tasks to the existing model roster, saves granted
edits into that folder, and runs checks in the same folder. The GUI opens on
this project workflow. Code discussion remains available for snippets.

```bash
# Existing project; granted edits stay here, even after the run ends.
quadratus "Fix the parser and add regression coverage" \
  --project /path/to/repository --allow-writes --check "pytest -q"

# Optional GitHub clone and new local branch; uses your existing Git credentials.
quadratus "Implement the first task" --project /path/to/new-checkout \
  --clone https://github.com/owner/repository.git --branch quadratus/task \
  --allow-writes
```

Without `--allow-writes`, the project is supplied as disposable source copies
for analysis. A nonexistent project folder is created and initialized with Git.
Files live in the selected folder; reports, raw transcripts, ledger, usage and
`changes.diff` live under `.quadratus/runs/<run-id>/`. An empty diff is reported
explicitly, and failed checks prevent a completed result. `--check` and write
grants require `--project`; they cannot accidentally test the launch directory.

`--project` selects the session engine automatically. A bare
`quadratus "<goal>"` retains the original four-phase discussion pipeline;
`--session` without a project is also text-only. `-o` exports the report, not a
source tree. No commit, push or pull request is created automatically.
See [Project workflow](docs/PROJECT_WORKFLOW.md) for contracts and validation.

## Backends: API keys or subscription CLIs

Every provider can be served by either backend, chosen per provider or
globally (`LLM_BACKEND=api|cli`, or `CLAUDE_BACKEND`, `OPENAI_BACKEND`, …):

| Provider | CLI backend (subscription, default) | API backend |
|----------|-------------------------------------|-------------|
| Claude | `claude` (Claude Pro/Max) | `ANTHROPIC_API_KEY` |
| ChatGPT | `codex` (ChatGPT Plus/Pro) | `OPENAI_API_KEY` |
| Grok | `grok` (SuperGrok / X Premium+) | `XAI_API_KEY` |

A provider whose CLI is missing reports itself unavailable rather than
quietly switching to billed transport — an unexpected invoice is a worse
failure than a clear error.

CLI specs are declarative (`quadratus/cli_providers.py`), so a vendor
renaming a flag is a one-line fix — and the operator can make that fix from
`.env` (`QUADRATUS_CLI_BINARY_<VENDOR>`, `QUADRATUS_CLI_ARGS_<VENDOR>`)
without touching Python. The current specs include recorded signed-in checks
against Claude 2.1.269, codex-cli 0.154.0 and Grok 1.0.30. Run
`quadratus --probe` to check the aliases accepted by your installed CLIs.

Project execution requires CLI transport; API providers remain available for
code discussion. Editing calls receive the persistent project only with a write
grant. Other calls receive fresh copies, and bounded editors return text patches
for the harness to apply. Copies isolate relative file writes; they are not an
operating-system sandbox against arbitrary absolute paths or malicious commands.

### Models are addressed by line, not by release

The orchestrator seats (`claude:fable`, `openai:gpt-6-astra`) carry
*floating* aliases: they name a model line, and the
vendor CLI resolves it to whatever the current release is. That is enforced
at import — a pinned alias in `ORCHESTRATOR_CHAIN` raises. The seat is
chosen once and held for a whole session, so it is the worst place in the
system to freeze an iteration: nothing would revisit the choice until
somebody noticed the run was still on last quarter's model.

`quadratus/latest.py` resolves an alias from, in order: an operator override
(`QUADRATUS_ALIAS_CLAUDE_FABLE=…`), what `quadratus --probe` last saw a CLI
accept, and the registry seed. Pinned rows ignore the cache — they name one
release deliberately.

The strongest form of this is not an alias at all. `grok:default` carries
`vendor_default=True`, which means the CLI is handed **no model flag** and
applies its own current default — xAI moves the pointer, the run follows, and
there is nothing to guess, probe, or edit. Available wherever a CLI has a
default (`grok models` marks one); an operator override still wins.

**Scope of use:** vendor terms permit driving your own subscription's CLI
for ordinary individual use, on your own machine. Routing other people's
prompts through your credential is prohibited by every vendor, so the GUI
stays on localhost. Its project controls have local filesystem and command access, so public sharing is disabled on every backend. See the
docstring in `quadratus/cli_providers.py` for the full reasoning.

## The collaboration pipeline (code discussion)

`quadratus/orchestrator.py` runs four phases:

1. **Plan** — every available model independently proposes an approach and a
   division of labour (in parallel).
2. **Consensus** — a coordinator merges the proposals into one plan with
   explicit per-model roles.
3. **Build & debate** — a lead drafts, the others adversarially review and
   refine over `ROUNDS` rounds.
4. **Synthesis** — a coordinator merges every contribution into one answer.

It degrades gracefully: one configured provider gives a strong solo answer;
a missing key or uninstalled CLI disables that provider instead of crashing
the run.

## The session engine (project workflow)

The rebuild replaces the flat debate with an orchestrated session. The
architecture, briefly:

- **A persistent orchestrator** (Claude's top tier) is the only participant
  that lives for the whole session; it names the next task and rules on
  questions. If Fable is unreachable the seat passes to GPT-6 Astra — the
  only fallback, behind a different subscription — and if that is gone too
  the run stops. Opus 5 is capable of the seat and deliberately never takes
  it: it is a brain-trust peer and half the reviewer pair, and those roles
  are worth more to the run. The substitution is recorded on the seat and
  lapses by recomputation the moment the primary is back. A
  security-classified segment yields the seat for a different reason and
  lands on the same deputy, which defers the work to Sol; because both are
  OpenAI's, the cross-vendor verification rule in `session.py` redraws the
  check to Opus.
- **A fixed brain trust** of frontier models leads individual tasks. A lead
  keeps full working memory for one task and is wiped when it closes;
  **worker bees** (each vendor's fast model, picked by errand type:
  lookup / read / visual / check / format) keep nothing. Three memory scopes, one
  per role (`memory.py`, `workers.py`).
- **Difficulty-ladder routing** (`task_kinds.py`, `registry.py`): task
  difficulty picks the model; kind-based pins (security and testing to Sol,
  review to the reviewer pair, scope and decomposition to the orchestrator)
  are the exception and each carries its evidence. The roster is a seed —
  `quadratus --probe` replaces the guesses with what your CLIs accept.
- **Task size beats model choice**: decomposition carries a hard size
  ceiling, because review quality collapses on large diffs faster than any
  gap between reviewers.
- **Review discipline** (`routing.py`, `session.py`): never one reviewer
  (the two pinned reviewers fail in opposite directions), reviewers are
  anonymous to the lead but named in the record, verification crosses
  vendor lines, and critique gets one rebuttal round then fix→verify —
  never open-ended debate.
- **Records that survive summarisation**: an append-only ledger that is
  never re-summarised (`ledger.py`), an artifact store where raw output is
  kept and summaries are indexes with `FETCH:` pointers (`artifacts.py`),
  invariants re-emitted verbatim on every render, and a cross-session
  codebase map (`codebase_map.py`).
- **Deterministic gates**: after a task's work is final the harness runs
  the project's own tests/build (`integration.py`); frontend claims are
  checked against a real browser — screenshots, console errors, failed
  requests (`browser.py`); models can also pilot a live page through a
  strict one-action-per-turn JSON protocol (`browser_pilot.py`).
- **Anti-death-spiral rules**: a verbatim retry of a failed action is
  refused, and naming the same task twice in a row stalls the run loudly.
- **Observational metering** (`usage.py`): every invocation is priced
  against API list prices, measured tokens when the CLI reports them,
  estimates (marked as such) otherwise.

Design rationale for all of this lives in `CLAUDE.md`.

## Setup

```bash
git clone https://github.com/Davisfox5/quadratus.git
cd quadratus
pip install -r requirements.txt   # or: pip install -e .

cp .env.example .env
```

Subscription transport is the default, so setup is installing and signing in
to the CLIs you have subscriptions for (commands verified on macOS,
2026-09-12):

```bash
npm install -g @anthropic-ai/claude-code     # -> claude
npm install -g @openai/codex                 # -> codex
brew install --cask grok-build               # -> grok  (x.ai/build)

claude          # then /login                  (Claude Pro/Max)
codex login     # `codex login status` to check (ChatGPT Plus/Pro)
grok login      # `grok models` lists what your account reaches

quadratus --status    # what is configured — free
quadratus --probe     # what the CLIs actually accept — spends a little budget
```

To use billed API keys instead, set `LLM_BACKEND=api` (or
`CLAUDE_BACKEND=api` for one provider) and the matching key —
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`. `.env.example`
documents every setting, including per-provider backends, CLI flag
overrides, and alias overrides.

## Usage

```bash
# After `pip install -e .`:
quadratus "Implement an LRU cache with O(1) get/put in Python, with tests"

quadratus --status                 # which providers/backends are configured,
                                   # and what each model resolves to today
quadratus --probe                  # ask the installed CLIs which aliases they
                                   # accept; caches the answers (costs a little
                                   # subscription budget)
quadratus --probe-all              # …for every model, not just the seats
quadratus "..." --rounds 2         # more refinement rounds
quadratus "..." --show-stages      # print every intermediate stage
quadratus "..." -o solution.md     # save the full run to a file
```

### The session engine

```bash
quadratus "Add rate limiting to the API, with tests" --project /path/to/repo

  --plan-gate          # review the whole task list before any window is spent
  --check 'pytest -q'  # run the project's own tests after each task's work
  --max-tasks 20       # runaway backstop, not a quality gate
  --mode solo          # adversarial (default) | collaborative | solo
  --allow-writes       # let the agents edit files (off by default)
  --state-dir DIR      # artifacts, codebase map, usage log (default .quadratus)
```

A run reports each seat and task as it moves, then prints every close-out and
the API-price counterfactual. Everything it accumulated stays in the state
directory: the artifact store holds the raw output that summaries only point
at, the codebase map is cross-session memory about the repository, and
each run's `usage.jsonl` is the cost ledger.

If the orchestrator's own window is exhausted mid-run, the seat falls to the
fallback and the run continues — availability is learned by calling, so the
first request is often what discovers it.

Web interface: `quadratus-gui` (or `python chat_gui.py`), then open
http://127.0.0.1:7860. Conversation memory, file uploads, and a per-model
contribution breakdown. Sharing is disabled while a CLI backend is active.

## Project layout

```
quadratus/
  config.py           # Settings dataclass, env loading, backend selection
  providers.py        # API providers (Claude/ChatGPT/Grok) + retries
  cli_providers.py    # Subscription CLI providers (claude/codex/grok)
  runtime.py          # Fleet: roster keys -> the CLI that answers them
  latest.py           # Floating aliases: addressing a line, not a release
  probe.py            # Asks the installed CLIs what they actually accept
  prompts.py          # Prompts for the pipeline phases
  orchestrator.py     # Four-phase collaboration pipeline (current entry)
  cli.py, gui.py      # Command-line and Gradio interfaces
  # --- session engine (in progress) ---
  session.py          # The run loop: orchestrator → lead → workers → close-out
  registry.py         # Model roster: shapes, families, capability tags
  task_kinds.py       # Routing policy: difficulty ladder, pins, gated kinds
  routing.py          # Seats, excursions, cross-family verification
  workers.py          # Worker-bee tree, escalation, failure rules, budgets
  memory.py           # Three memory scopes (session / task / none)
  ledger.py           # Append-only run record, never re-summarised
  artifacts.py        # Raw-output store behind summary pointers
  codebase_map.py     # Cross-session notes about the repo itself
  integration.py      # Deterministic post-task test/build gate
  browser.py          # Real-browser evidence (screenshots, console, network)
  browser_pilot.py    # Model-driven page piloting, one action per turn
  structured.py       # Structured-output parsing helpers
  usage.py            # Observational cost metering
tests/                # pytest suite — offline, providers mocked
```

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

CI runs ruff and the test suite on Python 3.11/3.12
(`.github/workflows/ci.yml`), installing a Chromium for the browser tests.
Tests need no network and no keys.

## License

MIT

### Repository policy preview

The installed package includes the 13 pilot family cards and their schemas from
War Room coordination commit `5cd658e`. Preview reads the selected project and
runs no models, check commands, clones, or branch changes:

```sh
quadratus --project /path/to/repo --policy-preview --path src/example.py
quadratus --project /path/to/repo --policy-preview --allow-writes \
  --path src/example.py --forbid src/protected.py
```

The Project screen has the same preview, task-path and forbidden-path controls.
Without `.quadratus/policy.json`, preview uses the built-in `pure-logic` card,
detects the test runner when possible, and marks unknown adapters as
`not_configured`. A configured policy must use the packaged `pilot-1` schema and
library. An optional digest must match the packaged library. Gate names resolve
by exact id or `gate_bindings`; an explicit absence keeps its reason in the plan.

Path rules are evaluated in document order. The first matched family is primary;
other matched families keep their checks in the same task. A required overlay
must be supported by the families on its rule. Directory and wildcard scopes
are matched conservatively, including files that do not exist yet. Narrow a
scope if it selects unrelated rules. A model tier or detected fact grants no
write permission. Sensitive paths remain blocked for writes pending an operator
ruling; read-only proposals can still be previewed.

`--path` and `--forbid` are repeatable. Operator and policy bounds combine with
per-task scopes; stricter line and overrun limits win. Conflicting declarations
stop before the lead is invoked. Post-call scope checks preserve and report any
unexpected edits. These controls do not constitute an OS filesystem sandbox.

Run records include `policy-plan.json` and each resolved task plan plus its hash
in `result.json`. Role-specific checklist packets are the separate Q6 item.
The Q5 branch is based on the pinned acceptance branch: explicit policies with
command gates stop before task dispatch until Q3's `GateSuite` is present.
Unsupported required runners and external-effect gates also stop explicitly.
Repository policies do not silently replace required checks with the legacy
single command. Projects without a policy retain their existing check behavior.
