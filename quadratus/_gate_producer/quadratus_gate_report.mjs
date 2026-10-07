// Harness-owned node:test report producer (quadratus phase 3, #25; node
// runner added after series rule-58a4625 f3).
//
// Loaded as a custom reporter only when a node --test check declares
// --test-reporter-destination={report}. The harness copies this file into a
// directory it owns for that one invocation, under a name derived from the
// invocation's nonce, and passes that copy as --test-reporter with the
// report path as its destination; the runner's own TAP output stays on
// stdout. The report states this module's own file and source digest, which
// the harness compares with the copy it placed, and the nonce the harness
// set, so a stale or foreign file is refused.
//
// It records facts the runner already emits and never interprets prose: for
// each failed test whether its failure was a plain assertion
// (cause.code === 'ERR_ASSERTION', the identity node:assert gives its own
// errors), a hook failure or a cancellation (never the application's), and a
// file that failed to load (a collection error). Same shape as the pytest
// producer so one attribution rule reads both.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const PRODUCER = 'quadratus-node/1';
const MAX_FAILURES = 200;

export default async function* quadratusGateReport(source) {
  const counts = { passed: 0, failed: 0, errors: 0, skipped: 0 };
  const failures = [];
  let truncated = false;
  let collected = 0;
  let collectErrors = 0;
  const record = (entry) => {
    if (failures.length >= MAX_FAILURES) { truncated = true; return; }
    failures.push(entry);
  };
  for await (const event of source) {
    if (event.type === 'test:summary') {
      const c = event.data && event.data.counts;
      if (c && typeof c.tests === 'number') {
        // The last summary is the whole run's; earlier ones are per file.
        collected = c.tests;
        counts.passed = c.passed;
        counts.skipped = (c.skipped || 0) + (c.todo || 0);
      }
      continue;
    }
    if (event.type !== 'test:fail') continue;
    const data = event.data || {};
    const details = data.details || {};
    const error = details.error || {};
    const cause = error.cause;
    const kind = error.failureType;
    const nodeid = `${data.file || ''}::${data.name || ''}`.slice(0, 300);
    const typeName = (e) => (e && (e.name || (e.constructor && e.constructor.name))) || null;
    const fileLevel = (data.nesting || 0) === 0 && details.type !== 'suite'
      && (details.exitCode !== undefined || details.signal !== undefined
          || (cause !== undefined && (cause === null || typeof cause !== 'object')));
    if (kind === 'testCodeFailure' && fileLevel) {
      // The file's own process failed (it threw while loading, or exited
      // on its own): no test body ran, so this is a collection error. Node
      // reports it as a test named after the file with the child's exit
      // code and a string cause ('test failed'), never an Error.
      collectErrors += 1;
      continue;
    }
    if (kind === 'testCodeFailure' && details.type !== 'suite') {
      counts.failed += 1;
      record({ nodeid, when: 'call', exc_type: typeName(cause) || typeName(error),
               assertion: !!(cause && cause.code === 'ERR_ASSERTION') });
      continue;
    }
    counts.errors += 1;
    record({ nodeid, when: kind === 'hookFailed' ? 'setup' : 'teardown',
             exc_type: `${kind || 'unknown'}:${typeName(cause) || typeName(error) || 'Error'}`,
             assertion: false });
  }
  const self = fileURLToPath(import.meta.url);
  const digest = createHash('sha256').update(readFileSync(self)).digest('hex');
  const failed = counts.failed + counts.errors + collectErrors > 0;
  const report = {
    producer: PRODUCER, nonce: process.env.QUADRATUS_GATE_NONCE || '', module_file: self, module_sha256: digest,
    exitstatus: failed ? 1 : 0, collected, collect_errors: collectErrors, counts, failures, truncated,
  };
  yield JSON.stringify(report);
}
