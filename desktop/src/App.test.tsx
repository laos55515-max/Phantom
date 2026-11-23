// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { bridge } from "./bridge";

vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({
    onDragDropEvent: vi.fn(async () => () => undefined),
  }),
}));

vi.mock("./bridge", async (importOriginal) => {
  const original = await importOriginal<typeof import("./bridge")>();
  return {
    ...original,
    isDemoMode: () => false,
    bridge: {
      ...original.bridge,
      probe: vi.fn(),
      pickFile: vi.fn(),
      saveFile: vi.fn(),
      revealPath: vi.fn(),
      exampleInfo: vi.fn(async () => ({
        ok: true,
        demo: true,
        carrier: { name: "example_carrier.png", sizeBytes: 201_798 },
        secret: { name: "example_payload.pdf", sizeBytes: 809 },
      })),
      hide: vi.fn(),
      appInfo: vi.fn(async () => ({ app: "2.1.0", core: "2.1.0", name: "PHANTOM" })),
    },
  };
});

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

function storedDraft(): Record<string, unknown> {
  const raw = window.localStorage.getItem("phantom.settings.draft");
  return raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
}

beforeEach(() => {
  stubMatchMedia();
  window.localStorage.clear();
  window.localStorage.setItem(
    "phantom.settings.draft",
    JSON.stringify({ language: "ru", theme: "dark" }),
  );
  pickFileMock.mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

const TEXT_SENTINEL = "Совершенно секретный текст для черновика — 1234567890";
const FILE_SENTINEL = "договор.pdf";

describe("App — in-memory draft survives a trip to Settings and a language switch", () => {
  it("keeps the typed text and the chosen PDF when language changes mid-edit", async () => {
    render(<App />);

    // 1. switch to text mode and type the draft
    fireEvent.click(screen.getByRole("button", { name: /^Текст/ }));
    fireEvent.change(screen.getByPlaceholderText(/Впишите или вставьте текст/), {
      target: { value: TEXT_SENTINEL },
    });

    // 2. open settings, switch the language to English
    fireEvent.click(screen.getByRole("button", { name: /Настройки/ }));
    expect(screen.getByRole("radiogroup")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: /English/ }));

    // 3. the UI switched language right away
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();

    // 4. back to Hide — the textarea still holds the exact text
    fireEvent.click(screen.getByRole("button", { name: /^Hide$/ }));
    expect(screen.getByDisplayValue(TEXT_SENTINEL)).toBeTruthy();

    // 5. nothing sensitive landed in persistent storage — language/theme only
    const draft = storedDraft();
    expect(Object.keys(draft).sort()).toEqual(["language", "savedAt", "theme"]);
    expect(draft.language).toBe("en");
    const raw = window.localStorage.getItem("phantom.settings.draft") ?? "";
    expect(raw).not.toContain(TEXT_SENTINEL);
    expect(raw).not.toContain("password");
  });

  it("keeps the selected PDF and carrier hint when language changes", async () => {
    pickFileMock.mockImplementation(async (kind) =>
      kind === "secret"
        ? { path: "/tmp/договор.pdf", name: FILE_SENTINEL, sizeBytes: 153_600 }
        : { path: "/tmp/carrier.png", name: "carrier.png", sizeBytes: 201_798 },
    );
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /^Файл/ }));
    await waitFor(() => expect(screen.getByText(FILE_SENTINEL)).toBeTruthy());

    // no capacity verdict before the carrier is probed
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.getByText("Выберите картинку или аудио")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Настройки/ }));
    fireEvent.click(screen.getByRole("radio", { name: /English/ }));
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /^Hide$/ }));
    expect(screen.getByText(FILE_SENTINEL)).toBeTruthy();

    const raw = window.localStorage.getItem("phantom.settings.draft") ?? "";
    expect(raw).not.toContain(FILE_SENTINEL);
    const draft = storedDraft();
    expect(Object.keys(draft).sort()).toEqual(["language", "savedAt", "theme"]);
  });
});
