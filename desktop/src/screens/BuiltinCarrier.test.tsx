// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { HideScreen } from "./HideScreen";
import { bridge } from "../bridge";

vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({
    onDragDropEvent: vi.fn(async () => () => undefined),
  }),
}));

vi.mock("../bridge", async (importOriginal) => {
  const original = await importOriginal<typeof import("../bridge")>();
  return {
    ...original,
    isDemoMode: () => false,
    bridge: {
      ...original.bridge,
      probe: vi.fn(),
      pickFile: vi.fn(),
      saveFile: vi.fn(),
      revealPath: vi.fn(),
      exampleInfo: vi.fn(),
      hide: vi.fn(),
      appInfo: vi.fn(async () => ({ app: "2.1.0", core: "2.1.0", name: "PHANTOM" })),
    },
  };
});

const probeMock = vi.mocked(bridge.probe);
const pickFileMock = vi.mocked(bridge.pickFile);

function stubMatchMedia() {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

function renderScreen() {
  return render(
    <I18nProvider>
      <HideScreen onGoExtract={() => undefined} />
    </I18nProvider>,
  );
}

beforeEach(() => {
  stubMatchMedia();
  window.localStorage.setItem(
    "phantom.settings.draft",
    JSON.stringify({ language: "ru", theme: "dark" }),
  );
  probeMock.mockReset();
  pickFileMock.mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("HideScreen — built-in carriers & auto-selection", () => {
  it("auto-selects a built-in PNG for text and shows required vs available space", async () => {
    renderScreen();
    fireEvent.click(screen.getByRole("button", { name: /^Текст/ }));
    fireEvent.change(screen.getByPlaceholderText(/Впишите или вставьте текст/), {
      target: { value: "Секретное сообщение для проверки автоподбора" },
    });

    await waitFor(() => expect(screen.getByText("01_horizon_320x240.png")).toBeTruthy());
    expect(screen.getByRole("progressbar")).toBeTruthy();
    expect(screen.getByText(/Нужно/)).toBeTruthy();
    expect(screen.getByText(/Вмещает/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Другой вариант" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Выбрать свой файл" })).toBeTruthy();
  });

  it("auto-selects a suitably sized built-in PNG for a PDF and switches image on «Другой вариант»", async () => {
    pickFileMock.mockResolvedValue({
      path: "/tmp/report.pdf",
      name: "report.pdf",
      sizeBytes: 150_000,
    });
    renderScreen();

    fireEvent.click(screen.getByRole("button", { name: /^Файл/ }));
    await waitFor(() => expect(screen.getByText("report.pdf")).toBeTruthy());
    // 150 KB PDF needs >150 KB capacity → 04_aurora_800x600.png (~180 KB)
    await waitFor(() => expect(screen.getByText("04_aurora_800x600.png")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Другой вариант" }));
    await waitFor(() => expect(screen.getByText("05_dunes_1024x768.png")).toBeTruthy());
  });

  it("lets the user pick their own carrier file via «Выбрать свой файл»", async () => {
    pickFileMock
      .mockResolvedValueOnce({ path: "/tmp/report.pdf", name: "report.pdf", sizeBytes: 5_000 })
      .mockResolvedValueOnce({ path: "/tmp/custom.png", name: "custom.png", sizeBytes: 500_000 });
    probeMock.mockResolvedValue({
      ok: true,
      kind: "image",
      path: "/tmp/custom.png",
      name: "custom.png",
      sizeBytes: 500_000,
      description: "1200x900 RGB",
      capacityBytes: 400_000,
      usableBits: 3_240_000,
      notices: [],
      image: { width: 1200, height: 900, mode: "RGB" },
    });

    renderScreen();
    fireEvent.click(screen.getByRole("button", { name: /^Файл/ }));
    await waitFor(() => expect(screen.getByText("01_horizon_320x240.png")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Выбрать свой файл" }));
    await waitFor(() => expect(screen.getByText("custom.png")).toBeTruthy());
    expect(probeMock).toHaveBeenCalledWith(
      expect.objectContaining({ path: "/tmp/custom.png", displayName: "custom.png" }),
    );
  });

  it("suggests choosing a larger carrier when no built-in PNG fits the attachment", async () => {
    pickFileMock.mockResolvedValue({
      path: "/tmp/huge.pdf",
      name: "huge.pdf",
      sizeBytes: 5_000_000,
    });

    renderScreen();
    fireEvent.click(screen.getByRole("button", { name: /^Файл/ }));
    await waitFor(() => expect(screen.getByText("huge.pdf")).toBeTruthy());

    await waitFor(() =>
      expect(screen.getByText(/Ни одна встроенная картинка не вмещает/)).toBeTruthy(),
    );
    expect(screen.getByText(/Выберите более вместительный носитель/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Выбрать свой файл" })).toBeTruthy();
    expect(screen.getByRole("progressbar")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Спрятать/ })).toHaveProperty("disabled", true);
  });
});
