/**
 * Bridge between the React UI and the Python core.
 *
 * Inside Tauri the calls go to the bundled `phantom-bridge` sidecar: a fresh
 * process per action, passwords travelling on stdin, never on the command
 * line.  In a plain browser (vite preview / screenshots) a clearly-labelled
 * demo implementation answers instead — `isDemoMode()` tells the UI it is
 * looking at simulations, and the banner says so.
 */

import { invoke } from "@tauri-apps/api/core";

export function isDemoMode(): boolean {
  return typeof window !== "undefined" && !("__TAURI_INTERNALS__" in window);
}

// ------------------------------------------------------------- shared types

export interface BridgeError {
  code: string;
  message: string;
  detail: string | null;
  params?: Record<string, unknown>;
}

export type BridgeResult<T> = ({ ok: true } & T) | { ok: false; error: BridgeError };

export interface FilePick {
  path: string;
  name: string;
  sizeBytes: number | null;
}

export interface ProbeInfo {
  kind: "image" | "audio";
  path: string;
  name: string;
  sizeBytes: number;
  description: string;
  capacityBytes: number;
  usableBits: number;
  notices: string[];
  builtin?: boolean;
  builtinId?: string;
  image?: { width: number; height: number; mode: string };
  audio?: { frames: number; channels: number; bits: number; sampleRate: number };
}

export interface BuiltinCarrierSelection {
  fits: boolean;
  requiredBytes: number;
  maxCapacityBytes: number;
  suitableCount: number;
  index: number;
  carrier: ProbeInfo | null;
}

export interface HideSuccess {
  outputPath: string;
  kind: "image" | "audio";
  payloadKind: "text" | "file";
  payloadBytes: number;
  userBytes: number;
  capacityBytes: number;
  verified: boolean;
  kdf: string;
  demo?: boolean;
}

export interface ExtractBase {
  originalSize: number;
  sha256: string;
  integrityOk: boolean;
  containerVersion: number;
  kdf: string;
}

export type ImagePreview =
  | { type: "JPEG" | "PNG"; status: "available"; data: string; width: number; height: number }
  | { type: "JPEG" | "PNG"; status: "unavailable"; reason: "size" | "decode" };

export type ExtractResponse = BridgeResult<
  | (ExtractBase & { kind: "text"; text?: string; textOmitted?: boolean })
  | (ExtractBase & { kind: "file"; suggestedName: string; outputPath?: string; imagePreview?: ImagePreview })
>;

export interface ExampleInfo {
  demo: true;
  carrier: { name: string; sizeBytes: number };
  secret: { name: string; sizeBytes: number };
}

export interface AppInfo {
  app: string;
  core: string;
  name: string;
}

export interface PhantomBridge {
  version(): Promise<BridgeResult<{ version: string; protocol: number }>>;
  exampleInfo(): Promise<BridgeResult<ExampleInfo>>;
  probe(request: {
    path: string;
    displayName?: string;
    displaySize?: number;
  }): Promise<BridgeResult<ProbeInfo>>;
  selectBuiltinCarrier(request: {
    secretPath?: string;
    text?: string;
    requiredBytes?: number;
    excludeId?: string;
    offset?: number;
  }): Promise<BridgeResult<BuiltinCarrierSelection>>;
  hide(args: {
    carrierPath?: string;
    builtinCarrierId?: string;
    outputPath: string;
    password: string;
    secretPath?: string;
    text?: string;
    example?: boolean;
  }): Promise<BridgeResult<HideSuccess>>;
  extract(args: {
    carrierPath: string;
    password: string;
    mode: "info" | "save_file";
    outputPath?: string;
  }): Promise<ExtractResponse>;
  verifyContainer(args: { carrierPath: string; password: string }): Promise<BridgeResult<{ verified: true }>>;
  pickFile(
    kind: "secret" | "carrier",
    media: "image" | "audio" | "any",
  ): Promise<FilePick | null>;
  saveFile(
    suggestedName: string,
    kind: "image" | "audio" | "text" | "file",
  ): Promise<string | null>;
  revealPath(path: string): Promise<void>;
  appInfo(): Promise<AppInfo>;
}

// ------------------------------------------------------------- error shape

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function normaliseError(raw: unknown): BridgeError {
  if (isRecord(raw) && isRecord(raw.error)) {
    const error = raw.error;
    return {
      code: typeof error.code === "string" ? error.code : "internal",
      message: typeof error.message === "string" ? error.message : "error",
      detail: typeof error.detail === "string" ? error.detail : null,
      params: isRecord(error.params) ? error.params : undefined,
    };
  }
  return {
    code: "internal",
    message: "error",
    detail: typeof raw === "string" ? raw : JSON.stringify(raw),
  };
}

function asError(result: unknown): BridgeError {
  return normaliseError(result);
}

function mapProbe(raw: Record<string, unknown>): ProbeInfo {
  const image = isRecord(raw.image)
    ? {
        width: Number(raw.image.width ?? 0),
        height: Number(raw.image.height ?? 0),
        mode: String(raw.image.mode ?? ""),
      }
    : undefined;
  const audio = isRecord(raw.audio)
    ? {
        frames: Number(raw.audio.frames ?? 0),
        channels: Number(raw.audio.channels ?? 0),
        bits: Number(raw.audio.bits ?? 0),
        sampleRate: Number(raw.audio.sample_rate ?? raw.audio.sampleRate ?? 0),
      }
    : undefined;
  return {
    kind: raw.kind === "audio" ? "audio" : "image",
    path: String(raw.path ?? ""),
    name: String(raw.name ?? ""),
    sizeBytes: Number(raw.size_bytes ?? raw.sizeBytes ?? 0),
    description: String(raw.description ?? ""),
    capacityBytes: Number(raw.capacity_bytes ?? raw.capacityBytes ?? 0),
    usableBits: Number(raw.usable_bits ?? raw.usableBits ?? 0),
    notices: Array.isArray(raw.notices) ? raw.notices.map(String) : [],
    builtin: raw.builtin === true ? true : undefined,
    builtinId:
      typeof raw.builtin_id === "string"
        ? raw.builtin_id
        : typeof raw.builtinId === "string"
          ? raw.builtinId
          : undefined,
    image,
    audio,
  };
}

function mapHide(raw: Record<string, unknown>): HideSuccess {
  return {
    outputPath: String(raw.output_path ?? raw.outputPath ?? ""),
    kind: raw.kind === "audio" ? "audio" : "image",
    payloadKind: raw.payload_kind === "text" || raw.payloadKind === "text" ? "text" : "file",
    payloadBytes: Number(raw.payload_bytes ?? raw.payloadBytes ?? 0),
    userBytes: Number(raw.user_bytes ?? raw.userBytes ?? 0),
    capacityBytes: Number(raw.capacity_bytes ?? raw.capacityBytes ?? 0),
    verified: Boolean(raw.verified),
    kdf: String(raw.profile_label ?? raw.kdf ?? "argon2id"),
    demo: raw.demo === true ? true : undefined,
  };
}

function mapExtract(raw: Record<string, unknown>): ExtractResponse {
  if (raw.ok !== true) {
    return { ok: false, error: asError(raw) };
  }
  const base: ExtractBase = {
    originalSize: Number(raw.original_size ?? raw.originalSize ?? 0),
    sha256: String(raw.sha256 ?? ""),
    integrityOk: Boolean(raw.integrity_ok ?? raw.integrityOk),
    containerVersion: Number(raw.container_version ?? raw.containerVersion ?? 0),
    kdf: String(raw.kdf_label ?? raw.kdf ?? ""),
  };
  if (raw.kind === "text") {
    return {
      ok: true,
      ...base,
      kind: "text",
      text: typeof raw.text === "string" ? raw.text : undefined,
      textOmitted: raw.text_omitted === true || raw.textOmitted === true ? true : undefined,
    };
  }
  const rawPreview = raw.image_preview;
  let imagePreview: ImagePreview | undefined;
  if (isRecord(rawPreview) && (rawPreview.type === "JPEG" || rawPreview.type === "PNG")) {
    if (rawPreview.status === "available" && rawPreview.mime === "image/jpeg" &&
        typeof rawPreview.data === "string" && rawPreview.data.length <= 1_398_104 &&
        /^[A-Za-z0-9+/]*={0,2}$/.test(rawPreview.data) &&
        typeof rawPreview.width === "number" && Number.isInteger(rawPreview.width) && rawPreview.width > 0 && rawPreview.width <= 1024 &&
        typeof rawPreview.height === "number" && Number.isInteger(rawPreview.height) && rawPreview.height > 0 && rawPreview.height <= 1024) {
      imagePreview = { type: rawPreview.type, status: "available", data: rawPreview.data, width: rawPreview.width, height: rawPreview.height };
    } else {
      imagePreview = { type: rawPreview.type, status: "unavailable", reason: rawPreview.reason === "size" ? "size" : "decode" };
    }
  }
  return {
    ok: true,
    ...base,
    kind: "file",
    imagePreview,
    suggestedName: String(raw.suggested_name ?? raw.suggestedName ?? "phantom-extracted.bin"),
    outputPath:
      typeof raw.output_path === "string"
        ? raw.output_path
        : typeof raw.outputPath === "string"
          ? raw.outputPath
          : undefined,
  };
}

async function callBridge(request: Record<string, unknown>): Promise<Record<string, unknown>> {
  try {
    const result = (await invoke("bridge_call", { request })) as unknown;
    return isRecord(result) ? result : { ok: false, error: { code: "internal", message: "bad reply", detail: null } };
  } catch (error) {
    return {
      ok: false,
      error: {
        code: "internal",
        message: "bridge invocation failed",
        detail: error instanceof Error ? error.message : String(error),
      },
    };
  }
}

export const tauriBridge: PhantomBridge = {
  async version() {
    const raw = await callBridge({ cmd: "version" });
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    return { ok: true, version: String(raw.version ?? "?"), protocol: Number(raw.protocol ?? 0) };
  },
  async exampleInfo() {
    const raw = await callBridge({ cmd: "example_info" });
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    const carrier = isRecord(raw.carrier) ? raw.carrier : {};
    const secret = isRecord(raw.secret) ? raw.secret : {};
    return {
      ok: true,
      demo: true,
      carrier: {
        name: String(carrier.name ?? "example_carrier.png"),
        sizeBytes: Number(carrier.size_bytes ?? carrier.sizeBytes ?? 0),
      },
      secret: {
        name: String(secret.name ?? "example_payload.pdf"),
        sizeBytes: Number(secret.size_bytes ?? secret.sizeBytes ?? 0),
      },
    };
  },
  async probe({ path }) {
    const raw = await callBridge({ cmd: "probe", path });
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    return { ok: true, ...mapProbe(raw) };
  },
  async selectBuiltinCarrier({ secretPath, text, requiredBytes, excludeId, offset }) {
    const req: Record<string, unknown> = { cmd: "select_carrier" };
    if (secretPath !== undefined) req.secret_path = secretPath;
    else if (text !== undefined) req.text = text;
    else if (requiredBytes !== undefined) req.required_bytes = requiredBytes;
    if (excludeId !== undefined) req.exclude_id = excludeId;
    if (offset !== undefined) req.offset = offset;
    const raw = await callBridge(req);
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    const carrier = isRecord(raw.carrier) ? mapProbe(raw.carrier) : null;
    return {
      ok: true,
      fits: Boolean(raw.fits),
      requiredBytes: Number(raw.required_bytes ?? raw.requiredBytes ?? 0),
      maxCapacityBytes: Number(raw.max_capacity_bytes ?? raw.maxCapacityBytes ?? 0),
      suitableCount: Number(raw.suitable_count ?? raw.suitableCount ?? 0),
      index: Number(raw.index ?? 0),
      carrier,
    };
  },
  async hide({ carrierPath, builtinCarrierId, outputPath, password, secretPath, text, example }) {
    const request: Record<string, unknown> = example
      ? { cmd: "example_hide", output_path: outputPath, password }
      : {
          cmd: "hide",
          output_path: outputPath,
          password,
          secret_path: secretPath,
          text,
          ...(builtinCarrierId ? { builtin_carrier_id: builtinCarrierId } : { carrier_path: carrierPath }),
        };
    const raw = await callBridge(request);
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    return { ok: true, ...mapHide(raw) };
  },
  async extract({ carrierPath, password, mode, outputPath }) {
    const raw = await callBridge({
      cmd: "extract",
      carrier_path: carrierPath,
      password,
      mode,
      output_path: outputPath,
    });
    return mapExtract(raw);
  },
  async verifyContainer({ carrierPath, password }) {
    const raw = await callBridge({ cmd: "extract", carrier_path: carrierPath, password, mode: "verify" });
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    if (raw.verified !== true) return { ok: false, error: { code: "internal", message: "invalid verification response", detail: null } };
    return { ok: true, verified: true };
  },
  async pickFile(kind, media) {
    const raw: unknown = await invoke("pick_file", { kind, media });
    if (raw === null) return null;
    if (!isRecord(raw) || typeof raw.path !== "string" || typeof raw.name !== "string") {
      throw new Error("Invalid file picker response");
    }
    // Rust returns snake_case. Never coerce missing/invalid sizes to zero.
    const size = raw.size_bytes;
    return {
      path: raw.path,
      name: raw.name,
      sizeBytes: typeof size === "number" && Number.isSafeInteger(size) && size >= 0 ? size : null,
    };
  },
  async saveFile(suggestedName, kind) {
    return (await invoke("save_file", { suggestedName, kind })) as string | null;
  },
  async revealPath(path) {
    await invoke("reveal_path", { path });
  },
  async appInfo() {
    return (await invoke("app_info")) as AppInfo;
  },
};

// ------------------------------------------------------------ browser demo

const demoSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function demoCapacityOf(name: string): number {
  return name.toLowerCase().endsWith(".wav") || name.toLowerCase().endsWith(".flac")
    ? 2_000_000
    : 33_684;
}

function demoUnsupported(name: string): BridgeError {
  const lower = name.toLowerCase();
  const code = /\.(mp3|aac|oga|ogg|opus|wma|m4a)$/.test(lower)
    ? "audio_lossy"
    : "unsupported_carrier";
  return { code, message: "unsupported (demo)", detail: null };
}

const DEMO_TEXT =
  "PHANTOM browser demo — this is simulated text.\nIn the desktop app the payload is AES-256-GCM decrypted on this device, byte-for-byte.";
const DEMO_SHA = "78d88af9d5e83f782f545ed8915b9a390b5d216c01c92df55f7f8eca0c16c9ed";

const DEMO_BUILTIN_CARRIERS: ProbeInfo[] = [
  { kind: "image", path: "builtin://01_horizon_320x240.png", name: "01_horizon_320x240.png", builtinId: "01_horizon_320x240.png", sizeBytes: 31_802, description: "320x240 RGB", capacityBytes: 28_680, usableBits: 230_400, notices: [], builtin: true, image: { width: 320, height: 240, mode: "RGB" } },
  { kind: "image", path: "builtin://02_ember_400x300.png", name: "02_ember_400x300.png", builtinId: "02_ember_400x300.png", sizeBytes: 42_769, description: "400x300 RGB", capacityBytes: 44_880, usableBits: 360_000, notices: [], builtin: true, image: { width: 400, height: 300, mode: "RGB" } },
  { kind: "image", path: "builtin://03_fjord_640x480.png", name: "03_fjord_640x480.png", builtinId: "03_fjord_640x480.png", sizeBytes: 94_661, description: "640x480 RGB", capacityBytes: 115_080, usableBits: 921_600, notices: [], builtin: true, image: { width: 640, height: 480, mode: "RGB" } },
  { kind: "image", path: "builtin://04_aurora_800x600.png", name: "04_aurora_800x600.png", builtinId: "04_aurora_800x600.png", sizeBytes: 122_090, description: "800x600 RGB", capacityBytes: 179_880, usableBits: 1_440_000, notices: [], builtin: true, image: { width: 800, height: 600, mode: "RGB" } },
  { kind: "image", path: "builtin://05_dunes_1024x768.png", name: "05_dunes_1024x768.png", builtinId: "05_dunes_1024x768.png", sizeBytes: 155_360, description: "1024x768 RGB", capacityBytes: 294_792, usableBits: 2_359_296, notices: [], builtin: true, image: { width: 1024, height: 768, mode: "RGB" } },
  { kind: "image", path: "builtin://06_canopy_1280x720.png", name: "06_canopy_1280x720.png", builtinId: "06_canopy_1280x720.png", sizeBytes: 174_083, description: "1280x720 RGB", capacityBytes: 345_480, usableBits: 2_764_800, notices: [], builtin: true, image: { width: 1280, height: 720, mode: "RGB" } },
  { kind: "image", path: "builtin://07_glacier_1600x900.png", name: "07_glacier_1600x900.png", builtinId: "07_glacier_1600x900.png", sizeBytes: 264_633, description: "1600x900 RGB", capacityBytes: 539_880, usableBits: 4_320_000, notices: [], builtin: true, image: { width: 1600, height: 900, mode: "RGB" } },
  { kind: "image", path: "builtin://08_nebula_1920x1080.png", name: "08_nebula_1920x1080.png", builtinId: "08_nebula_1920x1080.png", sizeBytes: 320_045, description: "1920x1080 RGB", capacityBytes: 777_480, usableBits: 6_220_800, notices: [], builtin: true, image: { width: 1920, height: 1080, mode: "RGB" } },
  { kind: "image", path: "builtin://09_canyon_2400x1600.png", name: "09_canyon_2400x1600.png", builtinId: "09_canyon_2400x1600.png", sizeBytes: 440_112, description: "2400x1600 RGB", capacityBytes: 1_439_880, usableBits: 11_520_000, notices: [], builtin: true, image: { width: 2400, height: 1600, mode: "RGB" } },
  { kind: "image", path: "builtin://10_alpine_3200x2000.png", name: "10_alpine_3200x2000.png", builtinId: "10_alpine_3200x2000.png", sizeBytes: 711_548, description: "3200x2000 RGB", capacityBytes: 2_399_880, usableBits: 19_200_000, notices: [], builtin: true, image: { width: 3200, height: 2000, mode: "RGB" } },
];

const demoBridge: PhantomBridge = {
  async version() {
    return { ok: true, version: "2.1.0 (demo)", protocol: 1 };
  },
  async exampleInfo() {
    return {
      ok: true,
      demo: true,
      carrier: { name: "example_carrier.png", sizeBytes: 201_798 },
      secret: { name: "example_payload.pdf", sizeBytes: 809 },
    };
  },
  async probe({ path, displayName, displaySize }) {
    await demoSleep(250);
    const name = displayName ?? path.split(/[\\/]/).pop() ?? path;
    const lower = name.toLowerCase();
    const isImage = lower.endsWith(".png");
    const isAudio = /\.(wav|flac)$/.test(lower);
    if (!isImage && !isAudio) return { ok: false, error: demoUnsupported(name) };
    const kind = isImage ? ("image" as const) : ("audio" as const);
    const capacityBytes = demoCapacityOf(name);
    return {
      ok: true,
      kind,
      path,
      name,
      sizeBytes: displaySize ?? 201_798,
      description: isImage ? "300x300 RGB" : "PCM, demo",
      capacityBytes,
      usableBits: capacityBytes * 8 + 400,
      notices: [],
      image: isImage ? { width: 300, height: 300, mode: "RGB" } : undefined,
      audio: isAudio ? { frames: 96_000, channels: 2, bits: 16, sampleRate: 48_000 } : undefined,
    };
  },
  async selectBuiltinCarrier({ text, requiredBytes, excludeId, offset = 0 }) {
    const rawBytes = requiredBytes ?? (text !== undefined ? new TextEncoder().encode(text).length + 63 : 1024);
    const maxCapacityBytes = DEMO_BUILTIN_CARRIERS[DEMO_BUILTIN_CARRIERS.length - 1].capacityBytes;
    const suitable = DEMO_BUILTIN_CARRIERS.filter((c) => c.capacityBytes >= rawBytes);
    if (suitable.length === 0) {
      return {
        ok: true,
        fits: false,
        requiredBytes: rawBytes,
        maxCapacityBytes,
        suitableCount: 0,
        index: 0,
        carrier: null,
      };
    }
    let idx = offset % suitable.length;
    if (excludeId) {
      const current = suitable.findIndex((c) => c.builtinId === excludeId);
      if (current >= 0) idx = (current + 1) % suitable.length;
    }
    return {
      ok: true,
      fits: true,
      requiredBytes: rawBytes,
      maxCapacityBytes,
      suitableCount: suitable.length,
      index: idx,
      carrier: suitable[idx],
    };
  },
  async hide({ outputPath, password, text, example }) {
    await demoSleep(1_400);
    if (password.length < 12) {
      return {
        ok: false,
        error: { code: "invalid_password", message: "too short (demo)", detail: null, params: { minimum: 12 } },
      };
    }
    const userBytes = example ? 809 : (text ?? "").length || 1024;
    return {
      ok: true,
      outputPath,
      kind: "image",
      payloadKind: text !== undefined ? "text" : "file",
      payloadBytes: userBytes,
      userBytes,
      capacityBytes: demoCapacityOf(outputPath),
      verified: true,
      kdf: "argon2id",
      demo: true,
    };
  },
  async extract({ password, mode, outputPath }) {
    await demoSleep(1_200);
    if (password.length < 12) {
      return {
        ok: false,
        error: { code: "invalid_password", message: "too short (demo)", detail: null, params: { minimum: 12 } },
      };
    }
    if (mode === "save_file") {
      return {
        ok: true,
        kind: "file",
        suggestedName: "example-payload.pdf",
        outputPath,
        originalSize: 809,
        sha256: DEMO_SHA,
        integrityOk: true,
        containerVersion: 5,
        kdf: "argon2id",
      };
    }
    return {
      ok: true,
      kind: "text",
      text: DEMO_TEXT,
      originalSize: DEMO_TEXT.length,
      sha256: DEMO_SHA,
      integrityOk: true,
      containerVersion: 5,
      kdf: "argon2id",
    };
  },
  async verifyContainer() {
    // Browser demo cannot inspect the real carrier; never report a fake success.
    return { ok: false, error: { code: "invalid_input", message: "Verification requires the desktop app", detail: null } };
  },
  async pickFile(kind, media) {
    return new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept =
        kind === "carrier"
          ? media === "image"
            ? ".png,image/png"
            : media === "audio"
              ? ".wav,.flac"
              : ".png,.wav,.flac"
          : "";
      input.addEventListener("change", () => {
        const file = input.files?.[0];
        if (!file) return resolve(null);
        resolve({ path: `demo://${file.name}`, name: file.name, sizeBytes: file.size });
      });
      input.click();
    });
  },
  async saveFile(suggestedName) {
    return suggestedName; // demo: pretend the user confirmed the suggestion
  },
  async revealPath() {
    /* no-op in the browser */
  },
  async appInfo() {
    return { app: "2.1.0", core: "2.1.0", name: "PHANTOM (browser demo)" };
  },
};

export const bridge: PhantomBridge = isDemoMode() ? demoBridge : tauriBridge;
