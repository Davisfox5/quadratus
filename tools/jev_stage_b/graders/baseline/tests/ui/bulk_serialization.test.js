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
  clips: [{ id: `${id}-c1`, start: 1, end: 2, label: `${id} clip`, notes: "",
    tag_type: "Pass", players: ["p1"] }],
  tag_types: [{ name: "Pass" }, { name: "Shot" }, { name: "Goal" }],
  players: [{ id: "p1", name: "Alex" }, { id: "p2", name: "Blair" }],
});

function setup() {
  const gates = { previewA: deferred(), previewB: deferred(), applyA: deferred(), applyB: deferred() };
  const routes = {
    "GET /api/projects/A": project("A"), "GET /api/projects/B": project("B"),
    "GET /api/projects/A/filter_presets": [], "GET /api/projects/B/filter_presets": [],
    "POST /api/projects/A/clips/bulk_preview": () => gates.previewA.promise,
    "POST /api/projects/B/clips/bulk_preview": () => gates.previewB.promise,
    "POST /api/projects/A/clips/bulk_apply": () => gates.applyA.promise,
    "POST /api/projects/B/clips/bulk_apply": () => gates.applyB.promise,
  };
  return { app: loadApp({ routes }), gates };
}

async function preview(app, gate, id = "A") {
  await app.evalInApp(`openProject('${id}')`);
  app.evalInApp("openBulkModal(); document.getElementById('bulk-tag-type').value = 'Shot'; document.getElementById('bulk-player').value = 'p2'");
  const pending = app.evalInApp("previewBulkEdit()");
  gate.resolve({ count: 1, preview_id: `${id}-preview`, project_id: id, clips: [{
    id: `${id}-c1`, start: 1, end: 2, label: `${id} clip`,
    before: { tag_type: "Pass", players: ["p1"] },
    after: { tag_type: "Shot", players: ["p2"] },
  }] });
  await pending;
}

const applyCalls = app => app.calls.fetch.filter(call => call.path.endsWith("/bulk_apply"));

test("committed apply survives change events with captured values", async () => {
  const { app, gates } = setup();
  await preview(app, gates.previewA);
  const applying = app.evalInApp("applyBulkEdit()");
  app.evalInApp("document.getElementById('bulk-tag-type').value = 'Goal'; document.getElementById('bulk-player').value = '__clear__'");
  for (const id of ["bulk-tag-type", "bulk-player"]) {
    app.document.getElementById(id).dispatchEvent(app.document.createEvent("change"));
  }
  assert.equal(app.document.getElementById("btn-bulk-preview").disabled, true);
  assert.equal(app.document.getElementById("btn-bulk-confirm").disabled, true);
  gates.applyA.resolve({ updated: 1, clip_ids: ["A-c1"] });
  await applying;
  assert.equal(app.evalInApp("currentProject.clips[0].tag_type"), "Shot");
  assert.equal(app.evalInApp("currentProject.clips[0].players.join(',')"), "p2");
  assert.equal(app.document.getElementById("bulk-tag-type").value, "Goal");
  assert.equal(app.document.getElementById("bulk-player").value, "__clear__");
  assert.match(app.evalInApp("document.getElementById('bulk-status').textContent"), /updated/);
  assert.equal(app.evalInApp("bulkApplyPending"), null);
});

test("dismiss and reopen keeps same-project apply busy", async () => {
  const { app, gates } = setup();
  await preview(app, gates.previewA);
  const applying = app.evalInApp("applyBulkEdit()");
  app.evalInApp("closeBulkModal(); openBulkModal()");
  assert.equal(app.evalInApp("document.getElementById('bulk-status').textContent"), app.evalInApp("BULK_APPLY_STATUS"));
  assert.equal(app.document.getElementById("btn-bulk-preview").disabled, true);
  app.evalInApp("previewBulkEdit(); applyBulkEdit()");
  assert.equal(applyCalls(app).length, 1);
  gates.applyA.resolve({ updated: 1, clip_ids: ["A-c1"] });
  await applying;
  assert.equal(app.document.getElementById("btn-bulk-preview").disabled, false);
  assert.equal(app.evalInApp("currentProject.clips[0].tag_type"), "Shot");
  assert.equal(app.evalInApp("currentProject.clips[0].players.join(',')"), "p2");
  assert.match(app.evalInApp("document.getElementById('bulk-status').textContent"), /updated/);
  assert.equal(app.evalInApp("bulkApplyPending"), null);
});

test("another project may preview but cannot confirm during pending apply", async () => {
  const { app, gates } = setup();
  await preview(app, gates.previewA);
  const applying = app.evalInApp("applyBulkEdit()");
  await preview(app, gates.previewB, "B");
  const secondApply = app.evalInApp("applyBulkEdit()");
  assert.equal(applyCalls(app).length, 1);
  await secondApply;
  assert.equal(app.evalInApp("document.getElementById('bulk-status').textContent"), app.evalInApp("BULK_APPLY_STATUS"));
  const displayed = app.evalInApp("JSON.stringify(currentProject.clips)");
  gates.applyA.resolve({ updated: 1, clip_ids: ["A-c1"] });
  await applying;
  assert.equal(app.evalInApp("JSON.stringify(currentProject.clips)"), displayed);
  // The wait is over: B's dialog gets its own preview state back.
  assert.equal(app.evalInApp("document.getElementById('bulk-status').textContent"), "1 clip(s) will change");
  assert.equal(app.document.getElementById("btn-bulk-confirm").disabled, false);
  assert.equal(app.document.getElementById("btn-bulk-preview").disabled, false);
  assert.equal(app.evalInApp("bulkApplyPending"), null);
});
