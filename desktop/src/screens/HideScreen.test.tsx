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
const exampleInfoMock = vi.mocked(bridge.exampleInfo);

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

function seedDraft(language: "uk" | "en" | "ru", theme: "light" | "dark" | "system" = "dark") {
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language, theme }));
}

beforeEach(() => {
  stubMatchMedia();
  window.localStorage.clear();
  probeMock.mockReset();
  pickFileMock.mockReset();
  exampleInfoMock.mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

function renderScreen() {
  return render(
    <I18nProvider>
      <HideScreen onGoExtract={() => undefined} />
    </I18nProvider>,
  );
}

describe("HideScreen — carrier selection gate", () => {
  it("auto-selects a suitable built-in carrier and shows capacity when a PDF is picked", async () => {
    seedDraft("ru");
    pickFileMock.mockResolvedValue({ path: "/tmp/договор.pdf", name: "договор.pdf", sizeBytes: 204_800 });
    renderScreen();

    fireEvent.click(screen.getByRole("button", { name: /Файл/ }));
    await waitFor(() => expect(screen.getByText("договор.pdf")).toBeTruthy());
    await waitFor(() => expect(screen.getByRole("progressbar")).toBeTruthy());

    expect(screen.getByRole("button", { name: "Другой вариант" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Выбрать свой файл" })).toBeTruthy();
    expect(screen.queryByText(/Не вмещается/)).toBeNull();
  });

  it("shows the capacity error only after the chosen carrier was probed and found too small", async () => {
    seedDraft("ru");
    exampleInfoMock.mockResolvedValue({
      ok: true,
      demo: true,
      carrier: { name: "example_carrier.png", sizeBytes: 201_798 },
      secret: { name: "example_payload.pdf", sizeBytes: 809 },
    });
    probeMock.mockResolvedValue({
      ok: true,
      kind: "image",
      path: "/tmp/tiny.png",
      name: "tiny.png",
      sizeBytes: 120_000,
      description: "300x300 RGB",
      capacityBytes: 100, // far too small for the text below
      usableBits: 1200,
      notices: [],
      image: { width: 300, height: 300, mode: "RGB" },
    });
    pickFileMock.mockResolvedValue({ path: "/tmp/tiny.png", name: "tiny.png", sizeBytes: 120_000 });

    renderScreen();
    fireEvent.click(screen.getByRole("button", { name: /^Текст/ }));
    fireEvent.change(screen.getByPlaceholderText(/Впишите или вставьте текст/), {
      target: { value: "A".repeat(4_000) },
    });

    await waitFor(() => expect(screen.getByRole("button", { name: "Выбрать свой файл" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Выбрать свой файл" }));
    await waitFor(() => expect(screen.getByText("tiny.png")).toBeTruthy());

    expect(screen.getByText(/Не вмещается/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Спрятать/ })).toHaveProperty("disabled", true);
  });

  it("keeps the example flow inside the example carrier capacity and shows the bar right away", async () => {
    seedDraft("ru");
    exampleInfoMock.mockResolvedValue({
      ok: true,
      demo: true,
      carrier: { name: "example_carrier.png", sizeBytes: 201_798 },
      secret: { name: "example_payload.pdf", sizeBytes: 809 },
    });
    renderScreen();
    fireEvent.click(screen.getByRole("button", { name: /Попробовать на примере/ }));
    await waitFor(() => expect(screen.getByRole("progressbar")).toBeTruthy());
    expect(screen.queryByText("Выберите картинку или аудио")).toBeNull();
  });
});
