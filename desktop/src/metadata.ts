/**
 * Display-independent logic for the Metadata tab.
 *
 * The core owns the policy: `metadata_inspect` returns the safe
 * `default_selection`, and `metadata_plan` is the authority on what a selection
 * removes, keeps or is blocked from removing.  The helpers here only mirror the
 * same rules so the checkboxes and the preview react instantly while the bridge
 * answer is in flight; the screen always replaces them with the core's plan.
 */

import {
  METADATA_CATEGORIES,
  type CleaningPlanInfo,
  type MetadataCategory,
  type MetadataFieldInfo,
  type MetadataReportInfo,
} from "./bridge";

export interface CategoryGroup {
  category: MetadataCategory;
  entries: MetadataFieldInfo[];
  derived: MetadataFieldInfo[];
}

/** Personal, removable entries: the selection the cleaner proposes by default. */
export function defaultSelection(report: MetadataReportInfo): string[] {
  return report.fields
    .filter((field) => field.category === "personal" && field.removable && !field.derived)
    .map((field) => field.key);
}

/** Mirror of `phantom.metadata.cleaner.plan`; the bridge result wins when it lands. */
export function planSelection(
  report: MetadataReportInfo,
  selection: Iterable<string>,
): CleaningPlanInfo {
  const selected = new Set(selection);
  if (report.provenance.detected) {
    return {
      allowed: false,
      reason: "signed_media",
      remove: [],
      keep: report.fields.filter((field) => !field.derived),
      blocked: [],
      warnings: report.warnings,
    };
  }
  const remove: MetadataFieldInfo[] = [];
  const keep: MetadataFieldInfo[] = [];
  const blocked: MetadataFieldInfo[] = [];
  for (const field of report.fields) {
    if (field.derived) continue;
    if (!selected.has(field.key)) {
      keep.push(field);
      continue;
    }
    if (field.removable) {
      remove.push(field);
    } else {
      blocked.push(field);
      keep.push(field);
    }
  }
  return {
    allowed: true,
    reason: "",
    remove,
    keep,
    blocked,
    warnings: report.warnings,
  };
}

/** Category order is the model's; groups keep the order the bridge sent. */
export function groupByCategory(fields: MetadataFieldInfo[]): CategoryGroup[] {
  const groups = new Map<MetadataCategory, CategoryGroup>();
  for (const category of METADATA_CATEGORIES) {
    groups.set(category, { category, entries: [], derived: [] });
  }
  for (const field of fields) {
    const group = groups.get(field.category) ?? groups.get("unknown")!;
    if (field.derived) group.derived.push(field);
    else group.entries.push(field);
  }
  return [...groups.values()].filter(
    (group) => group.entries.length > 0 || group.derived.length > 0,
  );
}

/** `photo.png` → `photo-clean.png`, the same suggestion the Tkinter panel makes. */
export function suggestedCleanName(path: string, format: MetadataReportInfo["format"]): string {
  const segments = path.split(/[\\/]/);
  const name = segments[segments.length - 1] ?? path;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  return `${stem}-clean.${format}`;
}

/** Path equality as the user sees it (Windows separators and trailing slashes). */
export function samePath(left: string, right: string): boolean {
  const normalise = (value: string) =>
    value
      .replace(/\\/g, "/")
      .replace(/\/+/g, "/")
      .replace(/\/$/, "");
  return normalise(left) === normalise(right);
}

/** A value the program wrote is translated; a value read from the file is not. */
export function fieldValue(
  field: MetadataFieldInfo,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const text = field.valueKey ? t(field.valueKey, field.valueParams) : field.value;
  return field.truncated ? `${text}…` : text;
}

export function fieldReason(
  field: MetadataFieldInfo,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  if (field.reasonKey) return t(field.reasonKey, field.reasonParams);
  return field.reason;
}

/** The file name part of a path, for both separator conventions. */
export function fileName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  return cut >= 0 ? trimmed.slice(cut + 1) : trimmed;
}

/** The folder part of a path, with its trailing separator, or "". */
export function folderName(path: string): string {
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut >= 0 ? path.slice(0, cut + 1) : "";
}
