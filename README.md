# Multi-LLM Workflow

Make **Claude**, **ChatGPT**, and **Gemini** collaborate on a single coding
task. The models first agree on a plan and divide the work by their strengths,
then one drafts a solution, the others review and refine it over one or more
rounds, and a synthesizer merges the strongest ideas into one definitive
answer. The goal: tackle harder problems than any single model handles alone.

## How it works

The workflow is a four-phase pipeline (see `multi_llm/orchestrator.py`):

1. **Plan** — every available model independently analyses the task and
   proposes how to divide the work.
2. **Consensus** — a coordinator merges those proposals into one agreed plan
   that assigns each model a role based on its strengths for *this* project.
3. **Build & debate** — a lead drafts the solution and the others adversarially
   review and refine it in their assigned roles, converging over `ROUNDS`
   rounds.
4. **Synthesis** — a coordinator merges every contribution into the final
   answer.

It **degrades gracefully**: configure one key and you get a strong solo answer;
configure two or three and they collaborate. A missing key or uninstalled SDK
disables that provider instead of crashing the run.

## Models

Defaults are the strongest coding models from each provider; every ID is
overridable via the environment to match your account access:

| Provider | Env var | Default |
|----------|---------|---------|
| Anthropic (Claude) | `CLAUDE_MODEL` | `claude-opus-4-8` |
| OpenAI (ChatGPT) | `OPENAI_MODEL` | `gpt-5.5-pro` |
| Google (Gemini) | `GEMINI_MODEL` | `gemini-3.1-pro` |

> If a default model name isn't available on your plan, set the corresponding
> `*_MODEL` variable to one that is.

## Setup

```bash
git clone https://github.com/Volunteer-Assistants/multi-llm-workflow.git
cd multi-llm-workflow
pip install -r requirements.txt   # or: pip install -e .

cp .env.example .env              # then add your API key(s)
```

You need **at least one** of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or
`GOOGLE_API_KEY`. See `.env.example` for every available setting.

## Usage

### Command line

```bash
# After `pip install -e .`:
multi-llm "Implement an LRU cache with O(1) get/put in Python, with tests"

# Or without installing:
python multi_model_workflow.py "Build a rate limiter using the token bucket algorithm"
```

Useful flags:

```bash
multi-llm --status                       # show which providers are configured
multi-llm "..." --rounds 2               # more refinement rounds
multi-llm "..." --show-stages            # print every intermediate stage
multi-llm "..." -o solution.md           # save the full run to a file
```

### Web interface

```bash
multi-llm-gui          # or: python chat_gui.py
```

Then open http://127.0.0.1:7860. Features: conversation memory, file uploads,
and a collapsible breakdown showing how each model contributed.

## Configuration

All settings are environment variables (read from `.env`). Key ones:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROVIDER_ORDER` | `claude,openai,gemini` | Role order; first is the lead |
| `ROUNDS` | `1` | Review/refinement rounds |
| `SYNTHESIZER` | `lead` | Which provider writes the final answer |
| `MAX_TOKENS` | `8000` | Max output tokens per call |
| `REQUEST_TIMEOUT` | `120` | Per-request timeout (seconds) |
| `MAX_RETRIES` | `4` | Retries on transient/rate-limit errors |

See `.env.example` for the complete list.

## Project layout

```
multi_llm/
  config.py         # Settings dataclass, env loading, defaults
  providers.py      # Unified Claude/ChatGPT/Gemini providers + retries
  prompts.py        # System & template prompts for each phase
  orchestrator.py   # Collaboration pipeline
  cli.py            # Command-line interface
  gui.py            # Gradio web interface
multi_model_workflow.py  # thin CLI wrapper (back-compat)
chat_gui.py              # thin GUI wrapper (back-compat)
tests/                   # pytest suite (no network needed)
```

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

CI runs ruff and the test suite on Python 3.9/3.11/3.12
(`.github/workflows/ci.yml`). The tests mock the providers, so they run
offline without API keys.

## License

MIT
