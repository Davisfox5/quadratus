# Read-only clip manifest preview

Add a CSV clip-manifest preview to GameTape so a coach can check a list of
clips before importing it elsewhere. This feature must never save clips,
change project data, or read/process video files.

Accept UTF-8 CSV (including an optional BOM) with columns `clip_id`, `label`,
`start_seconds`, `end_seconds`. Handle quoted commas and embedded newlines.
Require nonempty IDs/labels and finite numeric times with
`0 <= start_seconds < end_seconds`. Report duplicate IDs within the upload
and IDs already used by the selected project's clips. Show row-specific issues
and totals, retain input order, and render labels as text even if they resemble
HTML. Reject malformed CSV and files over 256 KiB or over 500 data rows with a
clear error. Missing projects and invalid inputs must not change stored data.

Expose the preview in the existing project screen with a labelled file input,
a clear results/status area, keyboard-operable controls and scrollable results.
Use a read-only preview endpoint under the existing project's API namespace.
Include automated backend and browser/UI checks, representative example CSVs,
and a short explanation of the supported format with a CSV specification
reference. Existing behavior and tests must continue to work.

Do not add dependencies, database changes, authentication, media processing,
or an action that saves imported clips. Keep the change confined to this preview.
