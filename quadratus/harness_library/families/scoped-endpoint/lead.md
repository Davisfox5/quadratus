# scoped-endpoint: lead

You are adding or changing one API endpoint. Done means a second tenant or
user provably gets 404 or 403, every query carries the scope predicate, the
route is registered and imports, and the gates pass. You cannot declare done;
the gate runner does.

Inputs you must have: the route with handler location, the scope owner
(per-user, per-tenant, program-shared or global), and the schemas. Missing one
means stop and return status blocked naming it.

Procedure

1. Read the closest existing route in the repo and match its guard order:
   auth, scope, feature gate, validation.
2. Write the handler. Every query on a scoped model carries the predicate on
   the same statement. A secondary lookup by id also carries it, or is
   justified by a prior scoped fetch you cite.
3. Declare the response model and use the repo's error envelope.
4. Register the router where the app includes routers and prove the import
   with the import-smoke gate.
5. Write the cross-tenant test with the repo's override mechanism, never a
   module patch. It must show a second tenant or user denied.
6. Run the gates in order: scope, scope-predicate-grep, import-smoke,
   cross-tenant-test, unit-tests, lint. Paste the tails.

Do not

- Read tenant identity from a client header.
- Write shared or global data for one tenant.
- Use the page-oriented guard on an API route.
- Add a route without its gate when the feature key is set.

Return: the route list line, one line per query with its predicate quoted, the
cross-tenant test name and tail, and the feature key or none with the reason.
