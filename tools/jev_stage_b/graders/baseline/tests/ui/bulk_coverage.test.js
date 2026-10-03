// Regressions for the interleavings the first harness left uncovered: stale
// previews after filter and project changes, a second project's preview
// actually reaching the server, the preview-project guard on Confirm, and
// where focus lands once a request settles.
const assert = require("node:assert/strict");
const test = require("node:test");
const { loadApp } = require("./load_app");

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

const project = id => ({
  id, name: `Project ${id}`,
  clips: [
    { id: `${id}-c1`, start: 1, end: 2, label: `${id} pass`, notes: "", tag_type: "Pass", players: ["p1"] },
    { id: `${id}-c2`, start: 3, end: 4, label: `${id} shot`, notes: "", tag_type: "Shot", players: [] },
  ],
  tag_types: [{ name: "Pass" }, { name: "Shot" }, { name: "Goal" }],
  players: [{ id: "p1", name: "Alex" }, { id: "p2", name: "Blair" }],
});

const previewFor = (id, clipId) => ({ count: 1, preview_id: `${id}-preview`, project_id: id, clips: [{
  id: clipId, start: 1, end: 2, label: "clip",
  before: { tag_type: "Pass", players: ["p1"] }, after: { tag_type: "Goal", players: ["p1"] },
}] });

function setup() {
  const gates = { previewA: deferred(), previewB: deferred(), applyA: deferred() };
  const routes = {
    "GET /api/projects/A": project("A"), "GET /api/projects/B": project("B"),
    "GET /api/projects/A/filter_presets": [], "GET /api/projects/B/filter_presets": [],
    "POST /api/projects/A/clips/bulk_preview": () => gates.previewA.promise,
    "POST /api/projects/B/clips/bulk_preview": () => gates.previewB.promise,
    "POST /api/projects/A/clips/bulk_apply": () => gates.applyA.promise,
  };
  return { app: loadApp({ routes }), gates };
}

const get = (app, id) => app.document.getElementById(id);
const status = app => app.evalInApp("document.getElementById('bulk-status').textContent");
const previewCalls = app => app.calls.fetch.filter(call => call.path.endsWith("/bulk_preview"));
const applyCalls = app => app.calls.fetch.filter(call => call.path.endsWith("/bulk_apply"));

function key(app, key, shiftKey = false) {
  const event = app.document.createEvent("keydown", { key, shiftKey });
  app.document.activeElement.dispatchEvent(event);
  return event;
}

async function openWithTag(app, id, tag = "Goal") {
  await app.evalInApp(`openProject('${id}')`);
  app.evalInApp(`openBulkModal(); document.getElementById('bulk-tag-type').value = '${tag}'`);
}

test("a preview that returns after the filter changed is discarded", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  const pending = app.evalInApp("previewBulkEdit()");
  assert.equal(previewCalls(app).length, 1);
  assert.equal(JSON.parse(previewCalls(app)[0].init.body).clip_ids.length, 2);
  // The filter changes while the request is in flight (real change event).
  get(app, "filter-tag-type").value = "Shot";
  get(app, "filter-tag-type").dispatchEvent(app.document.createEvent("change"));
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  assert.equal(app.evalInApp("bulkPreview"), null);
  assert.equal(get(app, "btn-bulk-confirm").disabled, true);
  assert.equal(get(app, "bulk-preview-body").children.length, 0);
  assert.equal(get(app, "bulk-affected").textContent, "1");
  assert.equal(get(app, "btn-bulk-preview").disabled, false);
});

test("a preview that returns after switching project is discarded", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  const pending = app.evalInApp("previewBulkEdit()");
  app.evalInApp("closeBulkModal(); closeProject()");
  await app.evalInApp("openProject('B')");
  app.evalInApp("openBulkModal()");
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  assert.equal(app.evalInApp("currentProject.id"), "B");
  assert.equal(app.evalInApp("bulkPreview"), null);
  assert.equal(get(app, "btn-bulk-confirm").disabled, true);
  assert.equal(get(app, "bulk-preview-body").children.length, 0);
  assert.equal(status(app), "");
});

test("another project's preview reaches the server while an apply is pending, and its dialog is released after", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  let pending = app.evalInApp("previewBulkEdit()");
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  const applying = app.evalInApp("applyBulkEdit()");
  assert.equal(applyCalls(app).length, 1);
  app.evalInApp("closeBulkModal(); closeProject()");
  await openWithTag(app, "B");
  assert.equal(get(app, "btn-bulk-preview").disabled, false, "busy is project-scoped");
  pending = app.evalInApp("previewBulkEdit()");
  assert.equal(previewCalls(app).length, 2, "B's preview must actually be sent");
  gates.previewB.resolve(previewFor("B", "B-c1"));
  await pending;
  assert.equal(get(app, "btn-bulk-confirm").disabled, false);
  await app.evalInApp("applyBulkEdit()");
  assert.equal(applyCalls(app).length, 1, "B's confirm waits for A's batch");
  assert.equal(status(app), app.evalInApp("BULK_APPLY_STATUS"));
  gates.applyA.resolve({ updated: 1, clip_ids: ["A-c1"] });
  await applying;
  assert.equal(status(app), "1 clip(s) will change");
  assert.equal(get(app, "btn-bulk-confirm").disabled, false);
  assert.equal(get(app, "btn-bulk-preview").disabled, false);
  assert.equal(app.evalInApp("currentProject.clips[0].tag_type"), "Pass", "B's clips untouched by A's success");
});

test("Confirm refuses a stored preview that belongs to another project", async () => {
  const { app } = setup();
  await openWithTag(app, "A");
  app.evalInApp("bulkPreview = { previewId: 'B-preview', projectId: 'B', count: 1, clipIds: ['B-c1'] }; document.getElementById('btn-bulk-confirm').disabled = false");
  await app.evalInApp("applyBulkEdit()");
  assert.equal(applyCalls(app).length, 0);
  assert.equal(app.evalInApp("bulkApplyPending"), null);
});

test("focus parked by the app moves to Confirm when the preview succeeds", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  get(app, "btn-bulk-preview").focus();
  const pending = app.evalInApp("previewBulkEdit()");
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel", "parked while busy");
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  assert.equal(app.document.activeElement.id, "btn-bulk-confirm");
  assert.equal(get(app, "btn-bulk-confirm").disabled, false);
});

test("focus the user moved during the request is left alone", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  get(app, "btn-bulk-preview").focus();
  const pending = app.evalInApp("previewBulkEdit()");
  key(app, "Tab", true);
  assert.equal(app.document.activeElement.id, "bulk-player");
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  assert.equal(app.document.activeElement.id, "bulk-player");
});

test("Tab that lands back on the parked control is still the user's choice", async () => {
  // Codex's finding on d829b87: Shift+Tab away then Tab back to Cancel is a
  // deliberate selection of Cancel, and the settle must not move it to Confirm.
  const { app, gates } = setup();
  await openWithTag(app, "A");
  get(app, "btn-bulk-preview").focus();
  const pending = app.evalInApp("previewBulkEdit()");
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel");
  key(app, "Tab", true);
  assert.equal(app.document.activeElement.id, "bulk-player");
  key(app, "Tab");
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel");
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel");
  assert.equal(get(app, "btn-bulk-confirm").disabled, false);
});

test("closing and reopening the dialog forgets any parked focus", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  get(app, "btn-bulk-preview").focus();
  const pending = app.evalInApp("previewBulkEdit()");
  app.evalInApp("closeBulkModal(); openBulkModal(); document.getElementById('bulk-tag-type').value = 'Goal'");
  get(app, "btn-bulk-cancel").focus();
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  assert.equal(app.document.activeElement.id, "btn-bulk-cancel");
});

test("a failed preview returns focus to Preview; a conflict moves it to Refresh", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  get(app, "btn-bulk-preview").focus();
  let pending = app.evalInApp("previewBulkEdit()");
  gates.previewA.resolve({ status: 400, body: { error: "Invalid tag type", code: "invalid_tag_type" } });
  await pending;
  assert.equal(status(app), "Invalid tag type");
  assert.equal(app.document.activeElement.id, "btn-bulk-preview");
  gates.previewA = deferred();
  pending = app.evalInApp("previewBulkEdit()");
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  get(app, "btn-bulk-confirm").focus();
  const applying = app.evalInApp("applyBulkEdit()");
  gates.applyA.resolve({ status: 409, body: { code: "stale", error: "Clips changed after preview", conflicts: [
    { id: "A-c1", reason: "modified", current: { tag_type: "Shot", players: [], label: "A pass" } }] } });
  await applying;
  assert.equal(get(app, "btn-bulk-refresh").hidden, false);
  assert.equal(app.document.activeElement.id, "btn-bulk-refresh");
  assert.match(status(app), /A pass: modified \(now Shot · no player\)/);
});

test("the preview table joins the Tab cycle only while it has rows", async () => {
  const { app, gates } = setup();
  await openWithTag(app, "A");
  get(app, "bulk-player").focus();
  key(app, "Tab");
  assert.equal(app.document.activeElement.id, "btn-bulk-preview", "empty table is skipped");
  const pending = app.evalInApp("previewBulkEdit()");
  gates.previewA.resolve(previewFor("A", "A-c1"));
  await pending;
  get(app, "bulk-player").focus();
  key(app, "Tab");
  assert.equal(app.document.activeElement.id, "bulk-preview-wrap", "rows make the scroller reachable");
  key(app, "Tab");
  assert.equal(app.document.activeElement.id, "btn-bulk-preview");
});
