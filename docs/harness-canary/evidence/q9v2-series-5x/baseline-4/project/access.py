"""Shared record visibility predicate. Missing rows must fail closed."""


def visible_to(record, tenant):
    return bool(tenant) and record is not None and record.get("tenant_id") == tenant
