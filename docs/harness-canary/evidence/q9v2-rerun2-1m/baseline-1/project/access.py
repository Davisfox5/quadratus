"""Shared record visibility predicate. Missing rows must fail closed."""


def visible_to(record, tenant):
    if record is None or not tenant:
        return False
    return record.get("tenant_id") == tenant
