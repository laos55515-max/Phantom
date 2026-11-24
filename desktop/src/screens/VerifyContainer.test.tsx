// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
beforeEach(() => {
  invoke.mockReset();
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) });
});
afterEach(() => { cleanup(); window.localStorage.clear(); });
async function setup(response: object) {
  invoke.mockImplementation(async (command) => {
    if (command === "pick_file") return { path: "/tmp/downloaded.png", name: "downloaded.png", size_bytes: 1234 };
    if (command === "bridge_call") return response;
    throw Error(`Unexpected command ${command}`);
  });
  render(<I18nProvider><ExtractScreen /></I18nProvider>);
  fireEvent.click(screen.getByRole("button", { name: /Выбрать файл/ }));
  await screen.findByText("downloaded.png");
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "valid-password-123" } });
  fireEvent.click(screen.getByRole("button", { name: "Проверить с паролем" }));
}
it("sends verification-only mode and shows success without content", async () => {
  await setup({ ok: true, verified: true });
  await screen.findByText("Контейнер проверен");
  expect(invoke).toHaveBeenCalledWith("bridge_call", { request: { cmd: "extract", carrier_path: "/tmp/downloaded.png", password: "valid-password-123", mode: "verify" } });
  expect(screen.queryByText("Найден файл")).toBeNull();
  expect(screen.queryByRole("button", { name: /Сохранить как/ })).toBeNull();
  expect(screen.getByLabelText("Пароль")).toHaveProperty("value", "");
});
it.each(["wrong_password_or_no_data", "integrity", "unsupported_carrier"])("does not claim success on %s", async (code) => {
  await setup({ ok: false, error: { code, message: "failed", detail: null } });
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect(screen.queryByText("Контейнер проверен")).toBeNull();
});
it("does not trust a successful reply lacking verified=true", async () => {
  await setup({ ok: true });
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect(screen.queryByText("Контейнер проверен")).toBeNull();
});
