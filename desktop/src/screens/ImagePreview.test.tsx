// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { ExtractScreen } from "./ExtractScreen";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/webview", () => ({ getCurrentWebview: () => ({ onDragDropEvent: vi.fn(async () => () => undefined) }) }));
vi.mock("../bridge", async (original) => {
  const mod = await original<typeof import("../bridge")>();
  return { ...mod, bridge: mod.tauriBridge, isDemoMode: () => false };
});
const audit = { original_size: 1234, sha256: "a".repeat(64), integrity_ok: true, container_version: 5, kdf_label: "argon2id" };
const preview = { type: "JPEG", status: "available", mime: "image/jpeg", data: "YWJj", width: 100, height: 50 };
beforeEach(() => {
  invoke.mockReset();
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) });
});
afterEach(() => { cleanup(); window.localStorage.clear(); });
async function extract(imagePreview: unknown) {
  invoke.mockImplementation(async (command, args) => {
    if (command === "pick_file") return { path: "/tmp/carrier.wav", name: "carrier.wav", size_bytes: 12345 };
    if (command === "save_file") return "/tmp/saved.jpg";
    if (command === "bridge_call") return { ok: true, kind: "file", suggested_name: "image.jpg", image_preview: imagePreview, ...audit };
    throw Error(`Unexpected ${command} ${JSON.stringify(args)}`);
  });
  render(<I18nProvider><ExtractScreen /></I18nProvider>);
  fireEvent.click(screen.getByRole("button", { name: /Выбрать файл/ }));
  await screen.findByText("carrier.wav");
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "valid-password-123" } });
  fireEvent.click(screen.getByRole("button", { name: /Извлечь/ }));
  await screen.findByText("image.jpg");
}
it("shows type, name and size, opens preview and closes with Escape while saving original through bridge", async () => {
  await extract(preview);
  expect(screen.getByText(/JPEG/)).toBeTruthy();
  expect(screen.getByText(/JPEG · 1.2 КБ/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Показать изображение" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("img")).toHaveProperty("src", "data:image/jpeg;base64,YWJj");
  fireEvent.keyDown(within(dialog).getByRole("button", { name: "Закрыть" }), { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Сохранить как/ }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("bridge_call", { request: expect.objectContaining({ cmd: "extract", mode: "save_file", output_path: "/tmp/saved.jpg" }) }));
});
it.each(["size", "decode"])("leaves save enabled when preview is unavailable: %s", async (reason) => {
  await extract({ type: "PNG", status: "unavailable", reason });
  expect(screen.queryByRole("button", { name: "Показать изображение" })).toBeNull();
  expect(screen.getByRole("status").textContent).toMatch(/сохранить оригинал/i);
  expect(screen.getByRole("button", { name: /Сохранить как/ })).toHaveProperty("disabled", false);
});
it("does not break the UI when the browser cannot display the preview", async () => {
  await extract(preview);
  fireEvent.click(screen.getByRole("button", { name: "Показать изображение" }));
  fireEvent.error(screen.getByRole("img"));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("status").textContent).toMatch(/сохранить оригинал/i);
  expect(screen.getByRole("button", { name: /Сохранить как/ })).toHaveProperty("disabled", false);
});
it("keeps ordinary PDF extraction and saving", async () => {
  await extract(undefined);
  expect(screen.queryByRole("button", { name: "Показать изображение" })).toBeNull();
  expect(screen.getByRole("button", { name: /Сохранить как/ })).toHaveProperty("disabled", false);
});
