const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { loadApp } = require("./load_app");

test("loads the template and real app, including an APP_JS_PATH override", async t => {
  const app = loadApp();
  await Promise.resolve();
  await Promise.resolve();
  assert.ok(app.document.getElementById("bulk-modal").parentNode);
  assert.ok(app.document.getElementById("bulk-tag-type").parentNode);
  assert.deepEqual(
    { method: app.calls.fetch[0].method, path: app.calls.fetch[0].path },
    { method: "GET", path: "/api/projects" },
  );
  assert.equal(app.evalInApp("typeof applyBulkEdit"), "function");

  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "gametape-load-app-"));
  t.after(() => fs.rmSync(temp, { recursive: true, force: true }));
  const copy = path.join(temp, "app.js");
  const source = fs.readFileSync(path.resolve(__dirname, "../../static/js/app.js"), "utf8");
  fs.writeFileSync(copy, `${source}\nglobalThis.__sourceOverrideLoaded = true;\n`);
  const previous = process.env.APP_JS_PATH;
  process.env.APP_JS_PATH = copy;
  try {
    assert.equal(loadApp().evalInApp("__sourceOverrideLoaded"), true);
  } finally {
    if (previous === undefined) delete process.env.APP_JS_PATH;
    else process.env.APP_JS_PATH = previous;
  }
});
