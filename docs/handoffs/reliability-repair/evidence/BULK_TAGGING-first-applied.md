# Bulk clip tagging — design and API contract

Filter the clip list as usual, open bulk edit, choose a new tag type and/or a
new player assignment, preview the exact clips and their before/after values,
then confirm. This document fixes the contract before any code is written.

## Why a two-step preview

Every mutation route in `app.py` today does `_load_projects()` → mutate the
dict → `_save_projects(projects)` with no lock: `update_clip` (lines 503–520)
and `delete_clip` (523–532) are the plainest examples. Two request threads can
each read the whole `projects.json`, change different clips, and write the full
file back; the second write silently discards the first. A bulk edit touches
many clips at once, so the design therefore adds explicit serialization and an
explicit optimistic-concurrency check rather than trusting request timing.

## Endpoints

`POST /api/projects/<pid>/clips/bulk/preview`

```
{"clip_ids": ["a1b2c3d4", ...], "tag_type": "Shot", "players": ["p1"]}
```

Omit `tag_type` or `players` to leave that field unchanged. `"players": []`
means explicitly clear the assignment; `"players": null` is rejected as invalid
rather than guessed at. Success is `200`:

```
{"preview_id": "...", "project_id": "...",
 "clips": [{"id": "a1b2c3d4",
            "before": {"tag_type": "Pass", "players": ["p1", "p2"]},
            "after":  {"tag_type": "Shot", "players": ["p1"]}}]}
```

`POST /api/projects/<pid>/clips/bulk/apply` takes `{"preview_id": "..."}` and
returns `200 {"applied": N, "clips": [...]}` with the updated clip records.

### Status codes

- `400` — `clip_ids` empty, not a list, containing non-strings, containing
  duplicates, or containing an id absent from this project; `tag_type` not in
  `tag_types[].name`; any player id not in `players[].id`; neither field
  supplied, or the request is a no-op because no clip would change. Body is
  `{"error": "..."}` naming the offending value.
- `404` — unknown project; unknown `preview_id`; or a `preview_id` that belongs
  to a different project than the one in the URL. Foreign-project clip ids are
  a `400` at preview time, since they are simply not ids of this project.
- `409` — the world moved under the preview. Body is
  `{"error": "...", "conflicts": [{"id": "...", "reason": "modified",
  "expected": {...}, "actual": {...}}]}`. `reason` is `"modified"` when the
  clip's current `tag_type`/`players` differ from the snapshot (`actual` is the
  current pair), `"deleted"` when the clip is gone (`actual` is `null`), and
  `"consumed"` on the whole batch when that preview was already applied. A
  preview_id not found after a server restart returns `404`; the client treats
  both the same way and asks the coach to refresh.
- `200` — applied, as above.

Validation is complete before any write: a batch is entirely applied or
entirely rejected.

## Concurrency model

A single module-level `threading.RLock` in `app.py` wraps load → modify → save
in **every** mutation route — clips, annotations, recordings, players, filter
presets, tag types, video trim/split/cut, and project create/delete — not only
the new ones. A lock on the bulk path alone would not stop `update_clip` from
overwriting a bulk save.

Previews live in a module-level in-memory registry,
`{preview_id: {"project_id": ..., "changes": ..., "snapshot": ...}}`, where
`snapshot` records each clip's `tag_type` and `players` at preview time. Apply,
inside the lock, `pop`s the preview from the registry first, then reloads
projects, then compares every clip's current `tag_type`/`players` against the
snapshot. Any mismatch or missing clip rejects the whole batch before writing.
Otherwise it sets only those two fields and calls `_save_projects` once.

Popping first is what makes two simultaneous confirmations of one preview
safe: exactly one thread gets the entry and applies it, the other finds nothing
and gets `409 consumed`. It never reports success twice, and it never applies
twice.

Untouched by a bulk apply: annotations, `start`/`end` timing, recordings,
`label`, `notes`, clips outside the batch, other projects, and
`filter_presets`. Exports keep their current filter semantics and read the
updated clips because they read the same file.

