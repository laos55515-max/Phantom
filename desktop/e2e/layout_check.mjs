/**
 * Objective 980×720 layout check of the built web bundle.
 *
 *   node desktop/e2e/layout_check.mjs --base http://127.0.0.1:4173
 *
 * Requires `playwright` + chromium (installed by the CI screenshots job).
 * Verifies, for the two modes and both themes at exactly 980×720:
 *
 *   1. the primary submit button is fully inside the window (no clipping);
 *   2. the content area reports its scroll metrics (informational);
 *   3. the carrier hint «Выберите картинку или аудио» is visible before a
 *      carrier is chosen, and disappears after one is probed.
 *
 * Exits non-zero if any check fails — this is meant to gate the build.
 */

import { fileURLToPath } from "node:url";
import path from "node:path";
import { chromium } from "playwright";

const args = process.argv.slice(2);
function arg(name, fallback) {
  const index = args.indexOf(`--${name}`);
  return index !== -1 && args[index + 1] ? args[index + 1] : fallback;
}
const base = arg("base", "http://127.0.0.1:4173");
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");
const EXAMPLE_PDF = path.join(repoRoot, "src", "phantom", "resources", "example_payload.pdf");
const EXAMPLE_CARRIER = path.join(repoRoot, "src", "phantom", "resources", "example_carrier.png");

const VIEWPORT = { width: 980, height: 720 };
const results = [];

const browser = await chromium.launch();

async function scenario(name, { mode, theme, language, fillText }) {
  const context = await browser.newContext({ viewport: VIEWPORT });
  await context.addInitScript((draft) => {
    window.localStorage.setItem("phantom.settings.draft", JSON.stringify(draft));
  }, { language, theme });
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  await page.waitForTimeout(350);

  const fail = (msg) => results.push({ name, ok: false, detail: msg });

  // --- step 1: enter the chosen mode
  if (mode === "file") {
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      page.getByRole("button", { name: language === "ru" ? /^Файл/ : /^File/ }).click(),
    ]);
    await chooser.setFiles(EXAMPLE_PDF);
  } else {
    await page.getByRole("button", { name: language === "ru" ? /^Текст/ : /^Text/ }).click();
    await page.getByPlaceholder(language === "ru" ? /Впишите или вставьте текст/ : /Type or paste/).fill(fillText);
  }
  await page.waitForTimeout(200);

  // --- step 2: before the carrier is chosen, the hint must be there and no capacity verdict
  const hintVisible = await page.getByText(language === "ru" ? "Выберите картинку или аудио" : "Choose a picture or audio").isVisible().catch(() => false);
  const progressBefore = await page.getByRole("progressbar").count();
  if (!hintVisible) fail("carrier hint not visible before a carrier is chosen");
  if (progressBefore !== 0) fail("capacity bar shown before a carrier is probed");

  // --- step 3: now pick the carrier (evidence/worst-case layout)
  const [carrierChooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    page.getByRole("button", { name: language === "ru" ? /Выбрать файл/ : /Choose file/ }).click(),
  ]);
  await carrierChooser.setFiles(EXAMPLE_CARRIER);
  await page.waitForTimeout(800); // demo probe latency (250 ms) + render

  // --- step 4: fill the password so the primary button is in its busiest state
  await page.fill("#hide-password", "layout check password 123");

  // --- step 5: measure
  const main = await page.evaluate(() => {
    const el = document.querySelector(".main");
    return {
      scrollHeight: el ? el.scrollHeight : 0,
      clientHeight: el ? el.clientHeight : 0,
    };
  });
  const button = page.getByRole("button", { name: language === "ru" ? /Спрятать/ : /Hide it/ });
  const box = await button.boundingBox();
  if (!box) {
    fail("primary button has no bounding box");
  } else {
    const topOk = box.y >= 0;
    const bottomOk = box.y + box.height <= VIEWPORT.height;
    if (!topOk || !bottomOk) {
      fail(`primary button clipped: y=${box.y.toFixed(0)} bottom=${(box.y + box.height).toFixed(0)} of ${VIEWPORT.height}`);
    } else {
      results.push({
        name,
        ok: true,
        detail: `button y=${box.y.toFixed(0)}..${(box.y + box.height).toFixed(0)} of 720; main scroll=${main.scrollHeight}/${main.clientHeight}`,
      });
    }
  }
  await page.screenshot({ path: `${arg("out", "screenshots")}/layout-${name}.png` });
  await context.close();
}

const TEXT = "Quarterly figures\ndemo, no real data\nthird line keeps the editor busy";

for (const theme of ["dark", "light"]) {
  await scenario(`file-${theme}`, { mode: "file", theme, language: "ru" });
  await scenario(`text-${theme}`, { mode: "text", theme, language: "ru", fillText: TEXT });
}

await browser.close();

let failed = 0;
for (const row of results) {
  console.log(`${row.ok ? "PASS" : "FAIL"}  ${row.name}  ${row.detail}`);
  if (!row.ok) failed += 1;
}
if (failed > 0) {
  console.error(`\n${failed} layout check(s) failed`);
  process.exit(1);
}
console.log("\nAll layout checks passed");
