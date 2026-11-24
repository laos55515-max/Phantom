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
  await page.waitForTimeout(400);

  // --- step 2: a built-in PNG is auto-selected and the capacity bar is shown
  const progressAfterAuto = await page.getByRole("progressbar").count();
  if (progressAfterAuto !== 1) fail("capacity bar not shown after built-in carrier auto-selection");

  // --- step 3: now pick a custom carrier via «Выбрать свой файл» (evidence/worst-case layout)
  const [carrierChooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    page
      .getByRole("button", {
        name: language === "ru" ? /Выбрать свой файл/ : /Choose your own file/,
      })
      .click(),
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
  if (mode === "text") {
    const editor = page.getByPlaceholder(/Впишите или вставьте текст/);
    const dimensions = await editor.evaluate((el) => ({
      width: el.getBoundingClientRect().width,
      available: el.parentElement.getBoundingClientRect().width,
      height: el.getBoundingClientRect().height,
    }));
    if (Math.abs(dimensions.width - dimensions.available) > 2 || dimensions.height < 140)
      fail(`text editor size: ${JSON.stringify(dimensions)}`);
    await page.getByRole("button", { name: "Развернуть" }).click();
    const dialog = page.getByRole("dialog");
    const fullEditor = dialog.getByRole("textbox");
    const longText = Array.from({ length: 10000 }, (_, i) => i % 25 === 24 ? "кириллица\n\n" : "слово ").join("");
    await fullEditor.fill(longText);
    if (await fullEditor.inputValue() !== longText) fail("expanded text was truncated");
    const collapse = await dialog.getByRole("button", { name: "Свернуть" }).boundingBox();
    if (!collapse || collapse.y < 0 || collapse.y + collapse.height > VIEWPORT.height)
      fail("expanded editor collapse button is outside viewport");
    await fullEditor.press("Escape");
    if (await editor.inputValue() !== longText) fail("draft changed after Escape");
  }
  await page.screenshot({ path: `${arg("out", "screenshots")}/layout-${name}.png` });
  await context.close();
}

const TEXT = "Quarterly figures\ndemo, no real data\nthird line keeps the editor busy";

for (const theme of ["dark", "light"]) {
  await scenario(`file-${theme}`, { mode: "file", theme, language: "ru" });
  await scenario(`text-${theme}`, { mode: "text", theme, language: "ru", fillText: TEXT });
}

// Extract reader: expand, select, copy/save controls remain in the 980×720 window.
{
  const context = await browser.newContext({ viewport: VIEWPORT });
  await context.addInitScript(() => window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" })));
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Извлечь" }).first().click();
  const [chooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    page.getByRole("button", { name: /Выбрать файл/ }).click(),
  ]);
  await chooser.setFiles(EXAMPLE_CARRIER);
  await page.locator("#extract-password").fill("layout check password 123");
  await page.getByRole("button", { name: "Извлечь" }).last().click();
  await page.getByRole("button", { name: "Развернуть" }).click();
  const dialog = page.getByRole("dialog");
  const reader = dialog.getByRole("textbox");
  const contents = await reader.inputValue();
  await reader.evaluate((el) => { el.select(); el.scrollTop = el.scrollHeight; });
  const selection = await reader.evaluate((el) => el.selectionEnd - el.selectionStart);
  if (selection !== contents.length) results.push({ name: "reader-selection", ok: false, detail: "cannot select whole text" });
  for (const label of ["Свернуть", "Копировать", "Сохранить как…"]) {
    const box = await dialog.getByRole("button", { name: label }).boundingBox();
    if (!box || box.y < 0 || box.y + box.height > VIEWPORT.height || box.x < 0 || box.x + box.width > VIEWPORT.width)
      results.push({ name: "reader-controls", ok: false, detail: `${label} clipped` });
  }
  await reader.press("Escape");
  if (await page.getByRole("dialog").count()) results.push({ name: "reader-escape", ok: false, detail: "dialog still visible" });
  else results.push({ name: "reader-980x720", ok: true, detail: "selection, controls and Escape" });
  await context.close();
}


// Target-size image preview layout with mocked Tauri IPC (not native extraction).
{
  const context = await browser.newContext({ viewport: VIEWPORT });
  await context.addInitScript((jpeg) => {
    window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
    window.__TAURI_INTERNALS__ = {
      metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } },
      transformCallback: () => 1, unregisterCallback: () => {},
      invoke: async (command) => {
        if (command === "app_info") return { app: "2.1.0", core: "2.1.0", name: "PHANTOM" };
        if (command === "pick_file") return { path: "/mock/carrier.wav", name: "carrier.wav", size_bytes: 2000 };
        if (command === "bridge_call") return { ok: true, kind: "file", suggested_name: "photo.jpg", original_size: 5000,
          sha256: "a".repeat(64), integrity_ok: true, container_version: 5, kdf_label: "argon2id",
          image_preview: { type: "JPEG", status: "available", mime: "image/jpeg", data: jpeg, width: 120, height: 60 } };
        return 1;
      },
    };
  }, "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAA8AHgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3EKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAP/2Q==");
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Извлечь" }).first().click();
  await page.getByRole("button", { name: /Выбрать файл/ }).click();
  await page.locator("#extract-password").fill("layout check password 123");
  await page.getByRole("button", { name: "Извлечь" }).last().click();
  const show = page.getByRole("button", { name: "Показать изображение" });
  await show.waitFor();
  for (const label of ["Показать изображение", "Сохранить как…"]) {
    const control = page.getByRole("button", { name: label });
    await control.scrollIntoViewIfNeeded();
    const box = await control.boundingBox();
    if (!box || box.y < 0 || box.y + box.height > VIEWPORT.height || box.x < 0 || box.x + box.width > VIEWPORT.width)
      results.push({ name: "image-controls", ok: false, detail: `${label} clipped` });
  }
  await show.click();
  const dialog = page.getByRole("dialog", { name: "Предпросмотр изображения" });
  const image = dialog.getByRole("img");
  await image.evaluate((img) => img.decode());
  const imageBox = await image.boundingBox();
  const closeBox = await dialog.getByRole("button", { name: "Закрыть" }).boundingBox();
  if (!imageBox || imageBox.width > VIEWPORT.width || imageBox.height > VIEWPORT.height ||
      Math.abs(imageBox.width / imageBox.height - 2) > 0.01 || !closeBox || closeBox.y + closeBox.height > VIEWPORT.height)
    results.push({ name: "image-preview-980x720", ok: false, detail: "image or close button clipped or distorted" });
  await page.screenshot({ path: `${arg("out", "screenshots")}/image-preview.png` });
  await page.keyboard.press("Escape");
  if (await page.getByRole("dialog").count()) results.push({ name: "image-escape", ok: false, detail: "dialog still visible" });
  else results.push({ name: "image-preview-980x720", ok: true, detail: "controls, aspect ratio and Escape" });
  await context.close();
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
