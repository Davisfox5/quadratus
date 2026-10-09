// Run with: node tests/ui/mutation_check.js
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "../.."), APP_PATH = path.join(ROOT, "static/js/app.js");
const TEST_FILES = ["tests/ui/bulk_serialization.test.js", "tests/ui/bulk_keyboard.test.js", "tests/ui/bulk_coverage.test.js"];

function replaceExact(source, pattern, replacement, name, after = 0) {
  const at = source.indexOf(pattern, after);
  if (at < 0) throw new Error(`${name}: pattern absent: ${JSON.stringify(pattern)}`);
  return source.slice(0, at) + replacement + source.slice(at + pattern.length);
}

const MUTANTS = [
  {
    name: "late-success-discarded",
    apply(source) {
      const start = source.indexOf("async function applyBulkEdit()");
      const end = source.indexOf("async function refreshBulkProject()", start);
      if (start < 0 || end < 0) throw new Error(`${this.name}: apply function not found`);
      const body = replaceExact(source.slice(start, end),
        "    if (!currentProject || currentProject.id !== projectId) return;",
        "    if (generation !== bulkPreviewGeneration || !currentProject || currentProject.id !== projectId) return;",
        this.name);
      return source.slice(0, start) + body + source.slice(end);
    },
  },
  {
    name: "guard-dropped",
    apply(source) {
      return replaceExact(source, "bulkApplyPending = { projectId };",
        "bulkApplyPending = null;", this.name);
    },
  },
  {
    name: "capture-after-await",
    apply(source) {
      let mutated = replaceExact(source,
        "  const tagType = $bulkTagType.value;\n  const player = $bulkPlayer.value;",
        "  let tagType, player;", this.name);
      mutated = replaceExact(mutated, "    const data = await res.json();",
        "    const data = await res.json();\n    tagType = $bulkTagType.value;\n    player = $bulkPlayer.value;",
        this.name, mutated.indexOf("  let tagType, player;"));
      return mutated;
    },
  },
  {
    name: "busy-ignores-project",
    apply(source) {
      return replaceExact(source,
        "  return Boolean(bulkApplyPending && currentProject\n    && bulkApplyPending.projectId === currentProject.id);",
        "  return Boolean(bulkApplyPending);", this.name);
    },
  },
  {
    name: "stale-preview-restored",
    apply(source) {
      const guard = "    if (generation !== bulkPreviewGeneration || !currentProject || currentProject.id !== projectId) return;\n";
      const start = source.indexOf("async function previewBulkEdit()");
      if (start < 0) throw new Error(`${this.name}: preview function not found`);
      let body = source.slice(start);
      for (let i = 0; i < 2; i++) body = replaceExact(body, guard, "", this.name);
      return source.slice(0, start) + body;
    },
  },
  {
    name: "apply-ignores-preview-project",
    apply(source) {
      return replaceExact(source,
        "  if (!bulkPreview || !currentProject || bulkPreview.projectId !== currentProject.id) return;",
        "  if (!bulkPreview || !currentProject) return;", this.name);
    },
  },
  {
    name: "settle-focus-dropped",
    apply(source) {
      return replaceExact(source,
        "  if (target && $bulkModal.contains(target) && !target.disabled && !target.hidden) target.focus();",
        "", this.name);
    },
  },
  {
    name: "tab-keeps-parked-ownership",
    apply(source) {
      return replaceExact(source,
        "      // The user chose where focus is now, even if that is the parked control.\n      bulkAutoFocused = null;\n",
        "", this.name);
    },
  },
  {
    name: "tab-not-prevented",
    apply(source) {
      return replaceExact(source,
        "      bulkAutoFocused = null;\n      e.preventDefault();",
        "      bulkAutoFocused = null;", this.name);
    },
  },
];

function runSuite(appPath) {
  const run = spawnSync(process.execPath, ["--test", "--test-timeout=2000", "--test-reporter=tap", ...TEST_FILES], {
    cwd: ROOT,
    env: { ...process.env, APP_JS_PATH: appPath, FORCE_COLOR: "0" },
    encoding: "utf8",
    timeout: 10000,
  });
  if (run.error) throw run.error;
  // Node 24's default spec reporter uses ℹ; TAP and earlier defaults use #.
  const passed = run.stdout.match(/^(?:#|ℹ) pass (\d+)$/m);
  const failed = run.stdout.match(/^(?:#|ℹ) fail (\d+)$/m);
  if (!passed || !failed) {
    throw new Error(`test summary missing for ${appPath}:\n${run.stdout}\n${run.stderr}`);
  }
  return {
    tests_failed: Number(failed[1]), tests_passed: Number(passed[1]), exit_code: run.status,
    failed_tests: [...run.stdout.matchAll(/^not ok \d+ - (.+)$/gm)].map(match => match[1]),
  };
}

const hash = source => crypto.createHash("sha256").update(source).digest("hex");

function main() {
  const sourceBytes = fs.readFileSync(APP_PATH), source = sourceBytes.toString("utf8");
  const before = hash(sourceBytes);
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "gametape-mutants-"));
  let after;
  let results;
  try {
    const baseline = {
      name: "baseline", target_file: APP_PATH, replacement_applied: false,
      ...runSuite(APP_PATH), killed: false,
    };
    const mutants = MUTANTS.map(mutant => {
      const target = path.join(tempDir, `${mutant.name}.js`);
      fs.writeFileSync(target, mutant.apply(source));
      const outcome = runSuite(target);
      return {
        name: mutant.name, target_file: target, replacement_applied: true,
        ...outcome, killed: outcome.tests_failed >= 1,
      };
    });
    results = { baseline, mutants };
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
    after = hash(fs.readFileSync(APP_PATH));
    if (before !== after) throw new Error("static/js/app.js changed during mutation check");
  }
  results.source_sha256_before = before;
  results.source_sha256_after = after;
  results.source_unchanged = true;
  console.log(JSON.stringify(results, null, 2));
  if (results.baseline.exit_code !== 0 || !results.baseline.tests_passed || results.baseline.tests_failed || results.mutants.some(item => !item.killed)) {
    process.exitCode = 1;
  }
}

main();
