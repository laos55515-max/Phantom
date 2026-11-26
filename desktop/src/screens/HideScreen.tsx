import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { bridge, isDemoMode, type BridgeError, type FilePick, type ProbeInfo } from "../bridge";
import { DropZone } from "../DropZone";
import { TextWorkspace } from "../TextWorkspace";
import { formatBytes, estimatePackedBytes, utf8Bytes } from "../format";
import { useI18n } from "../i18n";
import {
  IconAudio,
  IconChevronLeft,
  IconChevronRight,
  IconCopy,
  IconExtract,
  IconFile,
  IconFolder,
  IconImage,
  IconLock,
  IconPdf,
  IconSparkle,
  IconText,
  IconCheckCircle,
  IconSpinner,
} from "../icons";
import { CapacityBar, ErrorBanner, PasswordField } from "../ui";

export type ContentSel =
  | { kind: "none" }
  | { kind: "file"; path: string; name: string; sizeBytes: number | null }
  | { kind: "text"; text: string }
  | { kind: "example" };

interface SuccessInfo {
  outputPath: string;
  payloadBytes: number;
  payloadKind: "text" | "file";
  kdf: string;
  demo?: boolean;
}

export function HideScreen({ onGoExtract }: { onGoExtract: (path: string) => void }) {
  const { t } = useI18n();
  const units = { b: t("unit.b"), kb: t("unit.kb"), mb: t("unit.mb"), gb: t("unit.gb") };
  const fmt = (bytes: number) => formatBytes(bytes, units);

  const [content, setContent] = useState<ContentSel>({ kind: "none" });
  const [textExpanded, setTextExpanded] = useState(false);
  const [exampleSecretBytes, setExampleSecretBytes] = useState<number>(809);
  const [exampleSecretName, setExampleSecretName] = useState("example-payload.pdf");

  const [media, setMedia] = useState<"image" | "audio">("image");
  const [carrierMode, setCarrierMode] = useState<"auto" | "custom">("auto");
  const [carrier, setCarrier] = useState<ProbeInfo | null>(null);
  const [carrierError, setCarrierError] = useState<BridgeError | null>(null);
  const [carrierBusy, setCarrierBusy] = useState(false);
  const [suitableCount, setSuitableCount] = useState(0);
  const [builtinOverflow, setBuiltinOverflow] = useState<{
    requiredBytes: number;
    maxCapacityBytes: number;
  } | null>(null);
  const carrierReqSeq = useRef(0);

  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [busySec, setBusySec] = useState(0);
  const [done, setDone] = useState<SuccessInfo | null>(null);
  const [error, setError] = useState<BridgeError | null>(null);
  const [gateMsg, setGateMsg] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const phase: "choose" | "details" | "done" = done ? "done" : content.kind === "none" ? "choose" : "details";
  const isExample = content.kind === "example";

  useEffect(() => {
    if (!busy) {
      setBusySec(0);
      return undefined;
    }
    const started = Date.now();
    const id = window.setInterval(() => setBusySec(Math.floor((Date.now() - started) / 1000)), 250);
    return () => window.clearInterval(id);
  }, [busy]);

  useEffect(() => {
    if (content.kind !== "example") return;
    let cancelled = false;
    bridge
      .exampleInfo()
      .then((res) => {
        if (!cancelled && res.ok) {
          setExampleSecretBytes(res.secret.sizeBytes);
          setExampleSecretName(res.secret.name);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [content.kind]);

  const userBytes = useMemo(() => {
    switch (content.kind) {
      case "file":
        return typeof content.sizeBytes === "number" && Number.isSafeInteger(content.sizeBytes) && content.sizeBytes >= 0
          ? content.sizeBytes : null;
      case "text":
        return utf8Bytes(content.text);
      case "example":
        return exampleSecretBytes;
      default:
        return 0;
    }
  }, [content, exampleSecretBytes]);

  const nameBytes = useMemo(() => {
    switch (content.kind) {
      case "file":
        return utf8Bytes(content.name);
      case "example":
        return utf8Bytes(exampleSecretName);
      default:
        return 8;
    }
  }, [content, exampleSecretName]);

  const needBytes = userBytes === null ? null : estimatePackedBytes(userBytes, nameBytes);
  // The bundled example carrier: 300×300 RGB → 33 684 bytes of capacity.
  const exampleCapacity = 33_684;
  const capacityBytes = isExample ? exampleCapacity : carrier?.capacityBytes ?? 0;
  const fits = capacityBytes > 0 && needBytes !== null && needBytes <= capacityBytes;

  useEffect(() => {
    if (content.kind !== "file" && content.kind !== "text") {
      carrierReqSeq.current += 1;
      setBuiltinOverflow(null);
      setSuitableCount(0);
      return;
    }
    if (media !== "image" || carrierMode !== "auto") {
      setBuiltinOverflow(null);
      return;
    }
    if (content.kind === "file" && userBytes === null) {
      return;
    }
    let cancelled = false;
    const seq = ++carrierReqSeq.current;
    const req =
      content.kind === "file"
        ? { secretPath: content.path, requiredBytes: needBytes ?? undefined }
        : { text: content.text };
    const delayMs = content.kind === "text" ? 250 : 0;

    const run = () => {
      if (cancelled || carrierReqSeq.current !== seq) return;
      setCarrierBusy(true);
      bridge
        .selectBuiltinCarrier(req)
        .then((res) => {
          if (cancelled || carrierReqSeq.current !== seq) return;
          setCarrierBusy(false);
          if (!res.ok) return;
          setCarrierError(null);
          setSuitableCount(res.suitableCount);
          if (res.fits && res.carrier) {
            setCarrier(res.carrier);
            setBuiltinOverflow(null);
          } else {
            setCarrier(null);
            setBuiltinOverflow({
              requiredBytes: res.requiredBytes,
              maxCapacityBytes: res.maxCapacityBytes,
            });
          }
        })
        .catch(() => {
          if (!cancelled && carrierReqSeq.current === seq) setCarrierBusy(false);
        });
    };

    if (delayMs > 0) {
      const timerId = window.setTimeout(run, delayMs);
      return () => {
        cancelled = true;
        window.clearTimeout(timerId);
      };
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [content, media, carrierMode, userBytes, needBytes]);

  async function selectAnotherBuiltin() {
    if (carrierBusy || (content.kind !== "file" && content.kind !== "text")) return;
    const seq = ++carrierReqSeq.current;
    setCarrierBusy(true);
    setCarrierError(null);
    const req =
      content.kind === "file"
        ? {
            secretPath: content.path,
            requiredBytes: needBytes ?? undefined,
            excludeId: carrier?.builtinId,
          }
        : { text: content.text, excludeId: carrier?.builtinId };
    const res = await bridge.selectBuiltinCarrier(req);
    if (carrierReqSeq.current !== seq) return;
    setCarrierBusy(false);
    if (!res.ok) {
      setCarrierError(res.error);
      return;
    }
    setSuitableCount(res.suitableCount);
    if (res.fits && res.carrier) {
      setCarrier(res.carrier);
      setBuiltinOverflow(null);
    } else {
      setCarrier(null);
      setBuiltinOverflow({
        requiredBytes: res.requiredBytes,
        maxCapacityBytes: res.maxCapacityBytes,
      });
    }
  }

  async function probeCarrier(file: FilePick) {
    const seq = ++carrierReqSeq.current;
    setCarrierBusy(true);
    setCarrierError(null);
    const res = await bridge.probe({
      path: file.path,
      displayName: file.name,
      displaySize: file.sizeBytes ?? undefined,
    });
    if (carrierReqSeq.current !== seq) return;
    setCarrierBusy(false);
    if (res.ok) {
      setCarrierMode("custom");
      setMedia(res.kind);
      setBuiltinOverflow(null);
      setCarrier(res);
    } else {
      setCarrier(null);
      setCarrierError(res.error);
    }
  }

  async function pickSecretFile() {
    setError(null);
    const picked = await bridge.pickFile("secret", "any");
    if (picked) setContent({ kind: "file", ...picked });
  }

  async function pickCarrierFile(filterMedia: "image" | "audio" | "any" = media) {
    setError(null);
    const picked = await bridge.pickFile("carrier", filterMedia);
    if (picked) await probeCarrier(picked);
  }

  function resetAll(keepCarrier: boolean) {
    setContent({ kind: "none" });
    setTextExpanded(false);
    if (!keepCarrier) {
      setCarrier(null);
      setCarrierMode("auto");
    }
    setBuiltinOverflow(null);
    setSuitableCount(0);
    setPassword("");
    setDone(null);
    setError(null);
    setGateMsg(null);
    setCopied(false);
  }

  async function onSubmit(event?: FormEvent) {
    event?.preventDefault();
    if (busy) return;
    setError(null);
    setGateMsg(null);
    if (content.kind === "none") {
      setGateMsg(t("hide.need_content"));
      return;
    }
    if (!isExample && !carrier) {
      setGateMsg(t("hide.carrier_hint"));
      return;
    }
    if (password.length < 12) {
      setGateMsg(t("hide.need_password"));
      return;
    }
    if (needBytes === null) {
      setGateMsg(t("hide.size_unavailable"));
      return;
    }
    if (!fits) {
      if (isExample || carrier) {
        setError({ code: "capacity", message: "too much data", detail: null });
      } else {
        setGateMsg(t("hide.carrier_hint"));
      }
      return;
    }

    // Suggest a destination; the system dialog decides, we never overwrite silently.
    const ext = isExample || media === "image" || carrier?.kind === "image" ? "png" : carrier?.name.endsWith(".flac") ? "flac" : "wav";
    const suggested =
      content.kind === "file"
        ? `${content.name.replace(/\.[^.]+$/, "")}-phantom.${ext}`
        : content.kind === "example"
          ? `phantom-example.${ext}`
          : `phantom-secret.${ext}`;
    const saveKind: "image" | "audio" = ext === "png" ? "image" : "audio";
    let outputPath: string | null = null;
    try {
      outputPath = await bridge.saveFile(suggested, saveKind);
    } catch (err) {
      outputPath = null;
      setError({
        code: "io",
        message: "save dialog failed",
        detail: err instanceof Error ? err.message : String(err),
      });
      return;
    }
    if (!outputPath) return; // user cancelled — nothing was written

    setBusy(true);
    const isText = content.kind === "text";
    const useBuiltin = Boolean(carrier?.builtin && carrier.builtinId);
    const res = isExample
      ? await bridge.hide({ carrierPath: "", outputPath, password, example: true })
      : await bridge.hide({
          ...(useBuiltin ? { builtinCarrierId: carrier!.builtinId } : { carrierPath: carrier!.path }),
          outputPath,
          password,
          secretPath: content.kind === "file" ? content.path : undefined,
          text: isText ? (content as { kind: "text"; text: string }).text : undefined,
        });
    setBusy(false);
    if (res.ok) {
      setPassword(""); // wipe the secret from UI state as soon as the core has it
      setDone({
        outputPath: res.outputPath,
        payloadBytes: res.userBytes,
        payloadKind: res.payloadKind,
        kdf: res.kdf,
        demo: res.demo,
      });
    } else {
      setError(res.error);
    }
  }

  const canSubmit =
    !busy &&
    content.kind !== "none" &&
    (isExample || carrier !== null) &&
    password.length >= 12 &&
    fits;

  const disabledHint = busy
    ? null
    : content.kind === "none"
      ? t("hide.need_content")
      : !isExample && !carrier
        ? null // the carrier hint block above the button already says it
        : password.length < 12
          ? t("hide.need_password")
          : null;

  // ------------------------------------------------------------- screens

  if (phase === "choose") {
    return (
      <div className="screen">
        <h1 className="screen-title">{t("hide.q")}</h1>
        <p className="screen-sub">{t("hide.q_sub")}</p>

        <div className="choice-list" role="list">
          <button type="button" className="choice-card" data-testid="hide-pick-file" onClick={pickSecretFile}>
            <span className="choice-icon">
              <IconFile size={24} />
            </span>
            <span className="choice-text">
              <span className="choice-title">{t("hide.card_file")}</span>
              <span className="choice-desc" style={{ display: "block" }}>
                {t("hide.card_file_d")}
              </span>
            </span>
            <IconChevronRight size={18} className="choice-chevron" />
          </button>

          <button
            type="button"
            className="choice-card"
            onClick={() => setContent({ kind: "text", text: "" })}
          >
            <span className="choice-icon">
              <IconText size={24} />
            </span>
            <span className="choice-text">
              <span className="choice-title">{t("hide.card_text")}</span>
              <span className="choice-desc" style={{ display: "block" }}>
                {t("hide.card_text_d")}
              </span>
            </span>
            <IconChevronRight size={18} className="choice-chevron" />
          </button>

          <button
            type="button"
            className="choice-card"
            onClick={() => setContent({ kind: "example" })}
          >
            <span className="choice-icon">
              <IconSparkle size={24} />
            </span>
            <span className="choice-text">
              <span className="choice-title">{t("hide.card_example")}</span>
              <span className="choice-desc" style={{ display: "block" }}>
                {t("hide.card_example_d")}
              </span>
            </span>
            <IconChevronRight size={18} className="choice-chevron" />
          </button>
        </div>
      </div>
    );
  }

  if (phase === "done" && done) {
    const copiedNow = copied;
    return (
      <div className="screen">
        <div className="card success-panel">
          <div className="success-badge">
            <IconCheckCircle size={38} />
          </div>
          <div className="success-title">{t("hide.success_title")}</div>
          <p className="success-verified">
            <IconCheckCircle size={16} />
            {t("hide.success_verified")}
          </p>
          <div>
            <div className="section-label" style={{ textAlign: "center" }}>
              {t("hide.success_output")}
            </div>
            <div className="success-path">
              <span className="mono">{done.outputPath}</span>
              <button
                type="button"
                className="btn-icon"
                style={{ width: 30, height: 30 }}
                aria-label={t("common.copy")}
                onClick={() => {
                  try {
                    void navigator.clipboard?.writeText(done.outputPath);
                  } catch {
                    /* clipboard unavailable — the path stays selectable */
                  }
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1500);
                }}
              >
                {copiedNow ? <span aria-hidden>✓</span> : <IconCopy size={15} />}
              </button>
            </div>
          </div>
          <div className="success-meta">
            {t("hide.success_meta", { bytes: fmt(done.payloadBytes), kdf: done.kdf })}
          </div>
          <p className="success-note">{t("hide.success_note")}</p>
          <div className="success-actions">
            <button type="button" className="btn btn-ghost" onClick={() => void bridge.revealPath(done.outputPath).catch(() => undefined)}>
              <IconFolder size={17} />
              {t("hide.open_folder")}
            </button>
            <button type="button" className="btn btn-subtle" data-testid="hide-open-extract" onClick={() => onGoExtract(done.outputPath)}>
              <IconExtract size={17} />
              {t("hide.open_extract")}
            </button>
            <button type="button" className="btn btn-primary" onClick={() => resetAll(true)}>
              {t("hide.hide_another")}
            </button>
          </div>
          {done.demo || isDemoMode() ? <p className="hint">{t("demo.banner")}</p> : null}
        </div>
      </div>
    );
  }

  // ------------------------------------------------------------ details

  const contentCard =
    content.kind === "file" ? (
      <>
        <div className="file-glyph">
          <IconFile size={28} />
        </div>
        <div className="file-name" title={content.name}>
          {content.name}
        </div>
        <div className="file-meta">
          {userBytes === null ? <span role="alert">{t("hide.size_unavailable")}</span> : fmt(userBytes)}
        </div>
        <div className="row" style={{ marginTop: "auto" }}>
          <button type="button" className="btn btn-subtle" onClick={pickSecretFile}>
            {t("hide.change_selection")}
          </button>
        </div>
      </>
    ) : content.kind === "example" ? (
      <>
        <div className="file-glyph">
          <IconPdf size={28} />
        </div>
        <div className="file-name">{exampleSecretName}</div>
        <div className="file-meta">
          {fmt(exampleSecretBytes)} · {t("hide.example_note")}
        </div>
        <div className="row" style={{ marginTop: "auto" }}>
          <button type="button" className="btn btn-subtle" onClick={() => resetAll(true)}>
            {t("hide.change_selection")}
          </button>
        </div>
      </>
    ) : (
      <>
        <label className="section-label" htmlFor="payload-text">
          {t("hide.card_text")}
        </label>
        <textarea
          id="payload-text"
          className="textarea text-editor"
          rows={8}
          value={content.kind === "text" ? content.text : ""}
          placeholder={t("hide.text_placeholder")}
          onChange={(event) => setContent({ kind: "text", text: event.target.value })}
          autoFocus
        />
        <div className="text-counters">
          <span>
            {t("hide.text_count", {
              chars: content.kind === "text" ? content.text.length : 0,
              bytes: fmt(userBytes ?? 0),
            })}
          </span>
          <button type="button" className="btn btn-subtle" onClick={() => setTextExpanded(true)}>
            {t("text.expand")}
          </button>
        </div>
      </>
    );

  return (
    <form
      className="screen"
      onSubmit={onSubmit}
      aria-busy={busy}
      onDragOver={(event) => event.preventDefault()}
    >
      {textExpanded && content.kind === "text" ? <TextWorkspace
        label={t("hide.card_text")} value={content.text}
        onChange={(text) => setContent({ kind: "text", text })} onClose={() => setTextExpanded(false)} /> : null}
      <div className="back-row" style={{ marginBottom: 4 }}>
        <button type="button" className="btn btn-subtle" onClick={() => resetAll(true)}>
          <IconChevronLeft size={15} />
          {t("common.back")}
        </button>
        <span className="step-chip">
          <IconCheckCircle size={13} />
          {t("hide.what")}
        </span>
      </div>
      <h1 className="screen-title" style={{ fontSize: 21 }}>
        {t("hide.q")}
      </h1>

      <div className="panes">
        {/* -------------------------------------------------- what we hide */}
        <section className="card card-pad pane" aria-label={t("hide.what")}>
          <div className="pane-header">
            <h2 className="section-label" style={{ margin: 0 }}>
              {t("hide.what")}
            </h2>
          </div>
          <div className="content-summary">{contentCard}</div>
        </section>

        {/* ------------------------------------------------- where we hide */}
        <section className="card card-pad pane" aria-label={t("hide.where")}>
          <div className="pane-header">
            <h2 className="section-label" style={{ margin: 0 }}>
              {t("hide.where")}
            </h2>
            {carrier && !carrier.builtin ? (
              <button
                type="button"
                className="btn btn-subtle"
                onClick={() => {
                  setCarrier(null);
                  setCarrierMode("custom");
                }}
              >
                {t("hide.carrier_change")}
              </button>
            ) : null}
          </div>

          {isExample ? (
            <div className="content-summary">
              <div className="file-glyph">
                <IconImage size={28} />
              </div>
              <div className="file-name">example_carrier.png</div>
              <div className="file-meta">300×300 · RGB · {fmt(201_798)}</div>
              <p className="hint" style={{ marginTop: "auto" }}>
                {t("hide.example_note")}
              </p>
            </div>
          ) : carrier ? (
            <div className="content-summary">
              {carrier.builtin ? (
                <div className="media-toggle" role="group" aria-label={t("hide.where")} style={{ marginBottom: 8 }}>
                  <button
                    type="button"
                    className="media-option"
                    aria-pressed={media === "image"}
                    onClick={() => {
                      setMedia("image");
                      setCarrierMode("auto");
                    }}
                  >
                    <IconImage size={16} />
                    {t("hide.container_image")}
                  </button>
                  <button
                    type="button"
                    className="media-option"
                    aria-pressed={media === "audio"}
                    onClick={() => {
                      carrierReqSeq.current += 1;
                      setMedia("audio");
                      setCarrierMode("custom");
                      setCarrier(null);
                    }}
                  >
                    <IconAudio size={16} />
                    {t("hide.container_audio")}
                  </button>
                </div>
              ) : null}
              <div className="file-glyph">
                {carrier.kind === "image" ? <IconImage size={28} /> : <IconAudio size={28} />}
              </div>
              <div className="file-name" title={carrier.name}>
                {carrier.name}
              </div>
              <div className="file-meta">
                {fmt(carrier.sizeBytes)} · {carrier.description}
              </div>
              {carrier.builtin ? (
                <>
                  <p className="hint">{t("hide.builtin_auto_note")}</p>
                  <div className="row" style={{ marginTop: "auto", gap: 8, flexWrap: "wrap" }}>
                    <button
                      type="button"
                      className="btn btn-subtle"
                      disabled={carrierBusy || suitableCount <= 1}
                      onClick={() => void selectAnotherBuiltin()}
                    >
                      {t("hide.builtin_other")}
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost"
                      data-testid="hide-choose-own"
                      disabled={carrierBusy}
                      onClick={() => void pickCarrierFile("any")}
                    >
                      {t("hide.builtin_choose_own")}
                    </button>
                  </div>
                </>
              ) : (
                <>
                  {carrier.notices.length > 0 ? (
                    <p className="hint">{carrier.notices.join(" · ")}</p>
                  ) : null}
                  {media === "image" ? (
                    <div className="row" style={{ marginTop: "auto" }}>
                      <button
                        type="button"
                        className="btn btn-subtle"
                        onClick={() => setCarrierMode("auto")}
                      >
                        {t("hide.builtin_back")}
                      </button>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          ) : (
            <>
              <div className="media-toggle" role="group" aria-label={t("hide.where")}>
                <button
                  type="button"
                  className="media-option"
                  aria-pressed={media === "image"}
                  onClick={() => {
                    setMedia("image");
                    setCarrierMode("auto");
                  }}
                >
                  <IconImage size={16} />
                  {t("hide.container_image")}
                </button>
                <button
                  type="button"
                  className="media-option"
                  aria-pressed={media === "audio"}
                  onClick={() => {
                    setMedia("audio");
                    setCarrierMode("custom");
                  }}
                >
                  <IconAudio size={16} />
                  {t("hide.container_audio")}
                </button>
              </div>
              {builtinOverflow ? (
                <div className="banner banner-error" role="alert" style={{ marginBottom: 8 }}>
                  <div className="banner-text">
                    {t("hide.builtin_none_fits", {
                      need: fmt(builtinOverflow.requiredBytes),
                      max: fmt(builtinOverflow.maxCapacityBytes),
                    })}
                  </div>
                </div>
              ) : null}
              <DropZone
                ariaLabel={t("hide.drop_title", {
                  kind: media === "image" ? t("hide.kind_image") : t("hide.kind_audio"),
                })}
                onDrop={(file) => void probeCarrier({ ...file, sizeBytes: file.sizeBytes ?? 0 })}
              >
                <IconImage size={26} strokeWidth={1.4} />
                <div className="dropzone-title">
                  {t("hide.drop_title", {
                    kind: media === "image" ? t("hide.kind_image") : t("hide.kind_audio"),
                  })}
                </div>
                <div className="dropzone-hint">{t("hide.drop_hint")}</div>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void pickCarrierFile(builtinOverflow ? "any" : media)}
                >
                  {carrierBusy ? <IconSpinner size={16} className="spin" /> : null}
                  {builtinOverflow ? t("hide.builtin_choose_own") : t("common.choose_file")}
                </button>
              </DropZone>
              {carrierError ? <ErrorBanner error={carrierError} /> : null}
            </>
          )}
        </section>
      </div>

      {(isExample || carrier) && needBytes !== null ? (
        <CapacityBar needBytes={needBytes} capacityBytes={capacityBytes} />
      ) : builtinOverflow ? (
        <CapacityBar
          needBytes={builtinOverflow.requiredBytes}
          capacityBytes={builtinOverflow.maxCapacityBytes}
        />
      ) : !isExample && !carrier ? (
        <p className="carrier-hint" role="status">
          <IconImage size={16} aria-hidden />
          {t("hide.carrier_hint")}
        </p>
      ) : null}

      <div className="card card-compact" style={{ marginTop: 10 }}>
        <PasswordField
          id="hide-password"
          label={t("hide.password")}
          value={password}
          onChange={setPassword}
          onEnter={() => void onSubmit()}
        />
      </div>

      {error ? <ErrorBanner error={error} /> : null}
      {gateMsg ? (
        <p className="hint" role="status" style={{ color: "var(--amber)", marginTop: 10 }}>
          {gateMsg}
        </p>
      ) : null}

      <div className="action-row">
        <button type="submit" className="btn btn-primary btn-big" data-testid="hide-submit" disabled={!canSubmit}>
          {busy ? <IconSpinner size={17} className="spin" /> : <IconLock size={17} />}
          {busy ? t("hide.busy", { sec: busySec }) : t("hide.submit")}
        </button>
        {disabledHint ? <p className="action-hint">{disabledHint}</p> : null}
      </div>
    </form>
  );
}
