"""Offline positive/partial controls only. Never copied into a solver project."""

from pathlib import Path


def repair(project: Path, *, lookup: bool, security: bool):
    if lookup:
        path = project / "presentation.py"
        path.write_text('from catalog import compact_caption\n\n\ndef display_title(value):\n'
                        '    return compact_caption(value)\n')
    if security:
        path = project / "access.py"
        path.write_text('def visible_to(record, tenant):\n'
                        '    return record is not None and record.get("tenant_id") == tenant\n')
        path = project / "app.py"
        text = path.read_text().replace('from auth import current_tenant',
                                        'from access import visible_to\nfrom auth import current_tenant')
        path.write_text(text.replace('if record is None:', 'if not visible_to(record, tenant):'))
