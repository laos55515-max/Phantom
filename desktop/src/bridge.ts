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

// --------------------------------------------------------------- metadata

export const METADATA_CATEGORIES = [
  "personal",
  "descriptive",
  "technical",
  "rights",
  "provenance",
  "unknown",
] as const;

export type MetadataCategory = (typeof METADATA_CATEGORIES)[number];

/** One readable entry, mirrored field-for-field from `phantom.metadata.models`. */
export interface MetadataFieldInfo {
  key: string;
  label: string;
  value: string;
  category: MetadataCategory;
  container: string;
  removable: boolean;
  /** A view computed from other fields (the decimal GPS position): not an entry. */
  derived: boolean;
  reason: string;
  /** Locale key of the structured reason, when the core produced one. */
  reasonKey: string;
  reasonParams: Record<string, string>;
  /** Locale key and values of a prose value ("12 bytes"), never of data. */
  valueKey: string;
  valueParams: Record<string, string>;
  size: number;
  truncated: boolean;
}

export interface MetadataProvenanceInfo {
  state: "none" | "manifest" | "unverified" | "valid" | "invalid";
  detected: boolean;
  manifests: string[];
  signed: boolean;
  details: Array<[string, string]>;
}

export interface MetadataChunkInfo {
  kind: string;
  offset: number;
  size: number;
  checksumOk: boolean | null;
  note: string;
}

export interface MetadataReportInfo {
  path: string;
  format: "png" | "wav" | "flac";
  sizeBytes: number;
  sha256: string;
  counts: Record<MetadataCategory | "total" | "removable", number>;
  technical: Record<string, string>;
  technicalSummary: Record<string, string>;
  checksumOk: boolean | null;
  warnings: string[];
  fields: MetadataFieldInfo[];
  chunks: MetadataChunkInfo[];
  provenance: MetadataProvenanceInfo;
}

export interface CleaningPlanInfo {
  allowed: boolean;
  reason: string;
  remove: MetadataFieldInfo[];
  keep: MetadataFieldInfo[];
  blocked: MetadataFieldInfo[];
  warnings: string[];
}

export interface MetadataInspection {
  report: MetadataReportInfo;
  defaultSelection: string[];
  plan: CleaningPlanInfo;
}

export interface MetadataCleanOutcome {
  source: string;
  output: string;
  removed: MetadataFieldInfo[];
  kept: MetadataFieldInfo[];
  verified: boolean;
  checksumOk: boolean | null;
  sourceSha256: string;
  outputSha256: string;
  content: {
    identical: boolean;
    checked: boolean;
    differences: string[];
    source: string;
    output: string;
  };
  warnings: string[];
  reportAfter: MetadataReportInfo;
  demo?: boolean;
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
  metadataInspect(args: { path: string }): Promise<BridgeResult<MetadataInspection>>;
  metadataPlan(args: {
    path: string;
    selection: string[];
  }): Promise<BridgeResult<{ report: MetadataReportInfo; plan: CleaningPlanInfo }>>;
  metadataClean(args: {
    sourcePath: string;
    outputPath: string;
    selection: string[];
    overwrite?: boolean;
  }): Promise<BridgeResult<MetadataCleanOutcome>>;
  pickFile(
    kind: "secret" | "carrier",
    media: "image" | "audio" | "any" | "metadata",
  ): Promise<FilePick | null>;
  saveFile(
    suggestedName: string,
    kind: "image" | "audio" | "text" | "file" | "metadata",
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

function mapParams(raw: unknown): Record<string, string> {
  if (!isRecord(raw)) return {};
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(raw)) {
    out[key] = String(value);
  }
  return out;
}

function mapCategory(raw: unknown): MetadataCategory {
  return typeof raw === "string" && (METADATA_CATEGORIES as readonly string[]).includes(raw)
    ? (raw as MetadataCategory)
    : "unknown";
}

function mapField(raw: unknown): MetadataFieldInfo {
  const item = isRecord(raw) ? raw : {};
  return {
    key: String(item.key ?? ""),
    label: String(item.label ?? item.key ?? ""),
    value: String(item.value ?? ""),
    category: mapCategory(item.category),
    container: String(item.container ?? ""),
    removable: item.removable === true,
    derived: item.derived === true,
    reason: String(item.reason ?? ""),
    reasonKey: String(item.reason_key ?? item.reasonKey ?? ""),
    reasonParams: mapParams(item.reason_params ?? item.reasonParams),
    valueKey: String(item.value_key ?? item.valueKey ?? ""),
    valueParams: mapParams(item.value_params ?? item.valueParams),
    size: Number(item.size ?? 0),
    truncated: item.truncated === true,
  };
}

function mapFieldList(raw: unknown): MetadataFieldInfo[] {
  return Array.isArray(raw) ? raw.map(mapField) : [];
}

function mapProvenance(raw: unknown): MetadataProvenanceInfo {
  const item = isRecord(raw) ? raw : {};
  const state = ["none", "manifest", "unverified", "valid", "invalid"].includes(String(item.state))
    ? (String(item.state) as MetadataProvenanceInfo["state"])
    : "none";
  return {
    state,
    detected: item.detected === true,
    manifests: Array.isArray(item.manifests) ? item.manifests.map(String) : [],
    signed: item.signed === true,
    details: Array.isArray(item.details)
      ? item.details
          .filter((pair): pair is unknown[] => Array.isArray(pair))
          .map((pair) => [String(pair[0] ?? ""), String(pair[1] ?? "")] as [string, string])
      : [],
  };
}

function mapMetadataReport(raw: unknown): MetadataReportInfo {
  const item = isRecord(raw) ? raw : {};
  const counts = isRecord(item.counts) ? item.counts : {};
  const numeric = (key: string) => Number(counts[key] ?? 0);
  const technical = isRecord(item.technical) ? item.technical : {};
  const summary = isRecord(item.technical_summary) ? item.technical_summary : {};
  return {
    path: String(item.path ?? ""),
    format: item.format === "wav" || item.format === "flac" ? item.format : "png",
    sizeBytes: Number(item.size_bytes ?? item.sizeBytes ?? 0),
    sha256: String(item.sha256 ?? ""),
    counts: {
      total: numeric("total"),
      personal: numeric("personal"),
      descriptive: numeric("descriptive"),
      technical: numeric("technical"),
      rights: numeric("rights"),
      provenance: numeric("provenance"),
      unknown: numeric("unknown"),
      removable: numeric("removable"),
    },
    technical: Object.fromEntries(Object.entries(technical).map(([k, v]) => [k, String(v)])),
    technicalSummary: Object.fromEntries(Object.entries(summary).map(([k, v]) => [k, String(v)])),
    checksumOk: typeof item.checksum_ok === "boolean" ? item.checksum_ok : null,
    warnings: Array.isArray(item.warnings) ? item.warnings.map(String) : [],
    fields: mapFieldList(item.fields),
    chunks: Array.isArray(item.chunks)
      ? item.chunks.filter(isRecord).map((chunk) => ({
          kind: String(chunk.kind ?? ""),
          offset: Number(chunk.offset ?? 0),
          size: Number(chunk.size ?? 0),
          checksumOk: typeof chunk.checksum_ok === "boolean" ? chunk.checksum_ok : null,
          note: String(chunk.note ?? ""),
        }))
      : [],
    provenance: mapProvenance(item.provenance),
  };
}

function mapPlan(raw: unknown): CleaningPlanInfo {
  const item = isRecord(raw) ? raw : {};
  return {
    allowed: item.allowed !== false,
    reason: String(item.reason ?? ""),
    remove: mapFieldList(item.remove),
    keep: mapFieldList(item.keep),
    blocked: mapFieldList(item.blocked),
    warnings: Array.isArray(item.warnings) ? item.warnings.map(String) : [],
  };
}

function mapMetadataOutcome(raw: Record<string, unknown>): MetadataCleanOutcome {
  const content = isRecord(raw.content) ? raw.content : {};
  return {
    source: String(raw.source ?? ""),
    output: String(raw.output ?? ""),
    removed: mapFieldList(raw.removed),
    kept: mapFieldList(raw.kept),
    verified: raw.verified === true,
    checksumOk: typeof raw.checksum_ok === "boolean" ? raw.checksum_ok : null,
    sourceSha256: String(raw.source_sha256 ?? raw.sourceSha256 ?? ""),
    outputSha256: String(raw.output_sha256 ?? raw.outputSha256 ?? ""),
    content: {
      identical: content.identical === true,
      checked: content.checked === true,
      differences: Array.isArray(content.differences) ? content.differences.map(String) : [],
      source: String(content.source ?? ""),
      output: String(content.output ?? ""),
    },
    warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
    reportAfter: mapMetadataReport(raw.report_after ?? raw.reportAfter),
    demo: raw.demo === true ? true : undefined,
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
  async metadataInspect({ path }) {
    const raw = await callBridge({ cmd: "metadata_inspect", path });
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    return {
      ok: true,
      report: mapMetadataReport(raw.report),
      defaultSelection: Array.isArray(raw.default_selection)
        ? raw.default_selection.map(String)
        : [],
      plan: mapPlan(raw.plan),
    };
  },
  async metadataPlan({ path, selection }) {
    const raw = await callBridge({ cmd: "metadata_plan", path, selection });
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    return { ok: true, report: mapMetadataReport(raw.report), plan: mapPlan(raw.plan) };
  },
  async metadataClean({ sourcePath, outputPath, selection, overwrite }) {
    const request: Record<string, unknown> = {
      cmd: "metadata_clean",
      path: sourcePath,
      output_path: outputPath,
      selection,
    };
    if (overwrite !== undefined) request.overwrite = overwrite;
    const raw = await callBridge(request);
    if (raw.ok !== true) return { ok: false, error: asError(raw) };
    return { ok: true, ...mapMetadataOutcome(raw) };
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
// The demo "cleaned" copy has a different whole-file hash and the same decoded
// content hash, exactly like the core: that is the difference the UI explains.
const DEMO_CLEAN_SHA = "1c40b9d2e6a17f38b0d5c4a92f6e8d37a15b0ce4f92d68a7b3c51e0d9f42a6b8";

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

function demoField(
  key: string,
  label: string,
  value: string,
  category: MetadataCategory,
  container: string,
  removable: boolean,
  extra: Partial<MetadataFieldInfo> = {},
): MetadataFieldInfo {
  return {
    key,
    label,
    value,
    category,
    container,
    removable,
    derived: false,
    reason: "",
    reasonKey: "",
    reasonParams: {},
    valueKey: "",
    valueParams: {},
    size: 0,
    truncated: false,
    ...extra,
  };
}

function demoMetadataFields(format: "png" | "wav" | "flac"): MetadataFieldInfo[] {
  const keptByPolicy = { reason: "kept by policy", reasonKey: "meta.reason.kept_by_policy" };
  if (format === "wav") {
    return [
      demoField("wav.info.ICMT", "Comment", "recorded in my kitchen", "personal", "LIST/INFO", true),
      demoField("wav.bext.description", "bext description", "field recording", "personal", "bext", true),
      demoField("wav.info.INAM", "Title", "Session 7", "descriptive", "LIST/INFO", false, keptByPolicy),
      demoField("wav.info.IART", "Artist", "Ada Lovelace", "rights", "LIST/INFO", true),
      demoField("wav.info.ICOP", "Copyright", "(c) 2024 Ada", "rights", "LIST/INFO", false, keptByPolicy),
      demoField("wav.info.IKEY", "Keywords", "field recording", "descriptive", "LIST/INFO", false, keptByPolicy),
      demoField("wav.chunk.junk", "Unknown chunk 'junk'", "20 bytes", "unknown", "RIFF", false, {
        reason: "unknown data: listed, never removed automatically",
        reasonKey: "meta.reason.unknown_data",
        valueKey: "meta.value.bytes",
        valueParams: { size: "20" },
      }),
    ];
  }
  if (format === "flac") {
    return [
      demoField("flac.vorbis.COMMENT", "COMMENT", "recorded in my kitchen", "personal", "Vorbis", true),
      demoField("flac.vorbis.DATE", "DATE", "2024-05-06", "personal", "Vorbis", true),
      demoField("flac.vorbis.TITLE", "TITLE", "Session 7", "descriptive", "Vorbis", false, keptByPolicy),
      demoField("flac.vorbis.ARTIST", "ARTIST", "Ada Lovelace", "rights", "Vorbis", true),
      demoField("flac.vorbis.COPYRIGHT", "COPYRIGHT", "(c) 2024 Ada", "rights", "Vorbis", false, keptByPolicy),
      demoField("flac.vendor", "Vorbis vendor", "reference libFLAC (demo)", "technical", "Vorbis comment", false, keptByPolicy),
    ];
  }
  return [
    demoField("png.gps.0x0001", "GPSLatitudeRef", "N", "personal", "GPS", true),
    demoField("png.gps.0x0002", "GPSLatitude", "50, 27, 0", "personal", "GPS", true),
    demoField("png.gps.0x0004", "GPSLongitude", "30, 31, 0", "personal", "GPS", true),
    demoField("png.gps.coordinates", "GPS coordinates", "50.450000, 30.516667", "personal", "GPS", false, {
      derived: true,
      reason: "derived from the GPS tags above; removing them removes this",
      reasonKey: "meta.reason.gps_derived",
    }),
    demoField("png.text.tEXt.Comment", "Comment", "shot at home, kitchen wifi", "personal", "tEXt", true),
    demoField("png.exif.0x0112", "Orientation", "6 (rotate 90° CW)", "technical", "eXIf/0th", false, keptByPolicy),
    demoField("png.exif.0x0131", "Software", "PhantomCam 1.0", "technical", "eXIf/0th", false, keptByPolicy),
    demoField("png.text.tEXt.Title", "Title", "Holiday", "descriptive", "tEXt", false, keptByPolicy),
    demoField("png.exif.0x8298", "Copyright", "(c) 2024 Ada", "rights", "eXIf/0th", false, keptByPolicy),
    demoField("png.text.tEXt.Author", "Author", "Ada Lovelace", "rights", "tEXt", true),
    demoField("png.chunk.prVt", "Unknown chunk prVt", "17 bytes", "unknown", "container", false, {
      reason: "unknown data: listed, never removed automatically",
      reasonKey: "meta.reason.unknown_data",
      valueKey: "meta.value.bytes",
      valueParams: { size: "17" },
    }),
  ];
}

function demoCounts(fields: MetadataFieldInfo[]): MetadataReportInfo["counts"] {
  const counts = {
    total: 0,
    personal: 0,
    descriptive: 0,
    technical: 0,
    rights: 0,
    provenance: 0,
    unknown: 0,
    removable: 0,
  };
  for (const field of fields) {
    if (field.derived) continue;
    counts.total += 1;
    counts[field.category] += 1;
    if (field.removable) counts.removable += 1;
  }
  return counts;
}

function demoMetadataReport(path: string, format: "png" | "wav" | "flac", signed = false): MetadataReportInfo {
  const fields = demoMetadataFields(format);
  const sampleRate = "8000";
  return {
    path,
    format,
    sizeBytes: format === "png" ? 201_798 : 24_400,
    sha256: DEMO_SHA,
    counts: demoCounts(fields),
    technical:
      format === "png"
        ? { width: "300", height: "300", mode: "RGB" }
        : { sample_rate: sampleRate, channels: "1", bits_per_sample: "16", frames: "400" },
    technicalSummary:
      format === "png"
        ? { width: "300", height: "300", resolution: "300x300" }
        : { sample_rate: sampleRate, channels: "1", bits_per_sample: "16" },
    checksumOk: true,
    warnings: [],
    fields,
    chunks: [{ kind: format === "png" ? "IHDR" : "fmt ", offset: 0, size: 13, checksumOk: true, note: "" }],
    provenance: signed
      ? {
          state: "unverified",
          detected: true,
          manifests: ["urn:uuid:demo"],
          signed: true,
          details: [["signature", "present, not verified"]],
        }
      : { state: "none", detected: false, manifests: [], signed: false, details: [] },
  };
}

function demoMetadataPlan(report: MetadataReportInfo, selection: string[]): CleaningPlanInfo {
  const selected = new Set(selection);
  if (report.provenance.detected) {
    return {
      allowed: false,
      reason: "signed_media",
      remove: [],
      keep: report.fields,
      blocked: [],
      warnings: report.warnings,
    };
  }
  const remove: MetadataFieldInfo[] = [];
  const keep: MetadataFieldInfo[] = [];
  const blocked: MetadataFieldInfo[] = [];
  for (const field of report.fields) {
    if (field.derived) continue;
    if (selected.has(field.key)) {
      if (field.removable) remove.push(field);
      else {
        blocked.push(field);
        keep.push(field);
      }
    } else {
      keep.push(field);
    }
  }
  return { allowed: true, reason: "", remove, keep, blocked, warnings: report.warnings };
}

function demoMetadataFormat(name: string): "png" | "wav" | "flac" | null {
  const lower = name.toLowerCase();
  if (lower.endsWith(".png")) return "png";
  if (lower.endsWith(".wav")) return "wav";
  if (lower.endsWith(".flac")) return "flac";
  return null;
}

function demoDefaultSelection(report: MetadataReportInfo): string[] {
  return report.fields
    .filter((field) => field.category === "personal" && field.removable && !field.derived)
    .map((field) => field.key);
}

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
  async metadataInspect({ path }) {
    await demoSleep(220);
    const name = path.split(/[\\/]/).pop() ?? path;
    const format = demoMetadataFormat(name);
    if (!format) return { ok: false, error: demoUnsupported(name) };
    const report = demoMetadataReport(path, format, /signed|c2pa|provenance/i.test(name));
    const defaultSelection = demoDefaultSelection(report);
    return { ok: true, report, defaultSelection, plan: demoMetadataPlan(report, defaultSelection) };
  },
  async metadataPlan({ path, selection }) {
    await demoSleep(140);
    const name = path.split(/[\\/]/).pop() ?? path;
    const format = demoMetadataFormat(name);
    if (!format) return { ok: false, error: demoUnsupported(name) };
    const report = demoMetadataReport(path, format, /signed|c2pa|provenance/i.test(name));
    return { ok: true, report, plan: demoMetadataPlan(report, selection) };
  },
  async metadataClean({ sourcePath, outputPath, selection, overwrite: _overwrite }) {
    await demoSleep(900);
    const name = sourcePath.split(/[\\/]/).pop() ?? sourcePath;
    const format = demoMetadataFormat(name);
    if (!format) return { ok: false, error: demoUnsupported(name) };
    const same =
      sourcePath.replace(/\\/g, "/").replace(/\/+$/, "") ===
      outputPath.replace(/\\/g, "/").replace(/\/+$/, "");
    if (same) {
      return {
        ok: false,
        error: {
          code: "source_overwrite",
          message: "the cleaned copy must not replace the original",
          detail: outputPath,
          params: { source: sourcePath, output: outputPath },
        },
      };
    }
    const report = demoMetadataReport(sourcePath, format, /signed|c2pa|provenance/i.test(name));
    const plan = demoMetadataPlan(report, selection);
    if (!plan.allowed) {
      return {
        ok: false,
        error: {
          code: "signed_media",
          message: "a content credential is attached; it cannot survive a rewrite",
          detail: report.provenance.state,
          params: { state: report.provenance.state },
        },
      };
    }
    if (plan.remove.length === 0) {
      return {
        ok: false,
        error: { code: "metadata", message: "no removable field was selected", detail: null },
      };
    }
    const removed = new Set(plan.remove.map((field) => field.key));
    const remaining = report.fields.filter((field) => !removed.has(field.key));
    const after: MetadataReportInfo = {
      ...report,
      path: outputPath,
      fields: remaining,
      counts: demoCounts(remaining),
    };
    return {
      ok: true,
      source: sourcePath,
      output: outputPath,
      removed: plan.remove,
      kept: plan.keep,
      verified: true,
      checksumOk: true,
      sourceSha256: DEMO_SHA,
      outputSha256: DEMO_CLEAN_SHA,
      content: {
        identical: true,
        checked: true,
        differences: [],
        source: DEMO_SHA,
        output: DEMO_SHA,
      },
      warnings: [],
      reportAfter: after,
      demo: true,
    };
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
