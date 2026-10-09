// Loads the real template and app in a fresh VM. Route values are JSON bodies or
// { status, body }; unknown routes return 404 JSON. Await two microtask turns
// after loadApp() when initialization results must be rendered.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createDocument } = require("./fake_dom");

function loadApp(options = {}) {
  const root = path.resolve(__dirname, "../..");
  const template = fs.readFileSync(path.join(root, "templates/index.html"), "utf8");
  const body = template.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  if (!body) throw new Error("templates/index.html has no body");

  const document = createDocument();
  document.body.innerHTML = body[1].replace(/<script\b[^>]*>[\s\S]*?<\/script>\s*/gi, "");
  const calls = { alert: [], confirm: [], prompt: [], fetch: [] };
  const alert = (...args) => { calls.alert.push(args); };
  const confirm = (...args) => {
    calls.confirm.push(args);
    return options.confirmResult ?? true;
  };
  const prompt = (...args) => {
    calls.prompt.push(args);
    return options.promptResult ?? "";
  };
  const routes = { "GET /api/projects": [], ...(options.routes || {}) };
  const fetch = async (input, init = {}) => {
    const method = String(init.method || "GET").toUpperCase();
    const parsed = new URL(String(input), "http://app.test");
    const call = { method, path: parsed.pathname + parsed.search, input, init };
    calls.fetch.push(call);
    if (options.fetch) return options.fetch(input, init);
    const route = routes[`${method} ${call.path}`];
    const value = route === undefined
      ? { status: 404, body: { error: "Route not stubbed" } }
      : typeof route === "function" ? await route(call) : route;
    const response = value && Object.hasOwn(value, "body") ? value : { body: value };
    const status = response.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: () => response.body,
      blob: () => new Blob([JSON.stringify(response.body ?? null)]),
    };
  };
  const noop = () => {};
  const window = { location: { href: "" }, document };
  const context = vm.createContext({
    document, window, console, setTimeout, clearTimeout, URL, URLSearchParams, FormData,
    alert, confirm, prompt, fetch,
    initAnnotations: noop, initRecording: noop, toggleFreezeFrame: noop,
    tickAnnotations: noop, onAnnMouseDown: noop, onAnnMouseMove: noop, onAnnMouseUp: noop,
    exitAnnotationMode: noop, enterAnnotationMode: noop, clearAllAnnotations: noop,
    isRecording: () => false, startRecording: async () => {}, stopRecording: async () => null,
    isRecordingSupported: () => true, getRecordingDuration: () => 0,
    annList: [], annClip: null, annActive: false, annTool: "pen", annColor: "#f00",
    annLineWidth: 3, annFreezeDuration: 2,
  });
  const appPath = path.resolve(root, options.appPath || process.env.APP_JS_PATH || "static/js/app.js");
  vm.runInContext(fs.readFileSync(appPath, "utf8"), context, { filename: appPath });
  const evalInApp = expr => vm.runInContext(expr, context);
  return { document, context, calls, evalInApp, fake: { alert, confirm, fetch } };
}

module.exports = { loadApp };
