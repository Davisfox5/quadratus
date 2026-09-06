<p align="center">
  <img src="brand/mark-large.svg" alt="" width="96" height="96">
</p>

<h1 align="center">Quadratus</h1>

<p align="center"><em>Multi-LLM Workflow — four frontier models on one coding task</em></p>


A multi-model coding system: several frontier models collaborate on one
coding task instead of one model working alone. It runs in two ways —
against **billed APIs** (Anthropic, OpenAI, Google) or, more interestingly,
against the **consumer subscriptions you already pay for** (Claude Pro/Max,
ChatGPT Plus/Pro, Google AI Pro/Ultra, SuperGrok) by driving each vendor's
coding-agent CLI, so a run draws on subscription rate-limit windows rather
than per-token billing.

The repo contains two generations of the system:

1. **The collaboration pipeline** (shipping today) — the `quadratus` CLI and
   Gradio GUI run a four-phase plan → consensus → build-and-debate →
   synthesis loop across the configured providers.
2. **The session engine** (under active development) — a persistent
   orchestrator that decomposes a project into sized tasks and routes each
   one to the right model, with cross-vendor review, disposable worker
   models, an append-only ledger, and deterministic gates. Its modules live
   alongside the pipeline in `quadratus/` and are fully unit-tested, but it
   is not yet wired to the CLI entry points.

## Backends: API keys or subscription CLIs

Every provider can be served by either backend, chosen per provider or
globally (`LLM_BACKEND=api|cli`, or `CLAUDE_BACKEND`, `OPENAI_BACKEND`, …):

| Provider | API backend | CLI backend (subscription) |
|----------|-------------|----------------------------|
| Claude | `ANTHROPIC_API_KEY` | `claude` (Claude Pro/Max) |
| ChatGPT | `OPENAI_API_KEY` | `codex` (ChatGPT Plus/Pro) |
| Gemini | `GOOGLE_API_KEY` | `agy` (Google AI Pro/Ultra) |
| Grok | `XAI_API_KEY` | `grok` (SuperGrok / X Premium+) |

CLI specs are declarative (`quadratus/cli_providers.py`), so a vendor
renaming a flag is a one-line fix. Each CLI agent runs sandboxed in a
scratch directory with file writes denied unless explicitly granted —
coding agents will otherwise happily edit your working tree while
"reviewing" it.

**Scope of use:** vendor terms permit driving your own subscription's CLI
for ordinary individual use, on your own machine. Routing other people's
prompts through your credential is prohibited by every vendor, so the GUI
refuses to enable public sharing while a CLI backend is active. See the
docstring in `quadratus/cli_providers.py` for the full reasoning.

## The collaboration pipeline (current entry point)

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

## The session engine (in progress)

The rebuild replaces the flat debate with an orchestrated session. The
architecture, briefly:

- **A persistent orchestrator** (Claude's top tier) is the only participant
  that lives for the whole session; it names the next task and rules on
  questions. It is a hard dependency — unavailable means the run halts, not
  substitutes.
- **A fixed brain trust** of frontier models leads individual tasks. A lead
  keeps full working memory for one task and is wiped when it closes;
  **worker bees** (each vendor's fast model, picked by errand type:
  lookup / read / check / format) keep nothing. Three memory scopes, one
  per role (`memory.py`, `workers.py`).
- **Difficulty-ladder routing** (`task_kinds.py`, `registry.py`): task
  difficulty picks the model; kind-based pins (security, review, mobile…)
  are the exception and each carries its evidence. The roster is a seed —
  probe the installed CLIs rather than trusting hardcoded tables.
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

Then either add at least one API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GOOGLE_API_KEY`, `XAI_API_KEY`) or set `LLM_BACKEND=cli` and sign in to the vendor CLIs
you have subscriptions for. `.env.example` documents every setting,
including per-provider backends and CLI model tiers.

## Usage

```bash
# After `pip install -e .`:
quadratus "Implement an LRU cache with O(1) get/put in Python, with tests"

quadratus --status                 # which providers/backends are configured
quadratus "..." --rounds 2         # more refinement rounds
quadratus "..." --show-stages      # print every intermediate stage
quadratus "..." -o solution.md     # save the full run to a file
```

Web interface: `quadratus-gui` (or `python chat_gui.py`), then open
http://127.0.0.1:7860. Conversation memory, file uploads, and a per-model
contribution breakdown. Sharing is disabled while a CLI backend is active.

## Project layout

```
quadratus/
  config.py           # Settings dataclass, env loading, backend selection
  providers.py        # API providers (Claude/ChatGPT/Gemini/Grok) + retries
  cli_providers.py    # Subscription CLI providers (claude/codex/agy/grok)
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
