#!/usr/bin/env node
// Held-out real-browser check for the import preview UI. Reuses the solver
// tree's tests/browser/server.py (present since GameTape ecaef0d) so the
// fixtures reset the same way. Requires Playwright with Chromium.
//   GAMETAPE_ROOT=/path/to/solver/tree node run_browser.js
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const ROOT = path.resolve(process.env.GAMETAPE_ROOT);
const FIXTURES = path.join(__dirname, "fixtures");

function loadPlaywright() {
  for (const c of ["playwright", path.join(path.dirname(process.execPath), "..", "lib", "node_modules", "playwright")]) {
    try { return require(c); } catch (err) { if (err.code !== "MODULE_NOT_FOUND") throw err; }
  }
  return null;
}

function startServer() {
  return new Promise((resolve, reject) => {
    const proc = spawn(process.env.GAMETAPE_PYTHON || "python3", [path.join(ROOT, "tests/browser/server.py")],
      { env: { ...process.env, GAMETAPE_ROOT: ROOT }, stdio: ["ignore", "pipe", "pipe"] });
    let buf = "", err = "";
    proc.stdout.on("data", c => { buf += c; const l = buf.split("\n").find(x => x.startsWith("{")); if (l) resolve({ proc, info: JSON.parse(l) }); });
    proc.stderr.on("data", c => { err += c; });
    proc.on("exit", code => reject(new Error(`server exited ${code}\n${err}`)));
  });
}

async function main() {
  const pw = loadPlaywright();
  if (!pw) { console.error("FAIL: playwright not installed"); process.exit(1); }
  const { proc, info } = await startServer();
  const passed = [], failed = [];
  const check = (ok, label) => (ok ? passed : failed).push(label);
  const launch = process.env.GAMETAPE_CHROMIUM ? { executablePath: process.env.GAMETAPE_CHROMIUM } : {};
  let browser = null;
  try {
    browser = await pw.chromium.launch(launch);
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", e => errors.push(String(e)));
    await page.goto(info.url);
    await page.getByText("Bulk QA Match", { exact: true }).click();
    const button = page.locator("#btn-import-preview");
    check(await button.waitFor({ timeout: 5000 }).then(() => true).catch(() => false), "import preview button exists");
    check(await page.getByRole("button", { name: "Import preview", exact: true }).waitFor({ timeout: 5000 }).then(() => true).catch(() => false), "button has the accessible name");
    // Keyboard reachability: focus the button by Tab from the clips filter.
    await page.getByLabel("Search clips").focus();
    let reached = false;
    for (let i = 0; i < 25 && !reached; i++) {
      await page.keyboard.press("Tab");
      reached = await page.evaluate(() => document.activeElement && document.activeElement.id === "btn-import-preview");
    }
    check(reached, "button reachable by Tab");
    await page.keyboard.press("Enter");
    const fileInput = page.locator("#import-preview-file");
    check(await fileInput.count() === 1, "file input exists");
    await fileInput.setInputFiles(path.join(FIXTURES, "mixed.csv"));
    const table = page.locator("#import-preview-table");
    // The table element may exist before results arrive; wait for the rows.
    await page.locator("#import-preview-table tr[data-status]").first().waitFor({ timeout: 10000 });
    await page.waitForFunction(() => document.querySelectorAll("#import-preview-table tr[data-status]").length >= 10, null, { timeout: 10000 }).catch(() => {});
    const rows = page.locator("#import-preview-table tr[data-status]");
    check(await rows.count() === 10, "one table row per data row");
    check(await page.locator('#import-preview-table tr[data-status="valid"]').count() === 2, "valid rows marked");
    check(await page.locator('#import-preview-table tr[data-status="malformed"]').count() === 7, "malformed rows marked");
    check(await page.locator('#import-preview-table tr[data-status="duplicate"]').count() === 1, "duplicate rows marked");
    const summary = await page.locator("#import-preview-summary").innerText();
    check(/2/.test(summary) && /7/.test(summary) && /1/.test(summary), "summary shows the counts");
    const tableText = await table.innerText();
    check(tableText.includes("<script>alert(1)</script>"), "hostile label is displayed as text");
    check(await page.locator("#import-preview-table script").count() === 0, "hostile label is not interpreted as markup");
    check(tableText.includes("😀") && tableText.includes("RTL"), "unicode label survives");
    const inDialog = await page.evaluate(() => !!document.querySelector("#import-preview-table")?.closest("[role=dialog], .modal-overlay.active"));
    // Keyboard operability: every reason is reachable; Escape closes a dialog if used.
    const focusableInside = await page.evaluate(() => {
      const table = document.querySelector("#import-preview-table");
      const root = table.closest("[role=dialog]") || table.closest(".modal") || table.closest("section") || table.parentElement;
      const selector = "button, [tabindex], select, input, a[href]";
      return root.querySelectorAll(selector).length + (root.matches(selector) ? 1 : 0);
    });
    check(focusableInside > 0, "results region has keyboard-operable controls");
    if (inDialog) {
      await page.keyboard.press("Escape");
      check(!(await page.locator("#import-preview-table").isVisible()), "Escape dismisses the results dialog");
    }
    const projectAfter = await (await page.request.get(`${info.url}/api/projects`)).json();
    const a = projectAfter.find(p => p.name === "Bulk QA Match");
    check(a.clips.length === 3, "preview wrote nothing");
    check(errors.length === 0, "no page errors");
    check(await page.locator('[role="status"]').count() >= 1, "a live status region exists");
  } catch (err) {
    failed.push(`exception: ${String(err && err.message || err).split("\n")[0]}`);
  } finally {
    if (browser) await browser.close();
    proc.kill("SIGTERM");
  }
  console.log(JSON.stringify({ passed, failed }, null, 2));
  process.exit(failed.length ? 1 : 0);
}

main();
