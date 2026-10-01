# Jev sample packet (T-grok-samples)

Author: Grok. Reviewer: Codex. Not frozen. No Jev call.

Machine copy: `tests/fixtures/jev-samples/samples.json`. Engine binding is still `f96e0c88e7a18156a9582e3aa19de4c9d70ff89f`. Claude has not named the qualified combined SHA yet, so this commit does not move `base_sha` or `projects.engine`. GameTape stays `1cd9264edb4429f00cde43a04a1944d0dca37f11`.

Unlabelled `route()` is general/simple/`grok:default` on every row. Accepted pairs:

| id | accepted pair | lead |
|---|---|---|
| dev-favicon | frontend + simple | grok:default |
| dev-node-status | docs + rote | grok:default |
| dev-trial-sentence | docs + rote | grok:worker |
| hold-heading | frontend + rote | grok:default |
| hold-import-note | docs + simple | grok:default |
| hold-dotenv-guard | security + simple | openai:gpt-5.6-sol |
| hold-one-assertion | test + rote | openai:gpt-5.6-sol |
| hold-budget-threads | concurrency + simple | grok:default |
| hold-import-readonly | comprehend + simple | grok:default |
| hold-template-get | docs + simple | grok:default |

Coverage gaps, left open on purpose: no standard row, no complex row. Nothing was promoted to fill them.

## Row corrections

| id | was wrong | correction | evidence | uncertainty |
|---|---|---|---|---|
| dev-favicon | empty capture; prose could be read as a byte copy | wait for `link[rel=icon][href=/favicon.ico]`; SVG under 20 lines; reconstruction, not a byte copy | preserved request plus `templates/index.html` head | harness widths are the ordinary desktop and mobile captures, not a pixel size I invented |
| dev-node-status | a missing mutation script; grader was file existence | `node tests/ui/import_preview.test.js`; note must match the captured exit | script is in the 1cd9264 patch | the operator must capture the exit in the same environment |
| dev-trial-sentence | none; kept as the schema control | no-command binds the editor; the operator may still diff | previous prepare passed | none |
| hold-heading | capture of `/` with no steps cannot show a hidden section | seed, then click `.project-card`, wait `#btn-import-preview`, file `#import-preview-file`, wait `#import-preview-heading` | `static/js/app.js` `openProject` closes the section; `runImportPreview` opens it; heading starts `hidden` | the file step must dispatch the input `change` event; if the harness only sets `.value`, the section stays hidden |
| hold-import-note | a token plus a clean diff could pass | note must say `_load_projects` is called and `_save_projects` is not | `import_preview` in the 1cd9264 patch | none |
| hold-dotenv-guard | not-200 is already green | same assertion, 403 and `error=denied`, red on today's 404; check stays proposed | Codex baseline measurement | green-after is not claimed |
| hold-one-assertion | the BOM test already asserts `preview_only` | add it to `test_preview_repeated_ignored_headers_keep_physical_header_width` | that test's body in the patch stops at the valid summary | none |
| hold-budget-threads | called complex without a failing baseline | simple once-only finish coverage; production file not permitted | `reserve`/`finish` share one lock; existing parallel test never finishes | the barrier is not a proof that every lost-update mutant fails |
| hold-import-readonly | review was kept in a way that selected Sol | comprehend only; grader checks the call chain, and `git status --porcelain` includes untracked files | same `import_preview` source | none |
| hold-template-get | named the project list and used a millisecond bar | warm template `GET /` via `app.index`, three `perf_counter` samples, no threshold, kind docs | `app.index` returns `render_template` only | durations will differ by machine; that is not a failure |

## Recipes

### hold-heading seed and fixture

Operator, temporary data directory, not a candidate-source edit:

1. `POST /api/projects` with `{"name":"Preview seed"}`. A new project includes tag type `Pass`.
2. Copy the fixture bytes below to `.quadratus/capture-fixtures/hold-heading/one-row.csv`.

```csv
Tag Type,Start (s),End (s),Label
Pass,1,2,seed
```

Capture path `/`, one `.project-card`. Steps are in the task scope. Settled result: `#import-preview-summary` is `Total 1 · valid 1 · malformed 0 · duplicate 0`, and the heading text is `CSV preview`. Repeat at the harness desktop width and the harness mobile width. Keep an independent design review.

### hold-template-get command

Temporary directory. Point `DATA_DIR`, `VIDEOS_DIR`, `RECORDINGS_DIR`, and `PROJECTS_FILE` at it before any request. One warm-up `GET /` is outside the timer and must be 200. Then three timed `GET /` calls. The timer is `time.perf_counter` around `test_client.get("/")` only. Import, directory creation, the warm-up, and the status checks are outside it. Each measured response must be 200. Write the command and the three nonnegative second-values into `docs/template-get.md`. This does not call `list_projects`.

### hold-dotenv-guard assertion

Temporary test client and data directory. Do not read a credential file.

```python
rv = client.get("/.env")
assert rv.status_code == 403
assert rv.get_json()["error"] == "denied"
```

On the unchanged tree this fails because the status is 404. The check is proposed until a reviewer runs it.

### hold-budget-threads oracle

`RunLimits(max_calls=4, max_reported_tokens=100)`. Reserve twice on the main thread. Two futures wait on `Barrier(2)` and finish the two tickets with usages `{input_tokens: 9, output_tokens: 1}` and `{input_tokens: 4, output_tokens: 2}`. Barrier and `future.result` use a timeout. After both return: tickets distinct, `reserved_attempts == 2`, `input_tokens == 13`, `output_tokens == 3`, `reported_tokens == 16`, `in_flight == 0`, `unknown_usage_attempts == 0`, `stop_reason == ""`. A second finish of either ticket raises `RuntimeError` and those fields are unchanged. No sleep. `quadratus/run_budget.py` is not edited.

### Read-only graders

`hold-import-note`: the note names `import_preview`, says it calls `_load_projects`, and says it does not call `_save_projects`. `git status --porcelain` shows only that new file.

`hold-import-readonly`: the report makes the same three claims. `git status --porcelain` is empty, including untracked files. `edits` is `none`.

### dev-node-status grader

The operator runs `node tests/ui/import_preview.test.js` and keeps the exit status. The note must contain that same status. Existence of the file is not acceptance.

