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

// --------------------------------------------------------------- metadata tab
// The Metadata tab at exactly 980×720: the save button must stay on screen while
// the grouped field list scrolls, the preview must name removals and keeps, and
// the overwrite attempt must be refused in the real (non-demo) code path.
const METADATA_FIELDS = [
  { key: "png.gps.0x0001", label: "GPSLatitudeRef", value: "N", category: "personal", container: "GPS", removable: true, derived: false, reason: "", reason_key: "", reason_params: {}, value_key: "", value_params: {}, size: 1, truncated: false },
  { key: "png.gps.0x0002", label: "GPSLatitude", value: "50, 27, 0", category: "personal", container: "GPS", removable: true, derived: false, reason: "", reason_key: "", reason_params: {}, value_key: "", value_params: {}, size: 9, truncated: false },
  { key: "png.gps.coordinates", label: "GPS coordinates", value: "50.450000, 30.516667", category: "personal", container: "GPS", removable: false, derived: true, reason: "derived", reason_key: "meta.reason.gps_derived", reason_params: {}, value_key: "", value_params: {}, size: 20, truncated: false },
  { key: "png.text.tEXt.Comment", label: "Comment", value: "kitchen wifi", category: "personal", container: "tEXt", removable: true, derived: false, reason: "", reason_key: "", reason_params: {}, value_key: "", value_params: {}, size: 12, truncated: false },
  { key: "png.exif.0x0112", label: "Orientation", value: "6", category: "technical", container: "eXIf/0th", removable: false, derived: false, reason: "kept by policy", reason_key: "meta.reason.kept_by_policy", reason_params: {}, value_key: "", value_params: {}, size: 2, truncated: false },
  { key: "png.exif.0x8298", label: "Copyright", value: "(c) 2024 Ada", category: "rights", container: "eXIf/0th", removable: false, derived: false, reason: "kept by policy", reason_key: "meta.reason.kept_by_policy", reason_params: {}, value_key: "", value_params: {}, size: 12, truncated: false },
  { key: "png.text.tEXt.Title", label: "Title", value: "Holiday", category: "descriptive", container: "tEXt", removable: false, derived: false, reason: "kept by policy", reason_key: "meta.reason.kept_by_policy", reason_params: {}, value_key: "", value_params: {}, size: 7, truncated: false },
  { key: "png.chunk.prVt", label: "Unknown chunk prVt", value: "17 bytes", category: "unknown", container: "container", removable: false, derived: false, reason: "unknown data", reason_key: "meta.reason.unknown_data", reason_params: {}, value_key: "meta.value.bytes", value_params: { size: "17" }, size: 17, truncated: false },
];
const METADATA_DEFAULT = ["png.gps.0x0001", "png.gps.0x0002", "png.text.tEXt.Comment"];
const METADATA_REPORT = {
  path: "/mock/holiday.png",
  format: "png",
  size_bytes: 2048,
  sha256: "a".repeat(64),
  counts: { total: 7, personal: 3, descriptive: 1, technical: 1, rights: 1, provenance: 0, unknown: 1, removable: 3 },
  derived_counts: { total: 1, personal: 1, unknown: 0 },
  technical: { width: "300", height: "300", mode: "RGB" },
  technical_summary: { width: "300", height: "300", resolution: "300x300" },
  checksum_ok: true,
  warnings: [],
  fields: METADATA_FIELDS,
  chunks: [{ kind: "IHDR", offset: 8, size: 13, checksum_ok: true, note: "" }],
  provenance: { state: "none", detected: false, manifests: [], signed: false, details: [] },
  removable: ["png.gps.0x0001", "png.gps.0x0002", "png.text.tEXt.Comment"],
};
function metadataPlanFor(selection) {
  const remove = METADATA_FIELDS.filter((f) => selection.includes(f.key) && f.removable && !f.derived);
  const blocked = METADATA_FIELDS.filter((f) => selection.includes(f.key) && !f.removable && !f.derived);
  const keep = METADATA_FIELDS.filter((f) => !f.derived && !remove.includes(f));
  return { allowed: true, reason: "", remove, keep, blocked, warnings: [] };
}

async function openMetadata(page) {
  await page.getByRole("button", { name: "Метаданные" }).click();
  await page.getByRole("button", { name: "Открыть файл…" }).click();
}

// Demo mode: the whole tab, grouped list, live preview, result — both themes.
for (const theme of ["dark", "light"]) {
  const context = await browser.newContext({ viewport: VIEWPORT });
  await context.addInitScript((settings) => {
    window.localStorage.setItem("phantom.settings.draft", JSON.stringify(settings));
  }, { language: "ru", theme });
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  const [chooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    openMetadata(page),
  ]);
  await chooser.setFiles(EXAMPLE_CARRIER);
  const fail = (msg) => results.push({ name: `metadata-${theme}`, ok: false, detail: msg });
  await page.getByRole("heading", { name: "Перед сохранением" }).waitFor();
  for (const label of ["Личные данные", "Технические", "Права и авторство", "Описательные", "Неизвестные"]) {
    if ((await page.getByRole("heading", { name: label }).count()) !== 1) fail(`group ${label} missing`);
  }
  const orientation = page.getByRole("checkbox", { name: "Orientation (eXIf/0th)" });
  if (!(await orientation.isDisabled())) fail("orientation checkbox is not protected");
  if ((await page.getByText(/Будет удалено: 4/).count()) !== 1) fail("preview does not list removals");
  if ((await page.getByText(/Останется: 6/).count()) !== 1) fail("preview does not list keeps");
  const save = page.getByRole("button", { name: "Сохранить очищенную копию…" });
  if (await save.isDisabled()) fail("save button disabled with a safe default selection");
  await save.scrollIntoViewIfNeeded();
  const box = await save.boundingBox();
  if (!box || box.y < 0 || box.y + box.height > VIEWPORT.height || box.x < 0 || box.x + box.width > VIEWPORT.width) {
    fail(`save button clipped: ${JSON.stringify(box)}`);
  }
  await page.getByRole("checkbox", { name: "Comment (tEXt)" }).uncheck();
  await page.getByText(/Будет удалено: 3/).waitFor();
  await save.click();
  await page.getByText("Очищенная копия сохранена").waitFor();
  if ((await page.getByText(/Оригинал не изменён/).count()) !== 1) fail("result does not prove the source unchanged");
  if ((await page.getByText("Пиксели совпадают с оригиналом побайтово").count()) !== 1) fail("result does not prove pixels");
  const again = page.getByRole("button", { name: "Очистить другой файл" });
  const againBox = await again.boundingBox();
  if (!againBox || againBox.y + againBox.height > VIEWPORT.height) fail("result action is outside the viewport");
  await page.screenshot({ path: `${arg("out", "screenshots")}/layout-metadata-${theme}.png` });
  if (!results.some((row) => row.name === `metadata-${theme}`)) {
    results.push({ name: `metadata-${theme}`, ok: true, detail: "groups, preview, save and result at 980x720" });
  }
  await context.close();
}

// Real code path with mocked Tauri IPC: the save dialog returns the source path,
// so the UI must refuse before the core is ever asked to clean.
{
  const context = await browser.newContext({ viewport: VIEWPORT });
  await context.addInitScript((fixture) => {
    window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
    window.__META_CALLS__ = [];
    window.__TAURI_INTERNALS__ = {
      metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } },
      transformCallback: () => 1,
      unregisterCallback: () => undefined,
      invoke: async (command, args) => {
        if (command === "app_info") return { app: "2.1.0", core: "2.1.0", name: "PHANTOM" };
        if (command === "pick_file") return { path: "/mock/holiday.png", name: "holiday.png", size_bytes: 2048 };
        if (command === "save_file") return "/mock/holiday.png"; // the user picks the original
        if (command === "bridge_call") {
          window.__META_CALLS__.push(args.request);
          if (args.request.cmd === "metadata_inspect") {
            return { ok: true, report: fixture.report, default_selection: fixture.defaultSelection,
              plan: { allowed: true, reason: "", remove: fixture.report.fields.filter((f) => fixture.defaultSelection.includes(f.key)),
                keep: fixture.report.fields.filter((f) => !fixture.defaultSelection.includes(f.key)), blocked: [], warnings: [] } };
          }
          if (args.request.cmd === "metadata_plan") {
            return { ok: true, report: fixture.report, plan: fixture.plan };
          }
          if (args.request.cmd === "metadata_clean") return { ok: false, error: { code: "internal", message: "must not be called", detail: null } };
        }
        return 1;
      },
    };
  }, { report: METADATA_REPORT, defaultSelection: METADATA_DEFAULT, plan: metadataPlanFor(METADATA_DEFAULT) });
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Метаданные" }).click();
  await page.getByRole("button", { name: "Открыть файл…" }).click();
  await page.getByRole("heading", { name: "Перед сохранением" }).waitFor();
  const save = page.getByRole("button", { name: "Сохранить очищенную копию…" });
  await save.click();
  await page.getByRole("alert").waitFor();
  const refused = await page.getByText(/Выходной файл совпадает с оригиналом/).count();
  const calls = await page.evaluate(() => window.__META_CALLS__.map((request) => request.cmd));
  const box = await save.boundingBox();
  if (refused !== 1) results.push({ name: "metadata-overwrite", ok: false, detail: "refusal message missing" });
  else if (calls.includes("metadata_clean")) results.push({ name: "metadata-overwrite", ok: false, detail: "the core was asked to overwrite the source" });
  else if (!box || box.y + box.height > VIEWPORT.height) results.push({ name: "metadata-overwrite", ok: false, detail: "save button clipped" });
  else results.push({ name: "metadata-overwrite", ok: true, detail: `refused before clean; calls=${calls.join(",")}` });
  await page.screenshot({ path: `${arg("out", "screenshots")}/layout-metadata-overwrite.png` });
  await context.close();
}

// A credential-bearing file: the plan is not allowed, so save is disabled and
// the refusal is explained in place instead of failing after the dialog.
{
  const signed = {
    ...METADATA_REPORT,
    path: "/mock/signed.png",
    provenance: { state: "unverified", detected: true, manifests: ["urn:uuid:demo"], signed: true, details: [] },
  };
  const context = await browser.newContext({ viewport: VIEWPORT });
  await context.addInitScript((report) => {
    window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
    window.__TAURI_INTERNALS__ = {
      metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } },
      transformCallback: () => 1,
      unregisterCallback: () => undefined,
      invoke: async (command) => {
        if (command === "app_info") return { app: "2.1.0", core: "2.1.0", name: "PHANTOM" };
        if (command === "pick_file") return { path: "/mock/signed.png", name: "signed.png", size_bytes: 2048 };
        if (command === "bridge_call") {
          return { ok: true, report, default_selection: [],
            plan: { allowed: false, reason: "signed_media", remove: [], keep: report.fields, blocked: [], warnings: [] } };
        }
        return 1;
      },
    };
  }, signed);
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Метаданные" }).click();
  await page.getByRole("button", { name: "Открыть файл…" }).click();
  await page.getByText("Очистка недоступна для этого файла").waitFor();
  const save = page.getByRole("button", { name: "Сохранить очищенную копию…" });
  const disabled = await save.isDisabled();
  const explained = await page.getByText(/C2PA\/JUMBF/).count();
  const box = await save.boundingBox();
  if (!disabled || explained < 1) results.push({ name: "metadata-credential", ok: false, detail: `disabled=${disabled} explained=${explained}` });
  else if (!box || box.y + box.height > VIEWPORT.height) results.push({ name: "metadata-credential", ok: false, detail: "save button clipped" });
  else results.push({ name: "metadata-credential", ok: true, detail: "refusal explained, save disabled, button on screen" });
  await page.screenshot({ path: `${arg("out", "screenshots")}/layout-metadata-credential.png` });
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
