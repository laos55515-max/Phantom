import { describe, expect, it } from "vitest";
import type { MetadataFieldInfo, MetadataReportInfo } from "./bridge";
import {
  defaultSelection,
  fileName,
  folderName,
  fieldReason,
  fieldValue,
  groupByCategory,
  planSelection,
  samePath,
  suggestedCleanName,
} from "./metadata";

function field(partial: Partial<MetadataFieldInfo> & Pick<MetadataFieldInfo, "key">): MetadataFieldInfo {
  return {
    label: partial.key,
    value: "value",
    category: "personal",
    container: "tEXt",
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

function report(fields: MetadataFieldInfo[]): MetadataReportInfo {
  return {
    path: "/tmp/photo.png",
    format: "png",
    sizeBytes: 100,
    sha256: "a".repeat(64),
    counts: { total: fields.length, personal: 0, descriptive: 0, technical: 0, rights: 0, provenance: 0, unknown: 0, removable: 0 },
    technical: {},
    technicalSummary: {},
    checksumOk: true,
    warnings: [],
    fields,
    chunks: [],
    provenance: { state: "none", detected: false, manifests: [], signed: false, details: [] },
  };
}

const fields = [
  field({ key: "gps", category: "personal" }),
  field({ key: "comment", category: "personal" }),
  field({ key: "orientation", category: "technical", removable: false }),
  field({ key: "copyright", category: "rights", removable: false }),
  field({ key: "view", category: "personal", removable: false, derived: true }),
];

describe("metadata helpers — the core's policy, mirrored for the preview", () => {
  it("preselects personal removable entries only", () => {
    expect(defaultSelection(report(fields))).toEqual(["gps", "comment"]);
  });

  it("plans removals, protected selections and derived views exactly like the core", () => {
    const plan = planSelection(report(fields), ["gps", "orientation", "view"]);
    expect(plan.allowed).toBe(true);
    expect(plan.remove.map((item) => item.key)).toEqual(["gps"]);
    expect(plan.blocked.map((item) => item.key)).toEqual(["orientation"]);
    expect(plan.keep.map((item) => item.key)).toEqual(["comment", "orientation", "copyright"]);
  });

  it("refuses everything when a content credential is present", () => {
    const signed = report(fields);
    signed.provenance = { state: "unverified", detected: true, manifests: [], signed: true, details: [] };
    const plan = planSelection(signed, ["gps"]);
    expect(plan.allowed).toBe(false);
    expect(plan.reason).toBe("signed_media");
    expect(plan.remove).toEqual([]);
  });

  it("groups in the model's category order and separates derived views", () => {
    const groups = groupByCategory(fields);
    expect(groups.map((group) => group.category)).toEqual(["personal", "technical", "rights"]);
    expect(groups[0].entries.map((item) => item.key)).toEqual(["gps", "comment"]);
    expect(groups[0].derived.map((item) => item.key)).toEqual(["view"]);
  });

  it("suggests a copy name next to the source", () => {
    expect(suggestedCleanName("/home/ada/Holiday.PNG", "png")).toBe("Holiday-clean.png");
    expect(suggestedCleanName("C:\\Users\\ada\\take.wav", "wav")).toBe("take-clean.wav");
    expect(suggestedCleanName("session", "flac")).toBe("session-clean.flac");
  });

  it("recognises the same file through separators and trailing slashes", () => {
    expect(samePath("/tmp/a.png", "/tmp/a.png")).toBe(true);
    expect(samePath("C:\\tmp\\a.png", "C:/tmp/a.png")).toBe(true);
    expect(samePath("/tmp/a.png", "/tmp/b.png")).toBe(false);
  });

  it("translates prose values but never data", () => {
    const t = (key: string, params?: Record<string, string | number>) =>
      key === "meta.value.bytes" ? `${params?.size} bytes` : key;
    expect(fieldValue(field({ key: "chunk", value: "17 bytes", valueKey: "meta.value.bytes", valueParams: { size: "17" } }), t)).toBe("17 bytes");
    expect(fieldValue(field({ key: "gps", value: "N" }), t)).toBe("N");
    expect(fieldReason(field({ key: "orientation", reasonKey: "meta.reason.kept_by_policy" }), t)).toBe(
      "meta.reason.kept_by_policy",
    );
    expect(fieldReason(field({ key: "orientation", reason: "kept by policy" }), t)).toBe("kept by policy");
  });
});

describe("paths of the cleaned copy", () => {
  it("splits a path into the name the dialog shows and its folder", () => {
    expect(fileName("/home/ada/Holiday.png")).toBe("Holiday.png");
    expect(fileName("C:\\Users\\ada\\Holiday.png")).toBe("Holiday.png");
    expect(fileName("/tmp/folder/")).toBe("folder");
    expect(folderName("/home/ada/Holiday.png")).toBe("/home/ada/");
    expect(folderName("C:\\Users\\ada\\Holiday.png")).toBe("C:\\Users\\ada\\");
    expect(folderName("Holiday.png")).toBe("");
  });
});
