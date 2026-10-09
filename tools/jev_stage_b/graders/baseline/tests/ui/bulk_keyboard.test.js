const assert = require("node:assert/strict");
const test = require("node:test"), { loadApp } = require("./load_app");
function deferred() {
  let resolve; const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
const project = id => ({
  id, name: `Project ${id}`,
  clips: [{ id: `${id}-c1`, start: 1, end: 2, label: `${id} clip`, notes: "",
    tag_type: "Pass", players: ["p1"] }],
  tag_types: [{ name: "Pass" }, { name: "Shot" }],
  players: [{ id: "p1", name: "Alex" }, { id: "p2", name: "Blair" }],
});
const previewResult = { count: 1, preview_id: "A-preview", project_id: "A", clips: [{
  id: "A-c1", start: 1, end: 2, label: "A clip",
  before: { tag_type: "Pass", players: ["p1"] },
  after: { tag_type: "Shot", players: ["p1"] },
}] };
function setup() {
  const gates = { preview: deferred(), apply: deferred(), refresh: deferred() };
  let projectReads = 0;
  const routes = {
    "GET /api/projects/A": () => projectReads++ ? gates.refresh.promise : project("A"),
    "GET /api/projects/A/filter_presets": [],
    "POST /api/projects/A/clips/bulk_preview": () => gates.preview.promise,
    "POST /api/projects/A/clips/bulk_apply": () => gates.apply.promise,
  };
  return { app: loadApp({ routes }), gates };
}
async function open(app) {
  await app.evalInApp("openProject('A')");
  app.evalInApp("openBulkModal(); document.getElementById('bulk-tag-type').value = 'Shot'");
}
function key(app, key, shiftKey = false) {
  const event = app.document.createEvent("keydown", { key, shiftKey });
  app.document.activeElement.dispatchEvent(event);
  return event;
}
async function preview(app, gate) {
  const pending = app.evalInApp("previewBulkEdit()");
  gate.resolve(previewResult); await pending;
}
test("Tab wraps eligible controls and modal suppresses shortcuts", async () => {
  const { app } = setup(); await open(app);
  const get = id => app.document.getElementById(id);
  assert.equal(app.document.activeElement, get("bulk-tag-type"));
  assert.equal(get("btn-bulk-confirm").disabled, true);
  assert.equal(get("btn-bulk-refresh").hidden, true);
  get("btn-bulk-cancel").focus();
  const forward = key(app, "Tab");
  assert.equal(forward.defaultPrevented, true);
  assert.equal(app.document.activeElement, get("bulk-tag-type"));
  const backward = key(app, "Tab", true);
  assert.equal(backward.defaultPrevented, true);
  assert.equal(app.document.activeElement, get("btn-bulk-cancel"));
  app.evalInApp("document.getElementById('video').currentTime = 7");
  const calls = app.calls.fetch.length, shortcut = key(app, "i");
  assert.equal(shortcut.defaultPrevented, false);
  assert.equal(app.evalInApp("markIn"), null);
  assert.equal(app.calls.fetch.length, calls);
});
test("Escape closes the modal and restores focus", async () => {
  const { app } = setup(); await open(app);
  const escape = key(app, "Escape");
  assert.equal(escape.defaultPrevented, true);
  assert.equal(app.document.getElementById("bulk-modal").classList.contains("active"), false);
  assert.equal(app.evalInApp("document.getElementById('tagging-screen').inert"), false);
  assert.equal(app.document.activeElement, app.document.getElementById("btn-bulk-edit"));
  assert.equal(key(app, "Escape").defaultPrevented, false);
});
test("Preview recovers focus before its response", async () => {
  const { app, gates } = setup(); await open(app);
  const previewButton = app.document.getElementById("btn-bulk-preview");
  previewButton.focus();
  const pending = app.evalInApp("previewBulkEdit()");
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel");
  gates.preview.resolve(previewResult); await pending;
  assert.equal(app.document.getElementById("btn-bulk-confirm").disabled, false);
  previewButton.focus(); assert.equal(key(app, "Tab").defaultPrevented, true);
  assert.equal(app.document.activeElement.id, "btn-bulk-confirm");
});
test("Confirm and Refresh recover focus while controls disappear", async () => {
  const { app, gates } = setup(); await open(app); await preview(app, gates.preview);
  app.document.getElementById("btn-bulk-confirm").focus();
  const applying = app.evalInApp("applyBulkEdit()");
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel");
  assert.notEqual(app.document.activeElement, app.document.body);
  gates.apply.resolve({ status: 409, body: { code: "stale", error: "Clips changed after preview", conflicts: [] } });
  await applying;
  const refresh = app.document.getElementById("btn-bulk-refresh");
  assert.equal(refresh.hidden, false);
  app.document.getElementById("btn-bulk-preview").focus();
  assert.equal(key(app, "Tab").defaultPrevented, true);
  assert.equal(app.document.activeElement, refresh);
  const refreshing = app.evalInApp("refreshBulkProject()");
  assert.equal(refresh.hidden, true);
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel");
  gates.refresh.resolve(project("A")); await refreshing;
  assert.equal(refresh.hidden, true); assert.equal(app.document.getElementById("btn-bulk-preview").disabled, false);
});
