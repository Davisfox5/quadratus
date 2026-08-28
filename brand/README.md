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
| paper | `#EFE6D5` | tile ground, ribbon fill |
| ink | `#3A342C` | ribbon edge, rung outlines, wordmark |
| rust | `#B35A3C` | rung 1 |
| teal | `#43697A` | rung 2 |
| bone | `#E4D8C2` | rung 3 |
| slate | `#6B7B7E` | rung 4 |

Two colours are lifted for the small inverted mark, where the originals go
muddy against the ink tile: rust `#C9663F`, teal `#5A8798`.

**These values were read off the source plate by eye, not sampled.** Resample
them from the original illustration and update this table and the SVGs
together before the set goes anywhere public.

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
