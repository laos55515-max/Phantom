// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  bridge,
  type BridgeResult,
  type MetadataFieldInfo,
  type MetadataInspection,
  type MetadataReportInfo,
} from "../bridge";
import { dictionaries } from "../i18n-dict";
import { I18nProvider } from "../i18n";
import { MetadataScreen } from "./MetadataScreen";

vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({ onDragDropEvent: vi.fn(async () => () => undefined) }),
}));

vi.mock("../bridge", async (importOriginal) => {
  const original = await importOriginal<typeof import("../bridge")>();
  return {
    ...original,
    isDemoMode: () => false,
    bridge: {
      ...original.bridge,
      pickFile: vi.fn(),
      saveFile: vi.fn(),
      metadataInspect: vi.fn(),
      metadataPlan: vi.fn(),
      metadataOutputPath: vi.fn(),
      metadataClean: vi.fn(),
      revealPath: vi.fn(),
    },
  };
});

const pickFileMock = vi.mocked(bridge.pickFile);
const saveFileMock = vi.mocked(bridge.saveFile);
const inspectMock = vi.mocked(bridge.metadataInspect);
const planMock = vi.mocked(bridge.metadataPlan);
const targetMock = vi.mocked(bridge.metadataOutputPath);
const cleanMock = vi.mocked(bridge.metadataClean);

const RU = dictionaries.ru;

function field(
  partial: Partial<MetadataFieldInfo> & Pick<MetadataFieldInfo, "key" | "label" | "category">,
): MetadataFieldInfo {
  return {
    value: "",
    container: "eXIf",
    removable: true,
    derived: false,
    reason: "",
    reasonKey: "",
    reasonParams: {},
    valueKey: "",
    valueParams: {},
    size: 0,
    truncated: false,
    ...partial,
  };
}

const FIELDS: MetadataFieldInfo[] = [
  field({ key: "png.gps.0x0002", label: "GPSLatitude", category: "personal", container: "GPS", value: "50, 27, 0" }),
  field({ key: "png.text.tEXt.Comment", label: "Comment", category: "personal", container: "tEXt", value: "kitchen wifi" }),
  field({
    key: "png.exif.0x0112",
    label: "Orientation",
    category: "technical",
    container: "eXIf/0th",
    value: "6",
    removable: false,
    reason: "kept by policy",
    reasonKey: "meta.reason.kept_by_policy",
  }),
  field({ key: "png.exif.0x8298", label: "Copyright", category: "rights", container: "eXIf/0th", value: "(c) 2024 Ada", removable: false }),
  field({ key: "png.text.tEXt.Title", label: "Title", category: "descriptive", container: "tEXt", value: "Holiday", removable: false }),
  field({
    key: "png.chunk.prVt",
    label: "Unknown chunk prVt",
    category: "unknown",
    container: "container",
    value: "17 bytes",
    removable: false,
    valueKey: "meta.value.bytes",
    valueParams: { size: "17" },
    reason: "unknown data",
    reasonKey: "meta.reason.unknown_data",
  }),
];

function makeReport(fields: MetadataFieldInfo[] = FIELDS): MetadataReportInfo {
  return {
    path: "/home/ada/Holiday.png",
    format: "png",
    sizeBytes: 2048,
    sha256: "a".repeat(64),
    counts: { total: 6, personal: 2, descriptive: 1, technical: 1, rights: 1, provenance: 0, unknown: 1, removable: 2 },
    technical: { width: "300", height: "300", mode: "RGB" },
    technicalSummary: { resolution: "300x300" },
    checksumOk: true,
    warnings: [],
    fields,
    chunks: [{ kind: "IHDR", offset: 8, size: 13, checksumOk: true, note: "" }],
    provenance: { state: "none", detected: false, manifests: [], signed: false, details: [] },
  };
}

const REMOVE_KEYS = ["png.gps.0x0002", "png.text.tEXt.Comment"];
const KEEP_FIELDS = FIELDS.filter((item) => !REMOVE_KEYS.includes(item.key));

function defaultPlan(removable = REMOVE_KEYS) {
  const remove = FIELDS.filter((item) => removable.includes(item.key));
  return {
    allowed: true,
    reason: "",
    remove,
    keep: FIELDS.filter((item) => !removable.includes(item.key)),
    blocked: [],
    warnings: [],
  };
}

function inspection(overrides: Partial<MetadataInspection> = {}): BridgeResult<MetadataInspection> {
  return {
    ok: true,
    report: makeReport(),
    defaultSelection: REMOVE_KEYS,
    plan: defaultPlan(),
    ...overrides,
  };
}

function seed() {
  window.localStorage.setItem("phantom.settings.draft", JSON.stringify({ language: "ru", theme: "dark" }));
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  });
}

async function openFile() {
  render(
    <I18nProvider>
      <MetadataScreen />
    </I18nProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: RU["meta.choose_file"] }));
  await screen.findByRole("heading", { name: RU["meta.preview.title"] });
}

beforeEach(() => {
  seed();
  pickFileMock.mockReset();
  saveFileMock.mockReset();
  inspectMock.mockReset();
  planMock.mockReset();
  targetMock.mockReset();
  cleanMock.mockReset();
  pickFileMock.mockResolvedValue({ path: "/home/ada/Holiday.png", name: "Holiday.png", sizeBytes: 2048 });
  inspectMock.mockResolvedValue(inspection());
  planMock.mockResolvedValue({
    ok: true,
    report: makeReport(),
    plan: defaultPlan(),
  });
  targetMock.mockImplementation(async ({ path, outputPath }) => ({
    ok: true,
    source: path,
    output: outputPath,
  }));
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("MetadataScreen — inspect, preview, clean", () => {
  it("opens a PNG, groups fields by category and preselects the safe personal ones", async () => {
    await openFile();

    for (const category of ["personal", "descriptive", "technical", "rights", "unknown"]) {
      expect(screen.getByRole("heading", { name: RU[`meta.cat.${category}`] })).toBeTruthy();
    }
    expect((screen.getByRole("checkbox", { name: "GPSLatitude (GPS)" }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole("checkbox", { name: "Comment (tEXt)" }) as HTMLInputElement).checked).toBe(true);
    const orientation = screen.getByRole("checkbox", { name: "Orientation (eXIf/0th)" }) as HTMLInputElement;
    expect(orientation.checked).toBe(false);
    expect(orientation.disabled).toBe(true);
    expect((screen.getByRole("checkbox", { name: "Copyright (eXIf/0th)" }) as HTMLInputElement).checked).toBe(false);

    expect(screen.getByText(RU["meta.toolbar.selected"].replace("{count}", "2"))).toBeTruthy();
    expect(screen.getByText(RU["meta.preview.remove"].replace("{count}", "2"))).toBeTruthy();
    expect(screen.getByText(RU["meta.preview.keep"].replace("{count}", "4"))).toBeTruthy();
    expect(screen.getByText(RU["meta.value.bytes"].replace("{size}", "17"))).toBeTruthy();
    expect(screen.getByText(RU["meta.reason.kept_by_policy"])).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: RU["meta.save"] }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("re-asks the core when the selection changes and shows its plan", async () => {
    await openFile();
    planMock.mockResolvedValueOnce({
      ok: true,
      report: makeReport(),
      plan: defaultPlan(["png.gps.0x0002"]),
    });

    fireEvent.click(screen.getByRole("checkbox", { name: "Comment (tEXt)" }));

    await waitFor(() => expect(planMock).toHaveBeenCalled());
    expect(planMock.mock.calls[0][0]).toEqual({
      path: "/home/ada/Holiday.png",
      selection: ["png.gps.0x0002"],
    });
    await screen.findByText(RU["meta.preview.remove"].replace("{count}", "1"));
    expect(screen.getByText(RU["meta.preview.keep"].replace("{count}", "5"))).toBeTruthy();
  });

  it("saves the cleaned copy through the system dialog and proves the original did not change", async () => {
    await openFile();
    saveFileMock.mockResolvedValue("/home/ada/Holiday-clean.png");
    cleanMock.mockResolvedValue({
      ok: true,
      source: "/home/ada/Holiday.png",
      output: "/home/ada/Holiday-clean.png",
      removed: FIELDS.filter((item) => REMOVE_KEYS.includes(item.key)),
      kept: KEEP_FIELDS,
      verified: true,
      checksumOk: true,
      sourceSha256: "a".repeat(64),
      outputSha256: "b".repeat(64),
      content: { identical: true, checked: true, differences: [], source: "c".repeat(64), output: "c".repeat(64) },
      warnings: [],
      reportAfter: { ...makeReport(KEEP_FIELDS), path: "/home/ada/Holiday-clean.png", counts: { ...makeReport().counts, total: 4, personal: 0, removable: 0 } },
    });

    fireEvent.click(screen.getByRole("button", { name: RU["meta.save"] }));

    await screen.findByText(RU["meta.result.title"]);
    expect(saveFileMock).toHaveBeenCalledWith("Holiday-clean.png", "metadata", undefined);
    expect(targetMock).toHaveBeenCalledWith({
      path: "/home/ada/Holiday.png",
      outputPath: "/home/ada/Holiday-clean.png",
    });
    expect(cleanMock).toHaveBeenCalledWith({
      sourcePath: "/home/ada/Holiday.png",
      outputPath: "/home/ada/Holiday-clean.png",
      selection: REMOVE_KEYS,
      overwrite: false,
    });
    expect(screen.getByText(RU["meta.result.pixels"])).toBeTruthy();
    expect(screen.getByText(RU["meta.result.source_unchanged"].replace("{digest}", "a".repeat(16)))).toBeTruthy();
    expect(screen.getByText(/Удалено полей: 2/)).toBeTruthy();
    expect(screen.getByText(/Сохранено полей: 4/)).toBeTruthy();
    expect(screen.getByRole("button", { name: RU["meta.again"] })).toBeTruthy();
  });

  it("refuses to write over the source with a clear message and never calls the core", async () => {
    await openFile();
    saveFileMock.mockResolvedValue("/home/ada/Holiday.wav");
    targetMock.mockResolvedValue({
      ok: false,
      error: {
        code: "source_overwrite",
        message: "the cleaned copy must not replace the original",
        detail: "/home/ada/Holiday.png",
        params: { source: "/home/ada/Holiday.png", output: "/home/ada/Holiday.png" },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: RU["meta.save"] }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    // The message names the path the copy would really get, not the .wav typed.
    expect(
      screen.getByText(
        RU["meta.error.source_overwrite"].replace("{path}", "/home/ada/Holiday.png"),
      ),
    ).toBeTruthy();
    expect(cleanMock).not.toHaveBeenCalled();
    expect(screen.queryByText(RU["meta.result.title"])).toBeNull();
  });

  it("refuses when the resolved destination already exists", async () => {
    await openFile();
    saveFileMock.mockResolvedValue("/home/ada/existing.wav");
    targetMock.mockResolvedValue({
      ok: false,
      error: {
        code: "output_exists",
        message: "the destination already exists",
        detail: "/home/ada/existing.png",
        params: { path: "/home/ada/existing.png" },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: RU["meta.save"] }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(
      screen.getByText(
        RU["meta.error.output_exists"].replace("{path}", "/home/ada/existing.png"),
      ),
    ).toBeTruthy();
    expect(cleanMock).not.toHaveBeenCalled();
    expect(screen.queryByText(RU["meta.result.title"])).toBeNull();
  });

  it("asks for another folder when the filesystem cannot publish atomically", async () => {
    await openFile();
    saveFileMock.mockResolvedValue("/media/usb/copy.png");
    targetMock.mockResolvedValue({
      ok: false,
      error: {
        code: "publish_unavailable",
        message: "this folder cannot receive the copy atomically",
        detail: "/media/usb/copy.png",
        params: { path: "/media/usb/copy.png" },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: RU["meta.save"] }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(
      screen.getByText(
        RU["meta.error.publish_unavailable"].replace("{path}", "/media/usb/copy.png"),
      ),
    ).toBeTruthy();
    expect(cleanMock).not.toHaveBeenCalled();
    expect(screen.queryByText(RU["meta.result.title"])).toBeNull();
  });

  it("re-opens the dialog so it confirms the exact path that will be written", async () => {
    await openFile();
    saveFileMock
      .mockResolvedValueOnce("/home/ada/Holiday-clean")
      .mockResolvedValueOnce("/home/ada/Holiday-clean.png");
    targetMock
      .mockResolvedValueOnce({
        ok: true,
        source: "/home/ada/Holiday.png",
        output: "/home/ada/Holiday-clean.png",
      })
      .mockResolvedValueOnce({
        ok: true,
        source: "/home/ada/Holiday.png",
        output: "/home/ada/Holiday-clean.png",
      });
    cleanMock.mockResolvedValue({
      ok: true,
      source: "/home/ada/Holiday.png",
      output: "/home/ada/Holiday-clean.png",
      removed: FIELDS.filter((item) => REMOVE_KEYS.includes(item.key)),
      kept: KEEP_FIELDS,
      verified: true,
      checksumOk: true,
      sourceSha256: "a".repeat(64),
      outputSha256: "b".repeat(64),
      content: { identical: true, checked: true, differences: [], source: "c".repeat(64), output: "c".repeat(64) },
      warnings: [],
      reportAfter: { ...makeReport(KEEP_FIELDS), path: "/home/ada/Holiday-clean.png", counts: { ...makeReport().counts, total: 4, personal: 0, removable: 0 } },
    });

    fireEvent.click(screen.getByRole("button", { name: RU["meta.save"] }));

    await screen.findByText(RU["meta.result.title"]);
    expect(saveFileMock).toHaveBeenNthCalledWith(1, "Holiday-clean.png", "metadata", undefined);
    expect(saveFileMock).toHaveBeenNthCalledWith(
      2,
      "Holiday-clean.png",
      "metadata",
      "/home/ada/",
    );
    expect(cleanMock).toHaveBeenCalledWith({
      sourcePath: "/home/ada/Holiday.png",
      outputPath: "/home/ada/Holiday-clean.png",
      selection: REMOVE_KEYS,
      overwrite: false,
    });
  });

  it("shows the core's signed-media refusal before any save", async () => {
    inspectMock.mockResolvedValue(
      inspection({
        defaultSelection: [],
        report: {
          ...makeReport(),
          provenance: { state: "unverified", detected: true, manifests: ["urn:uuid:demo"], signed: true, details: [] },
        },
        plan: { allowed: false, reason: "signed_media", remove: [], keep: FIELDS, blocked: [], warnings: [] },
      }),
    );

    render(
      <I18nProvider>
        <MetadataScreen />
      </I18nProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: RU["meta.choose_file"] }));

    await screen.findByText(RU["meta.refusal.title"]);
    expect(screen.getByText(RU["meta.refusal.signed"])).toBeTruthy();
    expect((screen.getByRole("button", { name: RU["meta.save"] }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("explains a refusal that only the core discovers while cleaning", async () => {
    await openFile();
    saveFileMock.mockResolvedValue("/home/ada/Holiday-clean.png");
    cleanMock.mockResolvedValue({
      ok: false,
      error: {
        code: "signed_media",
        message: "a content credential is attached; it cannot survive a rewrite",
        detail: "unverified",
        params: { state: "unverified" },
      },
    });

    fireEvent.click(screen.getByRole("button", { name: RU["meta.save"] }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByText(RU["meta.error.signed"])).toBeTruthy();
    expect(screen.queryByText(RU["meta.result.title"])).toBeNull();
  });

  it("shows an unsupported file as a translated error and allows another pick", async () => {
    pickFileMock.mockResolvedValueOnce({ path: "/home/ada/photo.jpg", name: "photo.jpg", sizeBytes: 1000 });
    inspectMock.mockResolvedValueOnce({
      ok: false,
      error: { code: "unsupported_format", message: "only PNG, WAV and FLAC", detail: ".jpg" },
    });

    render(
      <I18nProvider>
        <MetadataScreen />
      </I18nProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: RU["meta.choose_file"] }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByText(RU["meta.error.unsupported"])).toBeTruthy();
    expect(screen.getByRole("button", { name: RU["meta.change_file"] })).toBeTruthy();
    expect(screen.queryByRole("button", { name: RU["meta.save"] })).toBeNull();
  });
});
