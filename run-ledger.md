## Goal (verbatim, unchanged)

Add one small usability feature to the existing sorted GameTape project library: a Restore library order button. Preserve current UI and every existing search, sort, count, create/delete confirmation, navigation, player/tagging/preset behavior. No dependency, backend, seed/reset, provider or fixture changes.

G1 Add #btn-project-sort-reset beside the sort controls. A normal click restores original API/library position order, sets #projects-container data-sort="original", and sets readable #project-sort-status to "Library order". Repeated reset is harmless. The next ordinary sort click starts A→Z, then Z→A as before. Reset is local: no API request or reload.
G2 Reset preserves typed search/hash/count and matching cards/no-match vs true-empty. It returns focus to the search field. Normal create/delete still reloads correctly in the active order and navigation uses the same ID. Add meaningful NEW tests only under tests/gui_completion/ for reset from both sort directions, stable library ordering, query/hash/count/focus preservation and no network. All baseline tests remain byte-identical, including the original23.
G3 Controls work at1280×800 and390×844 with no overflow or unexpected browser errors. Declare actual click/wait capture steps on fresh disposable seeded backend: wait for3cards, click sort once, click restore, wait for data-sort="original" and first Alpha Cup, capture desktop/mobile. Report actual capture/reviewer/check findings, blocked/skipped/NOTRUN honestly. Capture clicks do not prove typing; real Node and independent browser controls cover it. Do not claim GUI/report completion before actual harness facts.

## Standing rules (always in force)

- Project source is available in the working directory. Use actual files as evidence. Do not commit or publish changes.

## Requirements (numbered from the goal; the run is done only when every one is met)

- R1: A `#btn-project-sort-reset` button exists in `.project-search-row` beside `#btn-project-sort`, with no other markup/CSS changes to existing controls. [open]
- R2: Clicking reset reorders `.project-card` children of `#projects-container` to ascending `data-index` (API order), sets `#projects-container` `data-sort="original"`, and sets `#project-sort-status` text to "Library order". [open]
- R3: Clicking reset repeatedly leaves order, data-sort and status unchanged (idempotent, no error). [open]
- R4: After reset, the next `#btn-project-sort` click yields ascending (data-sort="asc", button "Sort Z→A"), then descending, exactly as before. [open]
- R5: Reset is local: it issues no fetch, does not call `loadProjects`, and leaves `window.location`/hash unchanged. [open]
- R6: Reset preserves the typed search value, the `#q=` hash, `#project-search-count` text, each card's hidden state, and the `#project-search-empty` vs `#projects-empty` distinction. [open]
- R7: Reset moves focus to `#project-search`. [open]
- R8: After reset, create/delete still reload via `loadProjects` and the list re-renders with data-sort="original" in API order; card click still opens the same project id. [open]
- R9: Initial-load state is unchanged: no `data-sort` attribute, status "Original order", button "Sort A→Z". [open]
- R10: New tests live only under `tests/gui_completion/` and cover reset from asc, reset from desc, stable tie/library ordering, query/hash/count/focus preservation, and no network; a pytest wrapper runs them with the project check command. [open]
- R11: No existing test file is modified (byte-identical baseline, including the existing 23 cases); no dependency, backend, seed/reset, provider or fixture changes. [open]
- R12: Controls render without overflow or console errors at 1280×800 and 390×844; a real-browser capture shows the post-reset state on a fresh seeded backend. [open]
- R13: Declare and execute fresh seeded-backend capture steps at both 1280×800 and 390×844: wait for 3 cards, click sort once, click restore, wait for data-sort="original" and first card "Alpha Cup", then capture. [open (added by the requirements review)]
- R14: Verify typing-related preservation with real Node tests and independent browser controls; capture clicks alone are insufficient. [open (added by the requirements review)]
- R15: Report actual harness, capture, reviewer, and check findings, marking blocked, skipped, or NOTRUN honestly and withholding GUI/report completion claims until those facts exist. [open (added by the requirements review)]

## Completed work

_Nothing completed yet._