const test = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");

function loadPlaywright() {
  const attempted = [];
  for (const name of ["playwright", "/usr/local/lib/node_modules/playwright", "/opt/homebrew/lib/node_modules/playwright"]) {
    attempted.push(name);
    try { return { playwright: require(name), attempted }; } catch (error) {
      if (error.code !== "MODULE_NOT_FOUND") throw error;
    }
  }
  return { playwright: null, attempted };
}

function startPreview() {
  const python = process.env.GAMETAPE_PYTHON || "python3";
  const child = spawn(python, ["diagnostic/preview.py", ROOT, "diagnostic/seed.json"], {
    cwd: ROOT, stdio: ["ignore", "pipe", "pipe"],
  });
  return new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error(`preview timeout: ${output}`)), 10000);
    const receive = (chunk) => {
      output += chunk;
      const match = output.match(/https?:\/\/127\.0\.0\.1:\d+/);
      if (match) { clearTimeout(timer); resolve({ child, url: match[0] }); }
    };
    child.stdout.on("data", receive);
    child.stderr.on("data", receive);
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("exit", (code) => {
      if (code !== null && !output.match(/https?:\/\/127\.0\.0\.1:\d+/)) {
        clearTimeout(timer); reject(new Error(`preview exited ${code}: ${output}`));
      }
    });
  });
}

async function state(page) {
  return page.evaluate(() => {
    const cards = [...document.querySelectorAll(".project-card")];
    const reset = document.querySelector("#btn-project-sort-reset").getBoundingClientRect();
    return {
      value: document.querySelector("#project-search").value,
      count: document.querySelector("#project-search-count").textContent,
      hash: location.hash,
      sort: document.querySelector("#projects-container").dataset.sort,
      status: document.querySelector("#project-sort-status").textContent,
      hidden: cards.filter((card) => card.hidden).map((card) => card.dataset.name),
      visible: cards.filter((card) => !card.hidden).map((card) => card.dataset.name),
      focused: document.activeElement?.id,
      searchEmptyHidden: document.querySelector("#project-search-empty").hidden,
      projectsEmptyHidden: document.querySelector("#projects-empty").hidden,
      noOverflow: document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth,
      resetRect: { width: reset.width, height: reset.height },
    };
  });
}

async function reviewResetViewport(t, width, height) {
  const { playwright, attempted } = loadPlaywright();
  const executablePath = process.env.GAMETAPE_CHROMIUM;
  if (!playwright) {
    t.skip(`playwright unavailable; attempted ${attempted.join(", ")}; GAMETAPE_CHROMIUM=${executablePath || "unset"}`);
    return;
  }
  let preview;
  let browser;
  try {
    try {
      browser = await playwright.chromium.launch(executablePath ? { executablePath } : {});
    } catch (error) {
      t.skip(`chromium launch failed: ${error.message}; attempted ${attempted.join(", ")}; GAMETAPE_CHROMIUM=${executablePath || "unset"}`);
      return;
    }
    preview = await startPreview();
    const page = await browser.newPage({ viewport: { width, height } });
    const errors = [];
    page.on("console", (message) => { if (message.type() === "error") errors.push(`console: ${message.text()}`); });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
    await page.goto(`${preview.url}/`);
    await page.waitForSelector(".project-card:nth-child(3)");
    await page.click("#project-search");
    await page.keyboard.type("alp");
    await page.click("#btn-project-sort");
    assert.deepEqual(await state(page), { ...(await state(page)), value: "alp", hash: "#q=alp", sort: "asc", visible: ["alpha cup", "alpha reserve"] });

    const expected = {
      value: "alp", count: "2 of 3 projects", hash: "#q=alp", sort: "original", status: "Library order",
      hidden: ["beta bowl"], visible: ["alpha cup", "alpha reserve"], focused: "project-search",
      searchEmptyHidden: true, projectsEmptyHidden: true, noOverflow: true,
    };
    for (let repeat = 0; repeat < 2; repeat += 1) {
      await page.click("#btn-project-sort-reset");
      const actual = await state(page);
      assert.deepEqual({ ...actual, resetRect: undefined }, { ...expected, resetRect: undefined });
      assert.ok(actual.resetRect.width > 0 && actual.resetRect.height > 0);
    }
    await page.click("#btn-project-sort");
    const final = await state(page);
    assert.equal(final.sort, "asc");
    assert.equal(final.value, "alp");
    assert.equal(final.hash, "#q=alp");
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    if (preview?.child) preview.child.kill();
  }
}

test("restore-order browser review at 1280x800", (t) => reviewResetViewport(t, 1280, 800));
test("restore-order browser review at 390x844", (t) => reviewResetViewport(t, 390, 844));
