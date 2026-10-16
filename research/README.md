# Research inbox

The Friday AI Coding Research Brief publishes one draft pull request per date.
The branch is `research/digest-YYYY-MM-DD`; the title is
`AI research digest: YYYY-MM-DD`. Issues are disabled in this repository.

The only input file is `research/digests/YYYY-MM-DD.json`, encoded as UTF-8:

```json
{
  "version": 1,
  "date": "2026-10-16",
  "markdown": "The complete digest, including publication dates and direct source links."
}
```

The local watcher adds `research/reviews/YYYY-MM-DD.md` and
`research/reviews/YYYY-MM-DD.receipt.json` to the same draft PR. The receipt
records the digest hash, exact source and engine commits, run status, and
independent truth-check verdict. Review completion does not verify a proposed experiment.

No source changes or automatic merges are part of this workflow. See
[installation and operations](../docs/RESEARCH_INBOX.md).
