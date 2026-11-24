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
  image?: { width: number; height: number; mode: string };
  audio?: { frames: number; channels: number; bits: number; sampleRate: number };
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
  hide(args: {
    carrierPath: string;
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
  async hide({ carrierPath, outputPath, password, secretPath, text, example }) {
    const request = example
      ? { cmd: "example_hide", output_path: outputPath, password }
      : {
          cmd: "hide",
          carrier_path: carrierPath,
          output_path: outputPath,
          password,
          secret_path: secretPath,
          text,
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
  async verifyContainer({ password }) {
    await demoSleep(400);
    if (password.length < 12) return { ok: false, error: { code: "invalid_password", message: "too short (demo)", detail: null } };
    return { ok: true, verified: true };
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
