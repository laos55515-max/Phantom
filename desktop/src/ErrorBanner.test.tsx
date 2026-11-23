// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { I18nProvider } from "./i18n";
import { ErrorBanner } from "./ui";

afterEach(() => { cleanup(); window.localStorage.clear(); });

it("explains that saving over the original carrier is refused", () => {
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  render(<I18nProvider><ErrorBanner error={{ code: "output_is_carrier", message: "", detail: null }} /></I18nProvider>);
  expect(screen.getByRole("alert").textContent).toContain("Выберите другое место: этот файл — исходный контейнер");
});
