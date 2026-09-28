# collector: lead

You are writing or changing code that reads from a third party. Done means
the parser is tested against a saved fixture, there is one parser for the
source, every fetch goes through the SSRF guard with a timeout, every failure
mode raises, no value is invented, and the gates pass. The gate runner
declares done.

Inputs you must have: the source, a saved fixture, and the existing parser or
none after a grep. A live probe during development needs a named
authorization and a row cap. Missing one means stop and return status blocked
naming it.

Procedure

1. Grep the parser registry for the source. Extend the existing parser; do
   not write a second one.
2. Write the parser against the fixture. Tests run with no network.
3. Route every fetch and navigation through the SSRF guard with a timeout.
4. Make each failure mode raise: captcha page, empty page, changed layout,
   network error. An empty list is never a success and never means absence.
5. Leave missing values missing. Never guess an email, a name or a date.
6. On reimport, merge field by field and keep human qualification and edits.
7. Reject rows outside the source's declared scope.
8. If a live probe is authorized: opt in by the adapter's flag, cap rows,
   label the output, paste the counts. It never runs in the default path.
9. Run the gates: scope, fixture-tests, duplicate-parser-grep, unit-tests,
   lint. Paste the tails.

Do not

- Touch the browser profile, cookie jar or credentials directory.
- Run the ingest pipeline, an enrich step, or a reconcile apply as a test.
- Fetch live inside a test file.
- Add a fallback that fills a field from a guess.

Return: source and fixture path, parser and duplicate grep result, guard and
timeout lines quoted, the failure cases tested, and the live probe line or
none.
