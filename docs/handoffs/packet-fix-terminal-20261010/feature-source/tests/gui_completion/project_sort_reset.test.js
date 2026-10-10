const test = require("node:test");
const assert = require("node:assert/strict");
const { loadApp } = require("../ui/load_app");

const seed = () => [
  { id: 1, name: "Charlie", clips: [], video_filename: null },
  { id: 2, name: "alpha reserve", clips: [], video_filename: null },
  { id: 3, name: "Beta Bowl", clips: [], video_filename: null },
];
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const cards = app => app.document.querySelectorAll(".project-card");
const names = app => cards(app).map(card => card.dataset.name);
const type = (app, value) => {
  const input = app.document.getElementById("project-search");
  input.value = value;
  input.dispatchEvent(app.document.createEvent("input"));
};
const fixture = (options = {}, initial = seed()) => {
  let projects = initial;
  const historyCalls = [];
  const routes = {
    "GET /api/projects": () => projects,
    "POST /api/projects": () => { projects.push({ id: 4, name: "Delta", clips: [], video_filename: null }); return {}; },
    "DELETE /api/projects/2": () => { projects = projects.filter(p => p.id !== 2); return {}; },
  };
  const history = options.history || { replaceState: (...args) => historyCalls.push(args) };
  return { app: loadApp({ ...options, history, routes }), historyCalls };
};
const clickSort = app => app.document.getElementById("btn-project-sort").click();
const clickReset = app => app.document.getElementById("btn-project-sort-reset").click();
const assertReset = app => {
  assert.equal(app.document.getElementById("projects-container").dataset.sort, "original");
  assert.equal(app.document.getElementById("project-sort-status").textContent, "Library order");
};

test("reset from ascending restores library order", async () => {
  const { app } = fixture(); await settle(); clickSort(app); clickReset(app);
  assert.deepEqual(names(app), ["charlie", "alpha reserve", "beta bowl"]);
  assertReset(app);
  assert.match(app.document.getElementById("btn-project-sort").textContent, /A→Z/);
});

test("reset from descending restores library order", async () => {
  const { app } = fixture(); await settle(); clickSort(app); clickSort(app); clickReset(app);
  assert.deepEqual(names(app), ["charlie", "alpha reserve", "beta bowl"]);
  assertReset(app);
  assert.match(app.document.getElementById("btn-project-sort").textContent, /A→Z/);
});

test("reset restores stable library ordering by data index", async () => {
  const tied = ["Delta", "delta", "DELTA", "apple"].map((name, i) => ({ id: i + 1, name, clips: [], video_filename: null }));
  const { app } = fixture({}, tied); await settle(); clickSort(app); clickReset(app);
  const ids = () => cards(app).map(card => card.querySelector(".delete-btn").dataset.id);
  assert.deepEqual(ids(), ["1", "2", "3", "4"]); assertReset(app);
  clickReset(app); assert.deepEqual(ids(), ["1", "2", "3", "4"]); assertReset(app);
});

test("repeated reset is harmless and sort restarts ascending", async () => {
  const { app } = fixture(); await settle();
  clickReset(app); const first = names(app); assertReset(app);
  clickReset(app); assert.deepEqual(names(app), first); assertReset(app);
  clickSort(app); assert.equal(app.document.getElementById("projects-container").dataset.sort, "asc");
  assert.deepEqual(names(app), ["alpha reserve", "beta bowl", "charlie"]);
  clickSort(app); assert.equal(app.document.getElementById("projects-container").dataset.sort, "desc");
});

test("reset preserves typed search hash count visibility and focus", async () => {
  const { app, historyCalls } = fixture({ location: { hash: "#q=ALP" } }); await settle(); type(app, "ALP");
  const snapshot = () => ({
    value: app.document.getElementById("project-search").value,
    hidden: Object.fromEntries(cards(app).map(card => [card.dataset.name, card.hidden])),
    count: app.document.getElementById("project-search-count").textContent,
    hash: historyCalls.at(-1),
    noMatch: app.document.getElementById("project-search-empty").hidden,
    empty: app.document.getElementById("projects-empty").hidden,
  });
  const before = snapshot(); const callCount = historyCalls.length; clickReset(app);
  assert.deepEqual(snapshot(), before); assert.equal(historyCalls.length, callCount); assertReset(app);
  assert.equal(app.document.activeElement, app.document.getElementById("project-search"));
});

test("reset makes no network request and leaves location unchanged", async () => {
  const { app } = fixture(); await settle(); const href = app.evalInApp("window.location.href");
  clickSort(app); clickReset(app); clickReset(app); assertReset(app);
  assert.equal(app.calls.fetch.length, 1);
  assert.equal(app.evalInApp("window.location.href"), href);
});
