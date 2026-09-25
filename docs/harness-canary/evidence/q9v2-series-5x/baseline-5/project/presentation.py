"""Display adapter. Preserve its signature and reuse the catalog helper."""

import catalog


def display_title(value):
    return catalog.compact_caption(value)
