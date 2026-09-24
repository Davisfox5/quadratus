"""Public synthetic data and the existing display helper. Read-only fixture."""

RECORDS = {
    "alpha-1": {"id": "alpha-1", "tenant_id": "alpha", "title": "Alpha record"},
    "beta-1": {"id": "beta-1", "tenant_id": "beta", "title": "Beta record"},
}


def compact_caption(value):
    """Collapse whitespace, trim, and limit to 24 characters including ellipsis."""
    text = " ".join(value.split())
    return text if len(text) <= 24 else text[:23] + "…"
