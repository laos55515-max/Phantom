// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useState } from "react";
import { I18nProvider } from "./i18n";
import { PasswordField } from "./ui";

const { writeText } = vi.hoisted(() => ({ writeText: vi.fn() }));
vi.mock("@tauri-apps/plugin-clipboard-manager", () => ({ writeText }));
vi.mock("./bridge", async (original) => ({ ...await original<typeof import("./bridge")>(), isDemoMode: () => false }));

function Form() {
  const [value, setValue] = useState("Entire-secret-123");
  return <I18nProvider><PasswordField label="Пароль" value={value} onChange={setValue} /></I18nProvider>;
}
beforeEach(() => {
  writeText.mockReset();
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }) });
});
afterEach(() => { cleanup(); window.localStorage.clear(); });

it("copies the complete hidden password only on button press and confirms after success", async () => {
  let finish!: () => void;
  writeText.mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
  render(<Form />);
  expect(screen.getByLabelText("Пароль")).toHaveProperty("type", "password");
  expect(writeText).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Скопировать пароль" }));
  expect(writeText).toHaveBeenCalledWith("Entire-secret-123");
  expect(screen.queryByText("Пароль скопирован")).toBeNull();
  finish();
  await screen.findByText("Пароль скопирован");
  fireEvent.change(screen.getByLabelText("Пароль"), { target: { value: "new-password-456" } });
  await waitFor(() => expect(screen.queryByText("Пароль скопирован")).toBeNull());
});

it("reports clipboard failure without claiming success", async () => {
  writeText.mockRejectedValue(new Error("denied"));
  render(<Form />);
  fireEvent.click(screen.getByRole("button", { name: "Скопировать пароль" }));
  await screen.findByText("Не удалось скопировать пароль.");
  expect(screen.queryByText("Пароль скопирован")).toBeNull();
});
