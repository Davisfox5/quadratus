# Working notes for Claude

## Communication

**Lead with a TL;DR on anything long or technical.** Plain language, bullets,
no jargon, at the very top — what it means and what the decision is. Put the
technical detail below it for when it's wanted. Don't make the summary an
afterthought at the bottom; it goes first.

This applies to design discussions, research findings, architecture proposals,
and post-change reports. A short answer to a short question doesn't need one.

## Project context

This repo is being rebuilt into a multi-model coding system that runs on
consumer *subscriptions* (Claude Max, ChatGPT, Google AI, SuperGrok) by
driving the vendor CLIs, rather than on billed API keys. See
`multi_llm/cli_providers.py` for why that is permitted for individual use and
where the line is.

Key design decisions already settled:

- **Fable 5 orchestrates and is a hard dependency.** If it is unavailable the
  run halts rather than substituting. It is the only participant that persists
  across a session; everyone else is re-invoked fresh each round.
- **The brain trust is fixed** (Opus 5, GPT-5.6 Sol, Gemini 3.1 Pro, Grok 4.6).
  Nobody is displaced from it, and Sonnet is not in it.
- **Security work is peeled into a bounded excursion**: Opus takes the seat,
  GPT-5.6 Sol does the work, Opus verifies, the excursion closes, Fable
  resumes. Never an open-ended handover.
- **Protecting Fable's context is the binding constraint** on the whole
  system. Nothing raw reaches it; Haiku digests first.
- Model tables are seeds, not truth. The lineup churns monthly — prefer a
  probe against the installed CLIs over anything hardcoded.
