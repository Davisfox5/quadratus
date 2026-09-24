"""Shared record visibility predicate. Missing rows must fail closed."""


def visible_to(record, tenant):
    return bool(record is not None and tenant and record.get("tenant_id") == tenant)
