/**
 * Interface screenshots of the built web bundle (980×720 — the app window size).
 *
 *   node desktop/e2e/screenshots.mjs --base http://127.0.0.1:4173 --out screenshots/
 *
 * Requires `playwright` and its chromium (installed by the CI screenshot job,
 * not a dependency of the app).  Fresh contexts start at the first-launch
 * language picker; seeded contexts skip it via the same localStorage draft the
 * app writes (`phantom.settings.draft`).
 */

import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const args = process.argv.slice(2);
function arg(name, fallback) {
  const index = args.indexOf(`--${name}`);
  return index !== -1 && args[index + 1] ? args[index + 1] : fallback;
}

const base = arg("base", "http://127.0.0.1:4173");
const outDir = arg("out", "screenshots");
mkdirSync(outDir, { recursive: true });

const VIEWPORT = { width: 980, height: 720 };
const shots = [];

const repoRoot = new URL("../..", import.meta.url).pathname;
const browser = await chromium.launch();

async function shoot(name, seed, navigate) {
  const context = await browser.newContext({ viewport: VIEWPORT });
  if (seed) {
    await context.addInitScript((settings) => {
      window.localStorage.setItem("phantom.settings.draft", JSON.stringify(settings));
    }, seed);
  }
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  await page.waitForTimeout(450);
  if (navigate) await navigate(page);
  await page.waitForTimeout(250);
  const path = `${outDir}/${name}.png`;
  await page.screenshot({ path, fullPage: false });
  shots.push(path);
  console.log("wrote", path);
  await context.close();
}

// 1) First launch — the language question, neutral on purpose.
await shoot("01-first-launch", null);

// 2) Hide — the question, dark theme, Russian shell (pre-seeded draft).
await shoot("02-hide-question-dark", { language: "ru", theme: "dark" });

// 3) Hide — the example flow: both cards filled in, dark.
await shoot("03-hide-example-dark", { language: "ru", theme: "dark" }, async (page) => {
  await page.getByRole("button", { name: /Попробовать на примере/ }).click();
  await page.waitForTimeout(400);
});

// 4) Hide — text chosen, showing the editor pane, light theme.
await shoot("04-hide-text-light", { language: "en", theme: "light" }, async (page) => {
  await page.getByRole("button", { name: /^Text/ }).click();
  await page.waitForTimeout(200);
  await page.getByPlaceholder(/Type or paste/).fill(
    "Quarterly report draft — demo only, no real data.\nNot for distribution.",
  );
});

// 5) Extract screen.
await shoot("05-extract-dark", { language: "ru", theme: "dark" }, async (page) => {
  await page.getByRole("button", { name: "Извлечь" }).first().click();
});

// 6) Settings — language applies instantly, draft saved.
await shoot("06-settings-uk", { language: "uk", theme: "dark" }, async (page) => {
  await page.getByRole("button", { name: "Налаштування" }).click();
});

// 7) Settings — light theme, English.
await shoot("07-settings-light-en", { language: "en", theme: "light" }, async (page) => {
  await page.getByRole("button", { name: "Settings" }).click();
});

// 8–11) The CTA at 980×720 in both modes and both themes.
for (const theme of ["dark", "light"]) {
  await shoot(`08-hide-file-${theme}`, { language: "ru", theme }, async (page) => {
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      page.getByRole("button", { name: /^Файл/ }).click(),
    ]);
    await chooser.setFiles(`${repoRoot}/src/phantom/resources/example_payload.pdf`);
    const [carrierChooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      page.getByRole("button", { name: /Выбрать файл/ }).click(),
    ]);
    await carrierChooser.setFiles(`${repoRoot}/src/phantom/resources/example_carrier.png`);
    await page.waitForTimeout(600);
    await page.fill("#hide-password", "screenshot password 1234");
  });
  await shoot(`09-hide-text-${theme}`, { language: "ru", theme }, async (page) => {
    await page.getByRole("button", { name: /^Текст/ }).click();
    await page.getByPlaceholder(/Впишите или вставьте текст/).fill(
      "Квартальный отчёт — демо\nбез реальных данных\nтретья строка для проверки компоновки",
    );
    const [carrierChooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      page.getByRole("button", { name: /Выбрать файл/ }).click(),
    ]);
    await carrierChooser.setFiles(`${repoRoot}/src/phantom/resources/example_carrier.png`);
    await page.waitForTimeout(600);
    await page.fill("#hide-password", "screenshot password 1234");
  });
}

await browser.close();
console.log(`\n${shots.length} screenshots → ${outDir}`);
