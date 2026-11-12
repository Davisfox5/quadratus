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
  lookup / read / visual / check / code / format / draft) keep nothing. Three
  memory scopes, one per role (`memory.py`, `workers.py`).
- **Difficulty-ladder routing** (`task_kinds.py`, `registry.py`): task
  difficulty picks the model; kind-based pins (security and testing to Sol,
  review to the reviewer pair, scope and decomposition to the orchestrator)
  are the exception and each carries its evidence. The roster is a seed —
  `quadratus --probe` replaces the guesses with what your CLIs accept.
- **Task size beats model choice**: decomposition carries a size guide
  (~100 lines), because review quality collapses on large diffs faster than
  any gap between reviewers. The guide is advisory: the harness measures
  every change and shows the size to the reviewers, who judge whether it
  was necessary; it stops nothing for size alone.
- **Review discipline** (`routing.py`, `session.py`): difficulty sets how
  many peers review a draft. A complex task draws every other brain-trust
  member, a standard task one reviewer, and simple and rote tasks none. Two
  kinds of work add one: a `review` task always draws its counterpart reviewer (the
  two pinned reviewers fail in opposite directions), and a UI task draws a
  reviewer from another vendor. Reviewers are anonymous to the lead but
  named in the record, verification crosses vendor lines, and critique gets
  one rebuttal round then fix→verify — never open-ended debate.
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

Quadratus needs Python 3.11 or newer. Install the package itself into a
virtual environment; that is what puts the `quadratus` and `quadratus-gui`
commands on your PATH:

```bash
git clone https://github.com/Davisfox5/quadratus.git
cd quadratus
python3 -m venv .venv
source .venv/bin/activate
pip install -e .                  # installs the package and both commands

cp .env.example .env
```

`pip install -r requirements.txt` installs the dependencies only, not the
commands. It is for working on the source; run it as `python -m
quadratus.cli` and `python -m quadratus.gui` in that case.

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
documents the transport settings: per-provider backends, API keys, CLI flag
overrides, alias overrides and request tuning.

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
  --mode collaborative # adversarial (default, one seat per vendor) |
                       # collaborative (two vendors) | solo (one peer;
                       # refused while the requirements ledger is on)
  --allow-writes       # let the agents edit files (off by default)
  --state-dir DIR      # artifacts, codebase map, usage log (default .quadratus)
  --tasks FILE         # run your own JSON list of task texts, in order, with
                       # no planner call; the goal is recorded, not judged
  --decider jev        # ask TypeSafe AI's Jev for a task kind or difficulty
                       # the orchestrator left unlabelled (default: rule)
```

`--tasks` reads each text the way an orchestrator reply is read (KIND, SCOPE,
TIER and COVERS lines, then the description) and checks the whole list before
any call. `--decider jev` is a billed API call outside your subscriptions: it
needs `pip install 'quadratus[decisions]'` and `TYPESAFE_API_KEY`, never
overrides a label the orchestrator stated, and records every answer on the
task. See [Decision models](docs/decisions-api.md).

The remaining options (`quadratus --help` has the full text):

| Option | What it does |
| --- | --- |
| `--repo DIR` | another name for `--project` |
| `--extra-check CMD` | a further required check beside `--check`; repeatable, and it must name its files |
| `--capture-profile JSON` | how to start the app, so the harness captures UI renders itself |
| `--readiness JSON` | probes run once before any model call; a failing probe stops the run |
| `--survey-recovery N` | continue through failures, spending at most N recovery tasks, and report them apart from acceptance |
| `--direct-tier` | let a small, local task run with no collaborator review; checks are unchanged, and the harness writes the close-out on every tier |
| `--decider-labels all` | with `--decider jev`: the decider routes every planned task (default `unstated`: only labels left out) |
| `--rulings FILE` | answers you give in advance to questions the planner may ask |
| `--repair-test PATH` | an existing test file this run may repair; repeatable |
| `--test-changes FILE` | your own exact test replacements or retirements, as JSON |
| `--resume RUN` | resume an interrupted run of the same job (a folder under the state directory's `runs`) as its next episode; refused before any call when the run is sealed, already resumed, or the source, goal, policy, limits, settings, gates or dependency trees differ |
| `--neutral-preferences` | run without your personal CLI plugins, hooks and rules where each CLI allows it |
| `--security-verdict-json` | require the strict JSON security verdict |
| `--max-calls N`, `--max-reported-tokens N`, `--wall-seconds N` | bound a `--project` run: model calls in total, reported tokens in total, seconds. Give all three or none |
| `--max-concurrent-workers N`, `--reserve-tokens-per-call N`, `--max-tokens-per-call N` | with the three above: worker errands at once (default 2), tokens that must remain before a call starts (default 0), and a stop after one call reports more than N (default none) |
| `--lead-max-turns N` | round limit for each lead or reviewer call; outranks the cap derived from `--max-tokens-per-call`; also works with `--session` |
| `--recent-entries N`, `--max-fix-cycles N`, `--max-consults N` | ledger entries shown in each prompt (default all), lead revisions per task (default 2), `CONSULT` questions per task (default 2) |
| `--max-parallel-tasks N`, `--no-spread-leads`, `--no-design-cross-check` | tasks at once (default 3; needs `--allow-writes`); keep each task on the lead the ladder names; drop the design reviewer from another vendor |
| `--compete complex` | draft each complex task twice and merge the better draft (default `off`; see Competing drafts) |
| `--baseline MODEL`, `--fix-rounds N` | run one model alone on the goal (see Measuring against one model alone) |
| `--trust-project-policy SHA256`, `--allow-project-agent-config`, `--no-declared-checks`, `--accept-watched-paths` | trust choices for one project (see Security model) |
| `--accept-unrecorded-parent` | with `--resume`: resume a run that left no record of its own (no readable `result.json`, as after a killed process, or one from before records named their committed state); otherwise refused. Recorded in `result.json` `trust` for every later episode, and `tools/truth_check.py` reports them unverified. Same as `QUADRATUS_ACCEPT_UNRECORDED_PARENT=1` |
| `--allow-host NAME`, `--allow-project DIR` | with `--monitor --serve`: a host name the page also answers to, and a folder `?project=` or `?series=` may name; repeatable |
| `--series DIR`, `--history N` | with `--monitor`: a Stage B series to read, and how many recent runs to list (default 10) |
| `--calls` | with `--monitor`: also print every call of the run under the by-role summary; refused with `--serve`, whose page lists every call already |
| `--claude-model`, `--openai-model`, `--grok-model` | model IDs for the code discussion pipeline only; the session engine refuses them |
| `-v`, `--verbose` | debug logging |

A flag the chosen run would ignore is an error, not a no-op: the engine flags
need `--session` or `--project`, `--check`, `--allow-writes`, the run limits
and the trust flags need `--project`, and `--serve`, `--series` and
`--history` need `--monitor`. A project run exits 0 only when it completed. An
interrupted run prints where its record is and exits 130.

With run limits, the allowance is printed before the first call and saved as
`run-limits.json`. A call still running keeps its reserve set aside, so calls
that overlap each need their own. A worker errand the reserve cannot cover
fails only that errand; a refused reserve for a lead, orchestrator or review
call stops the run.

A run reports each seat and task as it moves, then prints every close-out and
the API-price counterfactual. Everything it accumulated stays in the state
directory: the artifact store holds the raw output that summaries only point
at, the codebase map is cross-session memory about the repository, and
each run's `usage.jsonl` is the cost ledger.

If the orchestrator's own window is exhausted mid-run, the seat falls to the
fallback and the run continues — availability is learned by calling, so the
first request is often what discovers it.

To see what a run is doing without touching it:

```bash
quadratus --monitor --project /path/to/repo             # one screen of status, then exit
quadratus --monitor --project /path/to/repo --serve     # the same status as a web page
                                                        # on 127.0.0.1:7861 that reloads itself
```

`--monitor` only reads the run folder: it starts nothing, writes nothing and
calls no model. `--serve PORT` picks another port.

Web interface: `quadratus-gui` (or `python chat_gui.py`) prints an address of
the form `http://127.0.0.1:7860/?token=<random>` and opens it in your browser;
`--no-browser` only prints it. The token is new on every launch, and the bare
address answers 403 until the printed one has been opened once in that
browser. Choose another port with `quadratus-gui --port 8000` or
`QUADRATUS_GUI_PORT=8000`; a busy port is reported with the command to use
instead. The Project tab's "UI capture and further checks" section takes the
same capture profile, further required checks and readiness probes as the
CLI's `--capture-profile`, `--extra-check` and `--readiness`. Its "Run limits"
section caps one run: model calls, total reported tokens, time, parallel
workers and extra recovery tasks after failures, with a preset that fills the
fields. The run shows the effective numbers before any call and saves them as
`run-limits.json` beside its report. Token limits count what the CLIs report
after each call returns, so they are not a hard ceiling. A per-call threshold
also sets a lead turn cap unless you set one yourself; the summary names it. Left off, a
run has no shared allowance, as before. A "Contests" choice stands for
`--compete`, and a collapsed "Trust" section holds the four trust choices
under the names of their flags; opening a project clears it. The
Collaboration list leaves out solo while the requirements ledger is on. The
Code discussion tab keeps
conversation memory, takes a file upload and shows what each model
contributed. Public sharing is refused on every backend.

### Measuring against one model alone

Whether several models beat one is a measurement, not a premise.

```bash
quadratus "Fix the parser" --project /path/to/repo --baseline claude:opus \
  --check "pytest -q"
python -I tools/compare_runs.py RUN_DIR RUN_DIR [--grade FILE ...] [--json]
```

`--baseline MODEL` runs one unrestricted roster model alone on the same
project, goal, checks and limits as a project run, under the same lock, run
folder, trust checks, path bounds, dependency watch and budget. It edits your
project files with or without `--allow-writes`. The model gets one call with
the goal; the harness then runs your checks, and a failure the application can
repair buys one more call (`--fix-rounds N`, default 1). There is no
orchestrator, reviewer, worker or close-out call. Nothing is judged: the
record says `mode: "baseline"`, `completed: false` and
`completion: "not judged"`. The exit status is 0 when it ran to its end and
its last check passed, 1 when it stopped, and 2 when its last check failed or
no check ran. Beside the goal it reads `--check`, `--extra-check`,
`--readiness`, `--path`, `--forbid`, `--state-dir`, `--clone`, `--branch`,
the run limits, `--lead-max-turns`, `--neutral-preferences`, `-o` and the
trust flags; any other engine flag is refused by name.

`tools/compare_runs.py` reads two or more run folders without importing the
engine. Per run it prints the mode, models, calls, reported tokens, seconds,
stop reason, whether source changed, the final check with its executed, failed
and skipped counts, the verdict of `tools/truth_check.py`, the requirements
audited met and, with a grade file (`{"passed": n, "total": m}`), the tokens
spent per requirement a grader passed. Anything a record does not show prints
as `unknown`.

### Competing drafts

`--compete complex` has each task routed at complex difficulty drafted twice,
by two models from different vendors that cannot see each other's work, each
in a private copy of your project. The harness measures both drafts (did it
finish, did it stay inside the task's scope, does it pass your checks) and
merges the better one. On a tie, or when one draft changed nothing, a model
from a third vendor is asked once to pick between the two diffs, when the
brain trust has one. Today it has two vendors and both are drafting, so no
judge is asked: the record says so, and the tie goes to the smaller diff, then
to the routed lead. The task then goes through
the usual review and checks. A contest costs about one extra draft, needs
`--project` and `--allow-writes`, and is off by default. The drafts are made
in copies without installed dependencies (`node_modules`, a virtualenv), so a
draft cannot run checks that need them; the harness runs your checks on each
copy itself. The routed lead's own refusal still stops the run.

Every run that draws a review appends to `.quadratus/scoreboard.jsonl` in
your project: which model won each contest and why, and what each reviewer
raised. Read it with `python tools/scoreboard.py <project folder>`. It is a
record for you; Quadratus never chooses a model from it.

### What is recorded, and what leaves the machine

A run's records stay in its state directory (`.quadratus` inside the project
by default): each run's report, ledger, raw model replies, vendor
transcripts, usage and diff under `runs/<run-id>/`, plus the artifact store,
the codebase map and the scoreboard. Those files hold your goal, prompts and
source excerpts, so they are kept private: `.quadratus`, `runs/` and each run
folder are owner-only from the moment they are made, and so are the records
in them. `.quadratus` carries its own `.gitignore` (everything but
`policy.json`), so `git add -A` does not commit run records; your project's
own `.gitignore` is never changed. A `--state-dir` folder that already
exists keeps its mode and gets no ignore file. Records are written whole: an
interrupted run leaves the old file or the new one, never half of one.

`result.json` also says what you chose: `selected_limits`, `session_settings`
when a session setting was changed, `declared_checks`, `trust` (policy
approved, agent configuration allowed, watched paths accepted),
`child_environment` (the names withheld, never values), `project_watch`,
`contests` and `reviewers`. `trace.jsonl` is the trace meant for sharing: tool
names, outcomes, exit codes, hashes and paths. Search patterns, command text
and reasoning stay in `native-private/`.

What leaves the machine is what you send:

- Each model call sends its prompt, and the source that call is given, to the
  vendor whose CLI (or API key) answers it.
- `--decider jev` sends up to 8,000 characters of each task description it
  routes to TypeSafe AI's API, or to the gateway that `TYPESAFE_BASE_URL` or
  a gateway key names. Task text can carry file paths, requirement text and
  code. The run names the destination in its first progress lines.
- `--monitor --serve` listens on 127.0.0.1 only and has no flag to change
  that. The page cannot start, stop or change anything; it shows the project
  path, run id, task, seat, token counts, last event, recent runs, stop
  reason and report path. It has no sign-in. It answers only requests
  addressed to this machine by a loopback name, plus any host name you admit
  with `QUADRATUS_MONITOR_ALLOW_HOST` (how a Tailscale Serve or tunnel name
  gets in), and it shows only the project and series it was started with,
  plus folders you list in `QUADRATUS_MONITOR_ALLOW_PROJECT`. Everyone you
  admit on another device sees that much.

## Security model

Quadratus runs on your machine as you, and a vendor CLI is an agent with a
shell. The harness narrows what a run can reach; it is not an
operating-system sandbox. What it trusts, what it does not, and the setting
that changes each:

- **The project folder is not trusted configuration.** A clone can ship
  files and an editing call can write them. The built-in write denials
  (`.git/**`, `.env`, `.env.*`, `.quadratus/**`) always apply, and a
  project's own `.quadratus/policy.json` can add denials but never remove
  one. A policy that grants something (a command check, external effects, an
  instruction file kept out of model copies) is refused before any model
  call until you approve that exact file by its sha256, which the refusal
  prints: `--trust-project-policy SHA256` or
  `QUADRATUS_TRUST_PROJECT_POLICY`. `--no-declared-checks` runs only your
  own `--check` and `--extra-check`, and nothing the project declares.
- **Vendor agent configuration in the project is refused.** A project that
  carries `.claude/settings.json`, `.claude/settings.local.json`,
  `.mcp.json`, or anything under `.codex/` or `.grok/` does not start,
  because a headless CLI started there can run its hooks and MCP servers as
  you. `--allow-project-agent-config` (or
  `QUADRATUS_ALLOW_PROJECT_AGENT_CONFIG=1`) allows exactly the files present
  at the start. Those files, `.env` files and credential files (`.npmrc`,
  `.pypirc`, `.netrc`, `.git-credentials`, `id_rsa` and its siblings,
  `.aws/credentials`, `.docker/config.json`) are never copied into a copy a
  model reads.
- **What the diff does not read is watched.** Around every editing call the
  harness lists git hooks and config, env files, credential files, agent
  configuration, the policy file, the codebase map, the scoreboard and
  every symlink. A change to one fails the task and names the path; nothing
  is deleted. While the change is in place the run stops, and the next run
  on that project is refused before any model call until you remove or
  restore the path, or accept it with `--accept-watched-paths`
  (`QUADRATUS_ACCEPT_WATCHED_PATHS=1`). An acceptance is written to that
  run's record and later runs do not ask about the same path again. Every
  run that starts while the path is still in place says so in its own
  record, and `tools/truth_check.py` reports each of them unverified. The
  watch also walks the folders no listing reads (tool caches, the state
  folder, the top of `.git`): a file a test runner loads by position there
  (a `conftest.py`, any `.py` file, anything under `__mocks__`, a `.pth`
  file, a runner config) is a watched change too. A change in build output
  (`dist`, `build`, `.tox`, `.nox`) is recorded and shown to reviewers, not
  failed, and the truth check reports such a run unverified, whatever
  kind of file changed. The exceptions fail the task like any watched path: in
  `dist` and `build`, a `conftest.py`, `sitecustomize.py`,
  `usercustomize.py`, a `.pth` file or anything under `__mocks__`; in
  `.tox` and `.nox`, anything under `__mocks__`. A build that really emits
  one of those will trip this. What the watch still does not see: a
  `conftest.py` or `.pth` file written into a `.tox` or `.nox` environment
  (an observation only), a test file of another language planted in a tool
  cache, and anything outside the project. A finding lives in the run's
  record inside the project; a run whose own record folder is removed
  makes it again and stops as `RunRecordLost`.
  The watch brackets editing calls only, and nothing outside the project is
  watched. Its bounds are `QUADRATUS_WATCH_MAX_ENTRIES`,
  `QUADRATUS_WATCH_MAX_BYTES` and `QUADRATUS_WATCH_SECONDS`.
- **Child processes do not inherit credentials.** Vendor CLI calls, checks,
  preview servers, captures and readiness probes get your environment
  without names that read as credentials (a `KEY`, `TOKEN`, `SECRET`,
  `PASSWORD` or `AUTH` part; the `AWS_`, `GITHUB_`, `NPM_`, `DOCKER_` and
  similar families; `SSH_AUTH_SOCK`, `KUBECONFIG`) and without vendor API
  keys, base URLs and provider switches. `quadratus --status` and the start
  of every run name what is withheld. If a check needs one (`DOCKER_HOST`,
  `NODE_AUTH_TOKEN`, `GITHUB_TOKEN`), list it in
  `QUADRATUS_ENV_PASS=NAME,NAME`; that hands it to the vendor CLIs too. A
  vendor CLI is never given a billed API key or another vendor's sign-in,
  even when named. Only names are read: a secret inside a value
  (`DATABASE_URL`) is not seen, and files under your home folder stay
  readable. The harness's own git and Docker commands still inherit
  everything.
- **Working copies are private.** Source copies, prompt files and provider
  scratch live under `~/.cache/quadratus/scratch` (or
  `QUADRATUS_SCRATCH_DIR`): a real folder only you can read, outside the
  project. A run removes its own leftovers older than 24 hours when it
  starts, and only in a folder the harness itself made and marked. A
  scratch folder you created yourself is never swept; put an empty file
  named `.quadratus-scratch` in it to allow that.
- **The GUI answers only to this machine and to its own page.** It checks
  the Host header (127.0.0.1, localhost or [::1] on its own port; no setting
  changes that), refuses requests a browser marks as coming from another
  site, and needs the per-launch token in the address it prints.
  `QUADRATUS_GUI_NO_TOKEN=1` turns only the token off. Public sharing is
  refused on every backend. On a shared machine, a server another user runs
  on a different loopback port can read the GUI's cookie if you open a page
  from it, because browsers do not separate cookies by port.
- **The monitor page is read-only and answers by name.** It listens on
  loopback, answers only to 127.0.0.1 and localhost plus the host names you
  admit (`--allow-host NAME` or `QUADRATUS_MONITOR_ALLOW_HOST`), and reads
  only the project and series it was started with plus the folders you admit
  (`--allow-project DIR` or `QUADRATUS_MONITOR_ALLOW_PROJECT`). Any other
  path gets one fixed refusal.
- **A UI capture is held to the preview it started.** Requests are pinned to
  the preview's origin, shared workers are off, and a listener on the
  preview's port that is not the preview's stops the capture. Two optional
  booleans in the capture profile give that up on purpose:
  `"shared_workers": true` and `"outside_listener": true` (a preview served
  by another process, such as a container's port forwarder).

### Not yet probed against the real CLIs

Flag knowledge here is a hypothesis until a signed-in binary confirms it, and
none of these could be run where the changes were made. Each wants one
probe in an empty scratch folder:

- **Keeping a project's MCP servers and hooks out of a claude call**
  (`--strict-mcp-config`, `--setting-sources`; no flag was changed, and the
  refusal above stands in). Write `.mcp.json` naming a server whose command
  is `sh -c 'touch MCP_STARTED; cat'`, run
  `claude -p 'say ok' --output-format json`, then again with
  `--strict-mcp-config`: the marker should appear only the first time.
  Write `.claude/settings.json` with a SessionStart hook `touch HOOK_RAN` and
  run plain, with `--setting-sources user`, and with
  `--setting-sources project,local`.
- **The same for codex and grok.** `.codex/config.toml` with
  `notify = ["sh","-c","touch NOTIFY_RAN"]`, then
  `codex exec --skip-git-repo-check 'say ok'`; a `.grok/` settings file with
  a hook, then `grok -p 'say ok' --output-format json`.
- **Whether a vendor CLI writes its own project configuration during an
  editing call.** If one does, every editing call by that vendor fails its
  task. In a scratch git repository run one headless editing call per CLI
  and compare `ls -la` and `ls .claude .grok .codex` before and after.
- **Keeping `/tmp` out of codex's writable roots on an editing call.**
  `codex exec --sandbox workspace-write -c
  sandbox_workspace_write.exclude_slash_tmp=true -c
  sandbox_workspace_write.exclude_tmpdir_env_var=true --skip-git-repo-check`
  with a prompt that runs `touch /tmp/q-probe` and `touch ./q-probe`: the
  first should be denied and the second written.
- **Moving prompts and the worker bridge token out of argv.**
  `claude -p --system-prompt-file <0600 file> --output-format json`, and
  `claude -p --mcp-config <path to a 0600 JSON file> --strict-mcp-config`.
- **Resuming the orchestrator's vendor session across re-asks**, which is
  what would shrink its prompt. Call 1 gives the model a nonce and records
  the session id; call 2 resumes that id with a prompt that lacks the nonce
  and asks for it back.
- **Sign-in under the reduced child environment.** Run `quadratus --probe`
  once: claude, codex and grok should each still answer.
- **One real `--compete complex` run and one real `--baseline` run**, to
  confirm the judge call's arguments and turn cap on each CLI.

## Project layout

```
quadratus/
  config.py           # Settings dataclass, env loading, backend selection
  providers.py        # API providers (Claude/ChatGPT/Grok), retries, private scratch
  cli_providers.py    # Subscription CLI providers (claude/codex/grok)
  child_env.py        # What a child process inherits: no credentials it does not need
  runtime.py          # Fleet: roster keys -> the CLI that answers them
  latest.py           # Floating aliases: addressing a line, not a release
  probe.py            # Asks the installed CLIs what they actually accept
  prompts.py          # Prompts for the pipeline phases
  orchestrator.py     # Four-phase collaboration pipeline (a bare "<goal>")
  cli.py, gui.py      # Command-line and Gradio interfaces
  # --- session engine (what --project and --session run) ---
  session.py          # The run loop: orchestrator → lead → workers → close-out
  session_types.py    # Stops a run raises, TaskSpec, the questions asked of a task
  reply_parsers.py    # Readers of directive lines, captures and fixtures
  session_prompts.py  # Standing prompt text and the functions that format it
  check_facts.py      # Readers of recorded check receipts and test-file text
  session_checks.py, session_design.py, session_audit.py,
  session_seat_prompts.py, session_security.py, session_packets.py,
  session_dispatch.py, session_evidence.py, session_obligations.py,
  session_done.py, session_recovery.py, session_admission.py,
  session_findings.py, session_closeout.py
                      # Mixins Session inherits: checks, design, audit, seat prompts,
                      # security, role packets, dispatch, evidence, obligations, DONE
                      # declarations, recovery, admission, findings, close-outs
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
  verdicts.py         # The one reader of review, recheck, verifier and audit verdicts
  contest.py          # Competing blind drafts and the scoreboard records
  baseline.py         # One model alone, under a project run's bounds
  project_watch.py    # What an editing call changed outside the measured source
  safe_io.py          # Private, whole-file writes for run records
  local_http.py       # Host, origin and token checks for the GUI and the monitor
tools/
  compare_runs.py     # Two or more run records side by side
  scoreboard.py       # Reads .quadratus/scoreboard.jsonl
  truth_check.py      # Judges a run's success claim from its record alone
tests/                # pytest suite — offline, providers mocked
```

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

CI runs ruff and the test suite on Python 3.11, 3.12 and 3.13
(`.github/workflows/ci.yml`), installing a Chromium for the browser tests,
then builds the wheel, installs it outside the source tree and starts the
installed commands. Tests need no network and no keys.

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

`--path` and `--forbid` are repeatable. Operator and policy path bounds combine
with per-task scopes. Line figures are size estimates: a task's own is kept as
declared, the policy's is the default for a task that declares none, and the
measured size is reported to the reviewers rather than enforced. Conflicting
declarations stop before the lead is invoked. Post-call scope checks preserve
and report any unexpected edits. These controls do not constitute an OS
filesystem sandbox.

Run records include `policy-plan.json` and each resolved task plan plus its hash
in `result.json`. Role-specific checklist packets are the separate Q6 item.
The Q5 branch includes the merged Q3 base. Explicit policy commands run through
`GateSuite`; operator checks are retained. Conflicting gate definitions,
unsupported required runners and external-effect gates stop explicitly.
Projects without a policy retain their existing check behavior.

A lead can retry a failed worker errand with one read-only sibling helper:
`WORKER {"errand":"read","instruction":"revised request","retry_of":"read-1","helper":{"errand":"check","instruction":"independent check"}}`.
Both siblings run through the existing worker pool at depth one and report to
the lead. Workers cannot commission through either the single or batch entry
point. A helper cannot receive a write grant.

Worker requests may opt into `steps` (default 1, configured ceiling 3) and
`token_limit` (configured ceiling 50,000 reported tokens). A value outside the
configured ceiling, or an unknown `needs` label, is refused before any call
and counts as a failed errand; a field of the wrong type fails the task.
`CONTINUE:` requests
another step on the same errand, with the prior result as context. Each provider
attempt, including a retry, consumes the worker step allowance and the task's
worker allowance, as well as any shared run limits. Unknown token usage stops
the loop. Tokens are a post-return threshold, so a running call can overshoot;
the worker budget artifact retains the measured total and overshoot. The loop
does not grant shell, direct-write or delegation tools.

Role packets carry the resolved family checklist, scope, check configuration and
repository conventions to leads, independent reviewers, rechecks, consultants
and security verifiers. Required contract text must fit the packet byte limit;
reference notes may be excerpted with a visible truncation marker. Unrestricted
project editors must end each editing reply with `CHANGED: ["relative/path"]`
(or `CHANGED: []`). The runtime compares that list with files added, changed or
deleted during that call. The measured files are what every scope, check and
design step reads. A list that disagrees with them does not stop the run: it
is recorded on the task as an unverified fact, with the raw reply kept. Only
a reply with a malformed request line and no `CHANGED` line fails its task,
and the orchestrator re-plans. Restricted editors still use the existing PATCH
contract. No reviewer receives another reviewer's findings.
