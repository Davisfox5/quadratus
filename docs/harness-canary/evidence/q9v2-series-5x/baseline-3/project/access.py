"""Shared record visibility predicate. Missing rows must fail closed."""


def visible_to(record, tenant):
    return (
        record is not None
        and bool(tenant)
        and record["tenant_id"] == tenant
    )
