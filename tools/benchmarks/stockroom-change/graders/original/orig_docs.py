from pathlib import Path

import pytest


ERROR_STATUSES = [
    ("unknown_sku", "404"),
    ("invalid_quantity", "400"),
    ("insufficient_stock", "409"),
    ("invalid_holder", "400"),
    ("invalid_ttl", "400"),
    ("unknown_reservation", "404"),
    ("already_committed", "409"),
    ("reservation_released", "409"),
    ("reservation_expired", "409"),
    ("bad_request", "400, 414, 431, 505"),
    ("invalid_json", "400"),
    ("invalid_body", "400"),
    ("missing_field", "400"),
    ("unknown_field", "400"),
    ("invalid_length", "400"),
    ("invalid_id", "400"),
    ("not_found", "404"),
    ("method_not_allowed", "405"),
    ("body_too_large", "413"),
    ("usage", "-"),
]


@pytest.mark.parametrize("code,status", ERROR_STATUSES)
def test_error_reference_documents_code_status_and_meaning(code, status):
    path = Path(__file__).resolve().parents[1] / "docs" / "errors.md"
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("|")
    ]
    matches = [row for row in rows if row[0].strip("`") == code]
    assert len(matches) == 1, f"expected exactly one table row for {code}"
    row = matches[0]
    assert len(row) == 3
    assert row[1] == status
    assert row[2], f"missing meaning for {code}"
