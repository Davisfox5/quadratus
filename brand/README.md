# Quadratus — brand assets

The mark is one crossing of a DNA double helix: two ribbon backbones twisting
past each other, with four base-pair rungs between them. The four rungs are
the four models the tool runs — Claude, ChatGPT, Gemini, Grok — and each keeps
its own colour.

It derives from a full-plate engraved illustration of the helix (kept
separately as the hero asset). The illustration does not reduce; these files
are a vector redraw that keeps what survives scale — the ribbon silhouette
with its ink edge, the four-colour rung coding, the warm paper ground — and
drops what does not: stipple, hatching, aggregate fill, metal collars.

## Files

| File | Use |
|---|---|
| `mark-large.svg` | 96px and up. Four rungs, outlined; ribbon keeps its ink edge. |
| `mark-medium.svg` | 32–64px. Three rungs, no rung outlines; ribbon keeps its edge. |
| `mark-small.svg` | 24px and under. Inverted — bone helix on an ink tile. |
| `favicon.svg` | Copy of `mark-small.svg`. |
| `mark-reverse.svg` | One colour, no tile. Favicon masks, embroidery, single-ink print. |
| `lockup-horizontal.svg` | Mark + wordmark + descriptor, on one line. |
| `lockup-stacked.svg` | Mark above the wordmark, centred. |

These are three separate drawings, not one that scales. Pick the file that
matches the size you are rendering at — scaling `mark-large.svg` down to 16px
produces a muddy blob, which is the whole reason the set exists.

## Palette

| Token | Hex | Where |
|---|---|---|
| paper | `#F1E4CF` | tile ground, ribbon fill |
| ink | `#191711` | ribbon edge, rung outlines, wordmark |
| rust | `#BA7142` | rung 1 |
| teal | `#486E71` | rung 2 |
| bone | `#F0E2CD` | rung 3 |
| slate | `#668682` | rung 4 |

Sampled from the source plate, not estimated. `paper` and `ink` are the median
of the background field and of the darkest 0.5% of pixels; each rung colour is
the median of the most-saturated third of its colour family, which is the body
of the capsule rather than its stippled edge.

`bone` sits two steps off `paper` by design — in the plate those rungs are
separated from the ground by their outline, not by their fill, and the mark
reproduces that.

Contrast holds in both directions: every rung clears 3:1 against `paper` in the
large mark and against `ink` in the small inverted one, so the small mark needs
no lifted substitutes.

## Type

Wordmark is **Libre Baskerville Bold**, all caps, `letter-spacing: 4.6` at
27px (≈0.17em). Descriptor is **JetBrains Mono Regular**, caps, ≈0.24em.

The two lockup files set the wordmark as live `<text>` with a Georgia
fallback. That is fine on the web where the font is loaded; **convert the text
to outlines in a vector editor before any print or third-party use**, or the
wordmark will silently reflow to the fallback.

## Rules

- Do not recolour the rungs. The four colours are the four models; changing
  them breaks the only idea in the mark.
- Do not scale one file to a size band it is not drawn for.
- Do not place the paper-ground marks on a dark background — use
  `mark-small.svg` or `mark-reverse.svg` instead.
