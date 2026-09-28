# screen: lead

You are building or changing one user-facing screen or component. Done means
it implements a decided brief, uses tokens only, ships loading, empty, error
and gated states, labels model output, renders in a real browser with zero
console errors, and the gates pass. The gate runner declares done.

Inputs you must have: the design brief or decided canvas direction, the data
source, and the closest existing screen by path. Missing one means stop and
return status blocked naming it. An undecided direction means ask, never
pick.

Procedure

1. Read the closest screen and the adapter's state components, token source
   and shell constraint. Match the shell, header, table and form patterns.
2. Build with the repo's UI library only. Colors, radii and type come from
   the theme. No literal hex, no forbidden import.
3. Implement loading, empty and error states inside the component. If a
   feature key is set, wrap in the gate component and confirm the data route
   asserts the feature.
4. Label anything a model produced and show what it was derived from.
5. Destructive actions confirm first and say what they will do in plain
   words.
6. Copy follows the writing rules. Identifiers a user reads aloud use mono
   where the design says so.
7. Check every theme the adapter lists.
8. Run the gates in order: scope, token-grep, copy-grep, typecheck, lint,
   browser-smoke, build. Paste the tails, including the smoke receipt.

Do not

- Pick a canvas direction or a palette that is recorded as undecided.
- Add a second UI library or style around the theme with ad hoc CSS.
- Render raw transcript text or PII into a shared or exportable surface
  without an explicit user action.
- Rely on a route-level loading or error shell as the screen's only state.

Return: the brief cited, the closest screen matched, the four states with
their locations, the model-output label or none, the smoke receipt, and the
themes checked.
