# Tenant API fixture

Synthetic two-tenant API. GET /records/{record_id} uses current_tenant from the
Bearer token. The endpoint leaks other tenants' records. Fix only app.py while
preserving authentication, data fixtures, the Record(id,title) response and
404/401 behavior. There is no feature entitlement in this test application.

The immutable acceptance test is /opt/quadratus/test_contract.py. Run:
python -m pytest -q -p no:cacheprovider /opt/quadratus/test_contract.py

Do not commit, push, contact external services or install dependencies.
