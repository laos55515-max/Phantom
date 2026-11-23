// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { HideScreen } from "./HideScreen";
import { tauriBridge } from "../bridge";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({ onDragDropEvent: vi.fn(async () => () => undefined) }),
}));
vi.mock("../bridge", async (importOriginal) => {
  const original = await importOriginal<typeof import("../bridge")>();
  return { ...original, bridge: original.tauriBridge, isDemoMode: () => false };
});

beforeEach(() => {
  invoke.mockReset();
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) });
});
afterEach(() => { cleanup(); window.localStorage.clear(); });

function setupPicker(size: unknown, omit = false) {
  invoke.mockImplementation(async (command, args) => {
    if (command === "pick_file") {
      if (args.kind === "secret") return { path: "/tmp/input.pdf", name: "input.pdf", ...(omit ? {} : { size_bytes: size }) };
      return { path: "/tmp/5555.png", name: "5555.png", size_bytes: 200000 };
    }
    if (command === "bridge_call" && args.request.cmd === "probe") return {
      ok: true, path: "/tmp/5555.png", name: "5555.png", kind: "image", size_bytes: 200000,
      description: "RGB", capacity_bytes: 10000, usable_bits: 80000, notices: [],
    };
    throw new Error(`Unexpected command ${command}`);
  });
}

async function chooseFiles() {
  render(<I18nProvider><HideScreen onGoExtract={() => undefined} /></I18nProvider>);
  fireEvent.click(screen.getByRole("button", { name: /^Файл/ }));
  await screen.findByText("input.pdf");
  fireEvent.click(screen.getByRole("button", { name: /Выбрать файл/ }));
  await screen.findByText("5555.png");
  fireEvent.change(screen.getByLabelText(/Пароль/), { target: { value: "valid-password-123" } });
}

describe("native picker Rust size_bytes to HideScreen", () => {
  it("allows a small PDF with a valid password", async () => {
    setupPicker(512);
    await chooseFiles();
    expect(screen.getByText("512 Б")).toBeTruthy();
    expect(screen.getByRole("progressbar")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Спрятать/ })).toHaveProperty("disabled", false);
  });
  it("shows required and available capacity for an oversized PDF", async () => {
    setupPicker(20000);
    await chooseFiles();
    expect(screen.getByText(/Нужно ~/)).toBeTruthy();
    expect(screen.getByText(/Вмещает/)).toBeTruthy();
    expect(screen.getByText(/Не вмещается/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Спрятать/ })).toHaveProperty("disabled", true);
  });
  it.each([undefined, null, -1, 1.5, NaN, Infinity, "512", Number.MAX_SAFE_INTEGER + 1])("blocks invalid size %s with an actionable error, no bar or NaN", async (size) => {
    setupPicker(size, size === undefined);
    await chooseFiles();
    expect(screen.getByText(/Не удалось определить размер файла. Выберите файл снова/)).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(document.body.textContent).not.toContain("NaN");
    expect(screen.getByRole("button", { name: /Спрятать/ })).toHaveProperty("disabled", true);
    setupPicker(512);
    fireEvent.click(screen.getByRole("button", { name: /Изменить выбор/ }));
    await waitFor(() => expect(screen.getByRole("progressbar")).toBeTruthy());
    expect(screen.getByRole("button", { name: /Спрятать/ })).toHaveProperty("disabled", false);
  });
  it("distinguishes an actual empty file from a missing size", async () => {
    setupPicker(0);
    const picked = await tauriBridge.pickFile("secret", "any");
    expect(picked?.sizeBytes).toBe(0);
    await chooseFiles();
    expect(screen.getByText("0 Б")).toBeTruthy();
    expect(screen.queryByText(/Не удалось определить размер/)).toBeNull();
  });
});
