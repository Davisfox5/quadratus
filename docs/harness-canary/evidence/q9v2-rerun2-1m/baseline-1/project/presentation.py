"""Display adapter. Preserve its signature and reuse the catalog helper."""

from catalog import compact_caption


def display_title(value):
    return compact_caption(value)
