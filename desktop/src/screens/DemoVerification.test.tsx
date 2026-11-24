// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { bridge, isDemoMode } from "../bridge";
import { I18nProvider } from "../i18n";
import { ExtractScreen } from "./ExtractScreen";

beforeEach(() => {
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) });
});
afterEach(() => { cleanup(); window.localStorage.clear(); vi.restoreAllMocks(); });

it("does not verify a nonexistent carrier in browser demo", async () => {
  expect(isDemoMode()).toBe(true);
  const verify = vi.spyOn(bridge, "verifyContainer");
  render(<I18nProvider><ExtractScreen handoff={{ path: "/not-existing/container.png" }} /></I18nProvider>);
  await screen.findByText("container.png");
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "valid-password-123" } });
  const button = screen.getByRole("button", { name: "Проверить с паролем" });
  expect(button).toHaveProperty("disabled", true);
  expect(screen.getByText("Проверка доступна в настольном приложении")).toBeTruthy();
  fireEvent.click(button);
  expect(verify).not.toHaveBeenCalled();
  expect(screen.queryByText("Контейнер проверен")).toBeNull();
  // Even an accidental direct call cannot claim verification in the demo.
  expect(await bridge.verifyContainer({ carrierPath: "/not-existing/container.png", password: "valid-password-123" })).toMatchObject({ ok: false });
  expect(screen.queryByText("Контейнер проверен")).toBeNull();
});
