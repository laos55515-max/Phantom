// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { HideScreen } from "./HideScreen";
import { ExtractScreen } from "./ExtractScreen";
import { bridge } from "../bridge";

const { writeText } = vi.hoisted(() => ({ writeText: vi.fn() }));
vi.mock("@tauri-apps/plugin-clipboard-manager", () => ({ writeText }));
vi.mock("@tauri-apps/api/webview", () => ({ getCurrentWebview: () => ({ onDragDropEvent: vi.fn(async () => () => undefined) }) }));
vi.mock("../bridge", async (original) => {
  const mod = await original<typeof import("../bridge")>();
  return { ...mod, isDemoMode: () => false, bridge: { ...mod.bridge, pickFile: vi.fn(), extract: vi.fn(), saveFile: vi.fn() } };
});
const text = Array.from({ length: 10000 }, (_, i) => i % 25 === 24 ? "кириллица\n\n" : "слово ").join("");
const audit = { originalSize: new TextEncoder().encode(text).length, sha256: "a".repeat(64), integrityOk: true, containerVersion: 5, kdf: "argon2id" };

beforeEach(() => {
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) });
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
  vi.mocked(bridge.pickFile).mockReset(); vi.mocked(bridge.extract).mockReset(); vi.mocked(bridge.saveFile).mockReset(); writeText.mockReset();
});
afterEach(() => { cleanup(); window.localStorage.clear(); delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__; });
function wrap(child: JSX.Element) { return render(<I18nProvider>{child}</I18nProvider>); }

it("keeps a 10,000-word draft across expand, collapse and Escape", () => {
  wrap(<HideScreen onGoExtract={() => undefined} />);
  fireEvent.click(screen.getByRole("button", { name: /^Текст/ }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Развернуть" }));
  let dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("textbox")).toHaveProperty("value", text);
  fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: text + "конец\n" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Свернуть" }));
  expect(screen.getByRole("textbox")).toHaveProperty("value", text + "конец\n");
  fireEvent.click(screen.getByRole("button", { name: "Развернуть" }));
  dialog = screen.getByRole("dialog");
  fireEvent.keyDown(within(dialog).getByRole("textbox"), { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("textbox")).toHaveProperty("value", text + "конец\n");
});

async function openResult(omitted: boolean) {
  vi.mocked(bridge.pickFile).mockResolvedValue({ path: "/tmp/container.png", name: "container.png", sizeBytes: 100 });
  vi.mocked(bridge.extract).mockImplementation(async (req) => req.mode === "info" ?
    { ok: true, kind: "text", ...(omitted ? { textOmitted: true } : { text }), ...audit } :
    { ok: true, kind: "file", suggestedName: "output.txt", ...audit });
  vi.mocked(bridge.saveFile).mockResolvedValue("/tmp/output.txt");
  wrap(<ExtractScreen />);
  fireEvent.click(screen.getByRole("button", { name: /Выбрать файл/ }));
  await screen.findByText("container.png");
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "long-password-123" } });
  fireEvent.click(screen.getByRole("button", { name: /Извлечь/ }));
  await screen.findByText("Найденный текст");
}

it("reads, copies, collapses and saves all 10,000 words", async () => {
  writeText.mockResolvedValue(undefined);
  await openResult(false);
  expect(screen.getByRole("textbox")).toHaveProperty("value", text);
  fireEvent.click(screen.getByRole("button", { name: "Развернуть" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("textbox")).toHaveProperty("value", text);
  fireEvent.click(within(dialog).getByRole("button", { name: "Копировать" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(text));
  fireEvent.click(within(dialog).getByRole("button", { name: /Сохранить как/ }));
  await waitFor(() => expect(bridge.extract).toHaveBeenCalledWith(expect.objectContaining({ mode: "save_file", outputPath: "/tmp/output.txt" })));
  fireEvent.keyDown(within(dialog).getByRole("textbox"), { key: "Escape" });
  expect(screen.getByRole("textbox")).toHaveProperty("value", text);
});

it("offers saving omitted text without displaying an empty preview", async () => {
  await openResult(true);
  expect(screen.getByText("Текст извлечён. Сохраните его как TXT, чтобы прочитать полностью")).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Развернуть" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Сохранить как/ }));
  await waitFor(() => expect(bridge.extract).toHaveBeenCalledWith(expect.objectContaining({ mode: "save_file", outputPath: "/tmp/output.txt" })));
});
