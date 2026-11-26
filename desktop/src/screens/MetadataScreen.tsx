import { useCallback, useEffect, useRef, useState } from "react";
import {
  bridge,
  type BridgeError,
  type CleaningPlanInfo,
  type FilePick,
  type MetadataCleanOutcome,
  type MetadataFieldInfo,
  type MetadataReportInfo,
} from "../bridge";
import { DropZone } from "../DropZone";
import { formatBytes } from "../format";
import { useI18n } from "../i18n";
import {
  IconAlert,
  IconAudio,
  IconCheckCircle,
  IconFile,
  IconFolder,
  IconImage,
  IconLock,
  IconShield,
  IconSpinner,
} from "../icons";
import { ErrorBanner } from "../ui";
import {
  defaultSelection,
  fieldReason,
  fieldValue,
  fileName,
  folderName,
  groupByCategory,
  planSelection,
  samePath,
  suggestedCleanName,
} from "../metadata";

type Status = "empty" | "inspecting" | "ready" | "failed" | "cleaning" | "done";

/** The refusal the core explains, translated in the user's language. */
function errorExplanation(
  error: BridgeError,
  t: (key: string, params?: Record<string, string | number>) => string,
): string | null {
  const named = typeof error.params?.path === "string" ? error.params.path : null;
  const path = named ?? error.detail ?? "";
  switch (error.code) {
    case "source_overwrite":
      return t("meta.error.source_overwrite", { path });
    case "output_exists":
      return t("meta.error.output_exists", { path });
    case "output_path":
      return t("meta.error.extension", { path });
    case "publish_unavailable":
      return t("meta.error.publish_unavailable", { path });
    case "signed_media":
      return t("meta.error.signed");
    case "metadata_conflict":
      return t("meta.error.conflict");
    case "metadata":
      return t("meta.error.metadata");
    case "unsupported_format":
      return t("meta.error.unsupported");
    default:
      return null;
  }
}

function fieldIcon(format: MetadataReportInfo["format"]) {
  return format === "png" ? <IconImage size={28} /> : <IconAudio size={28} />;
}

export function MetadataScreen() {
  const { t } = useI18n();
  const units = { b: t("unit.b"), kb: t("unit.kb"), mb: t("unit.mb"), gb: t("unit.gb") };
  const fmt = (bytes: number) => formatBytes(bytes, units);

  const [file, setFile] = useState<FilePick | null>(null);
  const [report, setReport] = useState<MetadataReportInfo | null>(null);
  const [plan, setPlan] = useState<CleaningPlanInfo | null>(null);
  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [status, setStatus] = useState<Status>("empty");
  const [error, setError] = useState<BridgeError | null>(null);
  const [outcome, setOutcome] = useState<MetadataCleanOutcome | null>(null);
  const [savedPath, setSavedPath] = useState<string | null>(null);
  const [sourceSha, setSourceSha] = useState("");
  const [planPending, setPlanPending] = useState(false);

  const generation = useRef(0);
  const planSequence = useRef(0);
  const planTimer = useRef<number | null>(null);

  const cancelPlanTimer = useCallback(() => {
    if (planTimer.current !== null) {
      window.clearTimeout(planTimer.current);
      planTimer.current = null;
    }
  }, []);

  useEffect(() => cancelPlanTimer, [cancelPlanTimer]);

  const reset = useCallback(() => {
    generation.current += 1;
    planSequence.current += 1;
    cancelPlanTimer();
    setFile(null);
    setReport(null);
    setPlan(null);
    setSelection(new Set());
    setStatus("empty");
    setError(null);
    setOutcome(null);
    setSavedPath(null);
    setSourceSha("");
    setPlanPending(false);
  }, [cancelPlanTimer]);

  const inspect = useCallback(
    async (picked: FilePick) => {
      generation.current += 1;
      const current = generation.current;
      planSequence.current += 1;
      cancelPlanTimer();
      setFile(picked);
      setReport(null);
      setPlan(null);
      setSelection(new Set());
      setOutcome(null);
      setSavedPath(null);
      setSourceSha("");
      setError(null);
      setStatus("inspecting");
      let response;
      try {
        response = await bridge.metadataInspect({ path: picked.path });
      } catch {
        if (current !== generation.current) return;
        setError({ code: "internal", message: "metadata inspection failed", detail: null });
        setStatus("failed");
        return;
      }
      if (current !== generation.current) return;
      if (!response.ok) {
        setError(response.error);
        setStatus("failed");
        return;
      }
      setReport(response.report);
      setPlan(response.plan);
      setSelection(new Set(response.defaultSelection));
      setSourceSha(response.report.sha256);
      setStatus("ready");
    },
    [cancelPlanTimer],
  );

  const pickFile = useCallback(async () => {
    const picked = await bridge.pickFile("carrier", "metadata");
    if (picked) await inspect(picked);
  }, [inspect]);

  const applySelection = useCallback(
    (next: Set<string>) => {
      const currentReport = report;
      if (!currentReport) return;
      setSelection(next);
      setPlan(planSelection(currentReport, next)); // instant, replaced by the core
      planSequence.current += 1;
      const sequence = planSequence.current;
      const generationAtCall = generation.current;
      cancelPlanTimer();
      setPlanPending(true);
      planTimer.current = window.setTimeout(() => {
        planTimer.current = null;
        void bridge
          .metadataPlan({ path: currentReport.path, selection: [...next] })
          .then((response) => {
            if (sequence !== planSequence.current || generationAtCall !== generation.current) return;
            if (response.ok) setPlan(response.plan);
          })
          .catch(() => undefined)
          .finally(() => {
            if (sequence === planSequence.current && generationAtCall === generation.current) {
              setPlanPending(false);
            }
          });
      }, 180);
    },
    [cancelPlanTimer, report],
  );

  const toggle = useCallback(
    (field: MetadataFieldInfo) => {
      if (!field.removable || field.derived) return;
      const next = new Set(selection);
      if (next.has(field.key)) next.delete(field.key);
      else next.add(field.key);
      applySelection(next);
    },
    [applySelection, selection],
  );

  const selectDefaults = useCallback(() => {
    if (report) applySelection(new Set(defaultSelection(report)));
  }, [applySelection, report]);

  const selectAllRemovable = useCallback(() => {
    if (!report) return;
    applySelection(
      new Set(report.fields.filter((field) => field.removable && !field.derived).map((field) => field.key)),
    );
  }, [applySelection, report]);

  const clearSelection = useCallback(() => applySelection(new Set()), [applySelection]);

  /** Ask the core one last time, so the preview shown before the dialog is its answer. */
  const requestPlan = useCallback(async (): Promise<CleaningPlanInfo | null> => {
    if (!report) return null;
    const keys = [...selection];
    planSequence.current += 1;
    const sequence = planSequence.current;
    setPlanPending(true);
    try {
      const response = await bridge.metadataPlan({ path: report.path, selection: keys });
      if (sequence !== planSequence.current) return null;
      if (!response.ok) {
        setError(response.error);
        return null;
      }
      setPlan(response.plan);
      return response.plan;
    } catch {
      setError({ code: "internal", message: "metadata planning failed", detail: null });
      return null;
    } finally {
      if (sequence === planSequence.current) setPlanPending(false);
    }
  }, [report, selection]);

  const saveCopy = useCallback(async () => {
    if (!file || !report || status === "cleaning") return;
    cancelPlanTimer();
    setError(null);
    const confirmed = await requestPlan();
    if (!confirmed || !confirmed.allowed || confirmed.remove.length === 0) return;

    const generationAtCall = generation.current;
    setStatus("cleaning");
    const choose = async (name: string, directory?: string): Promise<string | null> => {
      try {
        return await bridge.saveFile(name, "metadata", directory);
      } catch {
        if (generationAtCall !== generation.current) return null;
        setError({ code: "io", message: "the save dialog failed", detail: null });
        setStatus("ready");
        return null;
      }
    };

    let output = await choose(suggestedCleanName(report.path || file.path, report.format));
    if (generationAtCall !== generation.current) return;
    if (!output) {
      setStatus("ready"); // the user cancelled: nothing chosen, nothing written
      return;
    }
    // The format is already known, so the extension in the dialog can be wrong
    // for the file. Ask the core where the copy would really go, then make the
    // dialog confirm exactly that path before anything is written.
    let target = await bridge.metadataOutputPath({ path: file.path, outputPath: output });
    if (generationAtCall !== generation.current) return;
    if (!target.ok) {
      setError(target.error);
      setStatus("ready");
      return;
    }
    if (!samePath(target.output, output)) {
      const resolved = target.output;
      output = await choose(fileName(resolved), folderName(resolved));
      if (generationAtCall !== generation.current) return;
      if (!output) {
        setStatus("ready");
        return;
      }
      target = await bridge.metadataOutputPath({ path: file.path, outputPath: output });
      if (generationAtCall !== generation.current) return;
      if (!target.ok) {
        setError(target.error);
        setStatus("ready");
        return;
      }
      if (!samePath(target.output, output)) {
        setError({
          code: "output_path",
          message: "the copy must keep the source format's extension",
          detail: target.output,
          params: { path: target.output },
        });
        setStatus("ready");
        return;
      }
    }
    const finalPath = target.output;
    if (samePath(finalPath, file.path)) {
      // The core refuses this too; here it is answered before a single byte of
      // the source is read, which is what the user expects from a dialog.
      setError({
        code: "source_overwrite",
        message: "the cleaned copy must not replace the original",
        detail: finalPath,
        params: { source: file.path, output: finalPath },
      });
      setStatus("ready");
      return;
    }
    let response;
    try {
      response = await bridge.metadataClean({
        sourcePath: file.path,
        outputPath: finalPath,
        selection: [...selection],
        // Never silently replace a file that appeared at the resolved path:
        // the dialog confirmed this name, not "this name or whatever exists".
        overwrite: false,
      });
    } catch {
      if (generationAtCall !== generation.current) return;
      setError({ code: "internal", message: "metadata cleanup failed", detail: null });
      setStatus("ready");
      return;
    }
    if (generationAtCall !== generation.current) return;
    if (!response.ok) {
      setError(response.error);
      setStatus("ready");
      return;
    }
    setOutcome(response);
    setSavedPath(response.output);
    setReport(response.reportAfter);
    const nextDefaults = defaultSelection(response.reportAfter);
    setSelection(new Set(nextDefaults));
    setPlan(planSelection(response.reportAfter, nextDefaults));
    setStatus("done");
  }, [cancelPlanTimer, file, report, requestPlan, selection, status]);

  const dirty = plan !== null && plan.remove.length > 0;
  const saveDisabled = !report || !plan?.allowed || !dirty || status === "cleaning" || planPending;
  const groups = report ? groupByCategory(report.fields) : [];
  const explanation = error ? errorExplanation(error, t) : null;
  const sourceUnchanged = outcome !== null && outcome.sourceSha256 === sourceSha;
  const contentKind = outcome?.reportAfter.format === "png" ? t("meta.result.pixels") : t("meta.result.samples");

  return (
    <div className="screen" aria-busy={status === "inspecting" || status === "cleaning"}>
      <h1 className="screen-title">{t("meta.title")}</h1>
      <p className="screen-sub">{t("meta.sub")}</p>

      {!file ? (
        <div className="card card-pad">
          <DropZone
            ariaLabel={t("meta.drop_title")}
            onDrop={(dropped) => {
              void inspect({ path: dropped.path, name: dropped.name, sizeBytes: dropped.sizeBytes });
            }}
          >
            <IconFile size={26} strokeWidth={1.4} />
            <div className="dropzone-title">{t("meta.drop_title")}</div>
            <div className="dropzone-hint">{t("meta.drop_hint")}</div>
            <button type="button" className="btn btn-ghost" data-testid="meta-open" onClick={() => void pickFile()}>
              {t("meta.choose_file")}
            </button>
          </DropZone>
        </div>
      ) : (
        <div className="card card-pad meta-file-card">
          <div className="file-glyph">{report ? fieldIcon(report.format) : <IconFile size={28} />}</div>
          <div className="meta-file-main">
            <div className="file-name" title={file.path}>
              {file.name || file.path}
            </div>
            <div className="file-meta">
              {report ? t(`meta.format.${report.format}`) : t("meta.format.png")}
              {report ? ` · ${fmt(report.sizeBytes)}` : file.sizeBytes !== null ? ` · ${fmt(file.sizeBytes)}` : ""}
              {report ? ` · ${t("meta.counts", {
                total: String(report.counts.total),
                personal: String(report.counts.personal),
                removable: String(report.counts.removable),
              })}` : ""}
            </div>
            {report ? (
              <div className="meta-file-badges">
                {report.checksumOk === false ? (
                  <span className="meta-badge warn">
                    <IconAlert size={13} /> {t("meta.checksum_broken")}
                  </span>
                ) : report.checksumOk === true ? (
                  <span className="meta-badge ok">
                    <IconCheckCircle size={13} /> {t("meta.checksum_ok")}
                  </span>
                ) : null}
                {report.provenance.detected ? (
                  <span className="meta-badge warn">
                    <IconShield size={13} /> {t(`meta.prov.${report.provenance.state}`)}
                  </span>
                ) : null}
                <span className="mono meta-hash" title={report.sha256}>
                  {t("meta.sha", { digest: report.sha256.slice(0, 16) })}
                </span>
              </div>
            ) : null}
          </div>
          <button type="button" className="btn btn-subtle" onClick={() => void pickFile()}>
            {t("meta.change_file")}
          </button>
        </div>
      )}

      {status === "inspecting" ? (
        <p className="hint meta-status" role="status">
          <IconSpinner size={14} className="spin" /> {t("meta.inspecting")}
        </p>
      ) : null}

      {error ? <ErrorBanner error={error} title={t("meta.error.title")} /> : null}
      {explanation ? (
        <div className="card card-compact meta-note" role="note">
          <IconInfoSafe />
          <span>{explanation}</span>
        </div>
      ) : null}

      {report && status !== "inspecting" ? (
        <>
          {outcome ? (
            <div className="card card-pad meta-result" role="status">
              <div className="row">
                <IconCheckCircle size={22} style={{ color: "var(--green)" }} />
                <strong>{t("meta.result.title")}</strong>
              </div>
              <div className="success-verified">
                <IconCheckCircle size={15} />
                {outcome.verified
                  ? contentKind
                  : t("meta.result.not_verified")}
              </div>
              <div className={sourceUnchanged ? "success-verified" : "meta-badge warn"}>
                {sourceUnchanged
                  ? t("meta.result.source_unchanged", { digest: sourceSha.slice(0, 16) })
                  : t("meta.result.source_changed")}
              </div>
              <div className="file-meta">
                {t("meta.result.removed", { count: String(outcome.removed.length) })} ·{" "}
                {t("meta.result.kept", { count: String(outcome.kept.length) })}
              </div>
              {savedPath ? (
                <div className="meta-saved">
                  <span className="file-meta">{t("meta.result.output")}</span>
                  <span className="mono" style={{ overflowWrap: "anywhere" }}>{savedPath}</span>
                  <div className="row" style={{ marginTop: 8 }}>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() => void bridge.revealPath(savedPath).catch(() => undefined)}
                    >
                      <IconFolder size={16} /> {t("hide.open_folder")}
                    </button>
                    <button type="button" className="btn btn-subtle" onClick={reset}>
                      {t("meta.again")}
                    </button>
                  </div>
                </div>
              ) : null}
              <details className="meta-result-fields">
                <summary>{t("meta.result.removed_list", { count: String(outcome.removed.length) })}</summary>
                <ul>
                  {outcome.removed.map((field) => (
                    <li key={field.key}>
                      {field.label}
                      <span className="mono"> · {field.container}</span>
                    </li>
                  ))}
                </ul>
              </details>
            </div>
          ) : plan && !plan.allowed ? (
            <div className="card card-pad meta-refusal" role="note">
              <div className="row">
                <IconLock size={20} />
                <strong>{t("meta.refusal.title")}</strong>
              </div>
              <p className="hint" style={{ marginTop: 6 }}>
                {plan.reason === "signed_media" ? t("meta.refusal.signed") : t("meta.refusal.generic")}
              </p>
            </div>
          ) : null}

          {report.warnings.length > 0 ? (
            <details className="card card-compact meta-warnings">
              <summary>{t("meta.warnings", { count: String(report.warnings.length) })}</summary>
              <ul>
                {report.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </details>
          ) : null}

          {!outcome ? (
            <div className="meta-toolbar" role="group" aria-label={t("meta.toolbar.label")}>
              <button type="button" className="btn btn-subtle" onClick={selectDefaults}>
                {t("meta.toolbar.default")}
              </button>
              <button type="button" className="btn btn-subtle" onClick={selectAllRemovable}>
                {t("meta.toolbar.all")}
              </button>
              <button type="button" className="btn btn-subtle" onClick={clearSelection}>
                {t("meta.toolbar.clear")}
              </button>
              <span className="file-meta" style={{ marginLeft: "auto" }} role="status">
                {t("meta.toolbar.selected", { count: String(selection.size) })}
              </span>
            </div>
          ) : null}

          {report.fields.length === 0 ? (
            <p className="hint meta-status" role="status">{t("meta.fields.empty")}</p>
          ) : report.fields.every((field) => !field.removable) ? (
            <p className="hint meta-status" role="status">{t("meta.fields.protected")}</p>
          ) : null}

          <div className="meta-groups">
            {groups.map((group) => (
              <section className="card meta-group" key={group.category} aria-label={t(`meta.cat.${group.category}`)}>
                <header className="meta-group-head">
                  <h2 className="section-label">{t(`meta.cat.${group.category}`)}</h2>
                  <span className="meta-group-count">
                    {t("meta.group_count", {
                      count: String(group.entries.length),
                      derived: String(group.derived.length),
                    })}
                  </span>
                </header>
                {group.entries.length > 0 ? (
                  <ul className="meta-field-list">
                    {group.entries.map((field) => {
                      const selected = selection.has(field.key);
                      return (
                        <li key={field.key} className={`meta-field${selected ? " is-selected" : ""}${field.removable ? "" : " is-protected"}`}>
                          <input
                            type="checkbox"
                            className="meta-check"
                            checked={selected}
                            disabled={!field.removable || outcome !== null}
                            aria-label={`${field.label} (${field.container})`}
                            onChange={() => toggle(field)}
                          />
                          <div className="meta-field-body">
                            <div className="meta-field-head">
                              <span className="meta-field-label">{field.label}</span>
                              <span className="meta-field-container">{field.container}</span>
                            </div>
                            <div className="meta-field-value">{fieldValue(field, t)}</div>
                            {!field.removable ? (
                              <div className="meta-field-reason">
                                <IconLock size={11} /> {fieldReason(field, t)}
                              </div>
                            ) : null}
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
                {group.derived.length > 0 ? (
                  <ul className="meta-field-list meta-derived">
                    {group.derived.map((field) => (
                      <li key={field.key} className="meta-field is-view">
                        <span className="meta-check meta-dash" aria-hidden="true">
                          —
                        </span>
                        <div className="meta-field-body">
                          <div className="meta-field-head">
                            <span className="meta-field-label">{field.label}</span>
                            <span className="meta-field-container">{t("meta.derived")}</span>
                          </div>
                          <div className="meta-field-value">{fieldValue(field, t)}</div>
                          <div className="meta-field-reason">{fieldReason(field, t)}</div>
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </section>
            ))}
          </div>

          {plan && !outcome ? (
            <div className="card card-pad meta-preview" aria-label={t("meta.preview.title")}>
              <h2 className="section-label">{t("meta.preview.title")}</h2>
              <p className="hint" style={{ marginTop: 0 }}>{t("meta.preview.sub")}</p>
              <div className="meta-preview-columns">
                <div>
                  <div className="meta-preview-title">
                    <span className="meta-dot remove" aria-hidden="true" />
                    {t("meta.preview.remove", { count: String(plan.remove.length) })}
                  </div>
                  {plan.remove.length === 0 ? (
                    <p className="hint">{t("meta.preview.nothing")}</p>
                  ) : (
                    <ul className="meta-preview-list">
                      {plan.remove.map((field) => (
                        <li key={field.key}>{field.label}<span className="mono"> · {field.container}</span></li>
                      ))}
                    </ul>
                  )}
                  {plan.blocked.length > 0 ? (
                    <div className="meta-blocked">
                      <div className="meta-preview-title">
                        <IconLock size={12} /> {t("meta.preview.blocked", { count: String(plan.blocked.length) })}
                      </div>
                      <ul className="meta-preview-list">
                        {plan.blocked.map((field) => (
                          <li key={field.key}>{field.label}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </div>
                <div>
                  <div className="meta-preview-title">
                    <span className="meta-dot keep" aria-hidden="true" />
                    {t("meta.preview.keep", { count: String(plan.keep.length) })}
                  </div>
                  <ul className="meta-preview-list">
                    {plan.keep.map((field) => (
                      <li key={field.key}>{field.label}<span className="mono"> · {field.container}</span></li>
                    ))}
                  </ul>
                </div>
              </div>
              <p className="hint" style={{ marginBottom: 0 }}>{t("meta.preview.source_note")}</p>
            </div>
          ) : null}
        </>
      ) : null}

      {report && !outcome && status !== "inspecting" ? (
        <div className="meta-actions">
          <div className="meta-actions-text">
            <strong>{plan ? t("meta.preview.counts", {
              remove: String(plan.remove.length),
              keep: String(plan.keep.length),
            }) : t("meta.preview.nothing")}</strong>
            <span className="hint">{t("meta.save_hint")}</span>
          </div>
          <button
            type="button"
            className="btn btn-primary btn-big"
            data-testid="meta-save"
            onClick={() => void saveCopy()}
            disabled={saveDisabled}
          >
            {status === "cleaning" || planPending ? (
              <IconSpinner size={17} className="spin" />
            ) : (
              <IconShield size={17} />
            )}
            {status === "cleaning" ? t("meta.cleaning") : t("meta.save")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** Inline info glyph, so the explanation card stays a plain note. */
function IconInfoSafe() {
  return (
    <span className="meta-note-icon" aria-hidden="true">
      <IconShield size={16} />
    </span>
  );
}

