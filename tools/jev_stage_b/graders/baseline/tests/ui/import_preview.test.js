const assert = require("node:assert/strict");
const test = require("node:test");
const { loadApp } = require("./load_app");

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

const project = {
  id: "A", name: "Project A", clips: [],
  tag_types: [{ name: "Pass" }],
  players: [{ id: "p1", name: "Alex", number: "7" }],
};
const result = {
  preview_only: true,
  summary: { total: 3, valid: 1, malformed: 1, duplicate: 1 },
  rows: [
    { line: 2, status: "valid", reasons: [], clip: {
      tag_type: "Pass", start: 1.5, end: 4, label: "<b>bench</b> & ready", players: ["p1"],
    } },
    { line: 3, status: "malformed", reasons: ["bad row"], clip: null },
    { line: 4, status: "duplicate", reasons: ["same as line 2"], clip: null },
  ],
};

function setup(previewRoute = result) {
  return loadApp({ routes: {
    "GET /api/projects/A": project,
    "GET /api/projects/A/filter_presets": [],
    "POST /api/projects/A/clips/import_preview": previewRoute,
  } });
}
async function open(app) { await app.evalInApp("openProject('A')"); }
function file() { return new Blob(["Tag Type,Start (s),End (s)\nPass,1,2"]); }
function key(app, value) {
  const event = app.document.createEvent("keydown", { key: value });
  app.document.activeElement.dispatchEvent(event);
  return event;
}

test("renders row statuses, summary, player, and escaped cell text", async () => {
  const app = setup(); await open(app);
  app.context.testFile = file();
  await app.evalInApp("runImportPreview(testFile)");

  const table = app.document.getElementById("import-preview-table");
  const rows = table.querySelectorAll("tr[data-status]");
  assert.deepEqual(rows.map(row => row.dataset.status), ["valid", "malformed", "duplicate"]);
  assert.equal(app.document.getElementById("import-preview-summary").textContent,
    "Total 3 · valid 1 · malformed 1 · duplicate 1");
  const html = table.querySelector("tbody")._html;
  assert.match(html, /&lt;b&gt;bench&lt;\/b&gt; &amp; ready/);
  assert.match(html, /#7 Alex/);
  assert.equal(table.querySelector("b"), null);
});

test("shows the empty state", async () => {
  const empty = { preview_only: true,
    summary: { total: 0, valid: 0, malformed: 0, duplicate: 0 }, rows: [] };
  const app = setup(empty); await open(app); app.context.testFile = file();
  await app.evalInApp("runImportPreview(testFile)");
  assert.equal(app.document.getElementById("import-preview-status").textContent, "No rows found");
  assert.equal(app.document.querySelectorAll("tr[data-status]").length, 0);
});

test("shows server and network errors", async () => {
  const server = setup({ status: 400, body: { error: "Nope" } });
  await open(server); server.context.testFile = file();
  await server.evalInApp("runImportPreview(testFile)");
  const serverStatus = server.document.getElementById("import-preview-status");
  assert.equal(serverStatus.textContent, "Nope");
  assert.equal(serverStatus.classList.contains("error"), true);

  const network = setup(() => { throw new Error("offline"); });
  await open(network); network.context.testFile = file();
  await network.evalInApp("runImportPreview(testFile)");
  const networkStatus = network.document.getElementById("import-preview-status");
  assert.equal(networkStatus.textContent, "Preview failed");
  assert.equal(networkStatus.classList.contains("error"), true);
});

test("clears stale results and announces loading before fetch resolves", async () => {
  const gate = deferred(), app = setup(() => gate.promise); await open(app);
  const summary = app.document.getElementById("import-preview-summary");
  const tbody = app.document.getElementById("import-preview-table").querySelector("tbody");
  const status = app.document.getElementById("import-preview-status");
  summary.textContent = "stale"; tbody.innerHTML = '<tr data-status="valid"><td>stale</td></tr>';
  status.textContent = "stale"; status.classList.add("error");
  app.context.testFile = file();
  const pending = app.evalInApp("runImportPreview(testFile)");
  assert.equal(summary.textContent, "");
  assert.equal(tbody.querySelectorAll("tr[data-status]").length, 0);
  assert.equal(status.textContent, "Checking…");
  assert.equal(status.classList.contains("error"), false);
  gate.resolve(result); await pending;
});

test("resets the file input so the same file can be selected twice", async () => {
  const app = setup(); await open(app);
  const input = app.document.getElementById("import-preview-file"), selected = file();
  input.files = [selected]; input.value = "manifest.csv";
  input.dispatchEvent(app.document.createEvent("change"));
  assert.equal(input.value, "");
  input.files = [selected]; input.value = "manifest.csv";
  input.dispatchEvent(app.document.createEvent("change"));
  assert.equal(input.value, "");
  assert.equal(app.calls.fetch.filter(call => call.path.endsWith("/import_preview")).length, 2);
});

test("M opens preview unless a modal is active; I/O/P/B remain intact", async () => {
  const app = setup(); await open(app); const body = app.document.body;
  const input = app.document.getElementById("import-preview-file");
  let pickerClicks = 0; input.addEventListener("click", () => { pickerClicks++; });
  body.focus(); assert.equal(key(app, "m").defaultPrevented, true); assert.equal(pickerClicks, 1);
  const modal = app.document.getElementById("bulk-modal"); modal.classList.add("active");
  body.focus(); key(app, "m"); assert.equal(pickerClicks, 1); modal.classList.remove("active");

  app.evalInApp("document.getElementById('game-video').currentTime = 7");
  body.focus(); key(app, "i"); assert.equal(app.evalInApp("markIn"), 7);
  app.evalInApp("document.getElementById('game-video').currentTime = 9");
  body.focus(); key(app, "o"); assert.equal(app.evalInApp("markOut"), 9);
  body.focus(); assert.equal(key(app, "p").defaultPrevented, true);
  assert.equal(app.document.activeElement.id, "filter-preset-select");
  body.focus(); assert.equal(key(app, "b").defaultPrevented, true);
  assert.equal(modal.classList.contains("active"), true);
});

test("newer upload invalidates older", async () => {
  const firstGate = deferred(), secondGate = deferred();
  const gates = [firstGate, secondGate];
  const app = setup(() => gates.shift().promise); await open(app);
  const tbody = app.document.getElementById("import-preview-table").querySelector("tbody");
  const summary = app.document.getElementById("import-preview-summary");
  const fiveRows = {
    ...result,
    summary: { total: 5, valid: 5, malformed: 0, duplicate: 0 },
    rows: Array.from({ length: 5 }, (_, index) => ({
      line: index + 2, status: "valid", reasons: [], clip: result.rows[0].clip,
    })),
  };
  app.context.testFile = file();
  const first = app.evalInApp("runImportPreview(testFile)");
  const second = app.evalInApp("runImportPreview(testFile)");

  firstGate.resolve(fiveRows); await first;
  assert.equal(tbody.querySelectorAll("tr[data-status]").length, 0);
  assert.equal(summary.textContent, "");
  secondGate.resolve(result); await second;
  assert.equal(tbody.querySelectorAll("tr[data-status]").length, 3);
  assert.equal(summary.textContent, "Total 3 · valid 1 · malformed 1 · duplicate 1");
});

test("close while pending leaves no rows or status and restores focus", async () => {
  const gate = deferred(), app = setup(() => gate.promise); await open(app);
  app.context.testFile = file();
  const pending = app.evalInApp("runImportPreview(testFile)");
  app.evalInApp("closeImportPreview()");
  gate.resolve(result); await pending;

  const status = app.document.getElementById("import-preview-status");
  const tbody = app.document.getElementById("import-preview-table").querySelector("tbody");
  assert.equal(app.document.getElementById("import-preview").hidden, true);
  assert.equal(tbody.querySelectorAll("tr[data-status]").length, 0);
  assert.equal(status.textContent, "");
  assert.equal(status.classList.contains("error"), false);
  assert.equal(app.document.activeElement.id, "btn-import-preview");
});

test("project switch while pending ignores old success and failure", async () => {
  const successGate = deferred(), failureGate = deferred();
  const gates = [successGate, failureGate];
  const app = setup(() => gates.shift().promise); await open(app);
  const status = app.document.getElementById("import-preview-status");
  const tbody = app.document.getElementById("import-preview-table").querySelector("tbody");
  const rows = () => tbody.querySelectorAll("tr[data-status]").length;

  app.context.testFile = file();
  const success = app.evalInApp("runImportPreview(testFile)");
  app.document.getElementById("btn-back").click(); await open(app);
  successGate.resolve(result); await success;
  assert.equal(rows(), 0);
  assert.equal(status.textContent, "");

  const failure = app.evalInApp("runImportPreview(testFile)");
  app.document.getElementById("btn-back").click(); await open(app);
  failureGate.resolve({ status: 500, body: { error: "late" } }); await failure;
  assert.equal(rows(), 0);
  assert.equal(status.textContent, "");
  assert.equal(status.classList.contains("error"), false);
});
