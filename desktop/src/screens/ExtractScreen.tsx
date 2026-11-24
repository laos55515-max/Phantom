import { useEffect, useRef, useState, type FormEvent } from "react";
import { bridge, isDemoMode, type BridgeError, type FilePick, type ImagePreview } from "../bridge";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { TextWorkspace } from "../TextWorkspace";
import { ImageWorkspace } from "../ImageWorkspace";
import { formatBytes } from "../format";
import { DropZone } from "../DropZone";
import { useI18n } from "../i18n";
import {
  IconCheckCircle,
  IconCopy,
  IconFile,
  IconFolder,
  IconImage,
  IconAudio,
  IconSpinner,
  IconShield,
} from "../icons";
import { ErrorBanner, PasswordField } from "../ui";

type ExtractPhase = "idle" | "busy" | "done";

interface AuditInfo {
  containerVersion: number;
  kdf: string;
  sha256: string;
  integrityOk: boolean;
  originalSize: number;
}

type ExtractOutcome =
  | { kind: "text"; text: string; textOmitted: boolean; audit: AuditInfo }
  | { kind: "file"; suggestedName: string; imagePreview?: ImagePreview; audit: AuditInfo };

export function ExtractScreen({ handoff = null }: { handoff?: { path: string } | null }) {
  const { t } = useI18n();
  const units = { b: t("unit.b"), kb: t("unit.kb"), mb: t("unit.mb"), gb: t("unit.gb") };
  const fmt = (bytes: number) => formatBytes(bytes, units);

  const [carrier, setCarrier] = useState<FilePick | null>(null);
  const [password, setPassword] = useState("");
  const [phase, setPhase] = useState<ExtractPhase>("idle");
  const [outcome, setOutcome] = useState<ExtractOutcome | null>(null);
  const [savedPath, setSavedPath] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<BridgeError | null>(null);
  const [copied, setCopied] = useState(false);
  const [textExpanded, setTextExpanded] = useState(false);
  const [imageExpanded, setImageExpanded] = useState(false);
  const [verified, setVerified] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    if (!handoff) return;
    generation.current += 1;
    const segments = handoff.path.split(/[\\/]/);
    setCarrier({ path: handoff.path, name: segments[segments.length - 1] || handoff.path, sizeBytes: null });
    setPassword("");
    setPhase("idle");
    setOutcome(null);
    setSavedPath(null);
    setError(null);
    setBusy(false);
    setVerified(false);
    setTextExpanded(false);
    setImageExpanded(false);
  }, [handoff]);

  const reset = () => {
    generation.current += 1;
    setVerified(false);
    setBusy(false);
    setTextExpanded(false);
    setImageExpanded(false);
    setCarrier(null);
    setPassword("");
    setOutcome(null);
    setSavedPath(null);
    setError(null);
    setPhase("idle");
  };

  async function pickCarrier() {
    const picked = await bridge.pickFile("carrier", "any");
    if (picked) {
      generation.current += 1;
      setVerified(false);
      setCarrier(picked);
      setError(null);
      setOutcome(null);
      setSavedPath(null);
    }
  }

  async function runExtract(event?: FormEvent) {
    event?.preventDefault();
    if (!carrier || password.length < 12 || phase === "busy") return;
    const current = generation.current;
    setPhase("busy");
    setVerified(false);
    setError(null);
    setSavedPath(null);
    const res = await bridge.extract({
      carrierPath: carrier.path,
      password,
      mode: "info",
    });
    if (current !== generation.current) return;
    if (!res.ok) {
      setPhase("idle");
      setError(res.error);
      return;
    }
    const audit: AuditInfo = {
      containerVersion: res.containerVersion,
      kdf: res.kdf,
      sha256: res.sha256,
      integrityOk: res.integrityOk,
      originalSize: res.originalSize,
    };
    if (res.kind === "text") {
      setOutcome({ kind: "text", text: res.text ?? "", textOmitted: res.textOmitted === true, audit });
    } else {
      setOutcome({ kind: "file", suggestedName: res.suggestedName, imagePreview: res.imagePreview, audit });
    }
    // The password stays in memory only until the terminal action
    // (save-as completes or the screen is reset): the save step reuses it for
    // the second, byte-identical decrypt.  It is never logged or persisted.
    setPhase("done");
  }

  async function verifyContainer() {
    if (!carrier || password.length < 12 || phase === "busy" || busy) return;
    const current = generation.current;
    setPhase("busy");
    setVerified(false);
    setError(null);
    setOutcome(null);
    setSavedPath(null);
    try {
      const res = await bridge.verifyContainer({ carrierPath: carrier.path, password });
      if (current !== generation.current) return;
      setPhase("idle");
      if (res.ok && res.verified) {
        setVerified(true);
        setPassword("");
      } else if (!res.ok) {
        setError(res.error);
      }
    } catch {
      if (current !== generation.current) return;
      setPhase("idle");
      setError({ code: "internal", message: "verification failed", detail: null });
    }
  }

  async function saveFilePayload() {
    if (outcome?.kind !== "file" || !carrier) return;
    const current = generation.current;
    setBusy(true);
    setError(null);
    const outputPath = await bridge.saveFile(outcome.suggestedName, "file");
    if (current !== generation.current) return;
    if (!outputPath) {
      setBusy(false);
      return; // user cancelled — nothing chosen, nothing written
    }
    const res = await bridge.extract({
      carrierPath: carrier.path,
      password,
      mode: "save_file",
      outputPath,
    });
    if (current !== generation.current) return;
    setBusy(false);
    if (res.ok) {
      setSavedPath(outputPath);
      setPassword(""); // the job is done — drop the secret from UI state
    } else {
      setError(res.error);
    }
  }

  async function saveTextPayload() {
    if (outcome?.kind !== "text" || !carrier) return;
    const current = generation.current;
    setBusy(true);
    setError(null);
    try {
      const outputPath = await bridge.saveFile("phantom-extracted.txt", "text");
      if (current !== generation.current || !outputPath) return;
      if (!isDemoMode()) {
        // The bridge re-decrypts and writes all bytes, including omitted previews.
        const res = await bridge.extract({ carrierPath: carrier.path, password, mode: "save_file", outputPath });
        if (current !== generation.current) return;
        if (res.ok) setSavedPath(outputPath);
        else setError(res.error);
      } else if (!outcome.textOmitted) {
        const textBlob = new Blob([outcome.text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(textBlob);
        const a = document.createElement("a");
        a.href = url;
        a.download = outputPath.split(/[\\/]/).pop() ?? "phantom-extracted.txt";
        a.click();
        URL.revokeObjectURL(url);
        setSavedPath(outputPath);
      } else {
        setError({ code: "io", message: "Text preview unavailable in demo", detail: null });
      }
    } catch {
      if (current === generation.current) setError({ code: "io", message: "Save failed", detail: null });
    } finally {
      if (current === generation.current) setBusy(false);
    }
  }

  async function copyText(text: string) {
    try {
      if (isDemoMode()) await navigator.clipboard.writeText(text);
      else await writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setError({ code: "io", message: "Clipboard write failed", detail: null });
    }
  }

  const textActions = outcome?.kind === "text" ? (
    <>
      {!outcome.textOmitted ? <button type="button" className="btn btn-ghost" onClick={() => void copyText(outcome.text)}>
        {copied ? <IconCheckCircle size={16} /> : <IconCopy size={16} />}
        {copied ? t("common.copied") : t("common.copy")}
      </button> : null}
      <button type="button" className="btn btn-ghost" onClick={saveTextPayload} disabled={busy}>
        {busy ? <IconSpinner size={15} className="spin" /> : null}{t("extract.save_as")}
      </button>
    </>
  ) : null;

  return (
    <form className="screen" onSubmit={runExtract} aria-busy={phase === "busy" || busy}>
      {textExpanded && outcome?.kind === "text" && !outcome.textOmitted ? <TextWorkspace
        label={t("extract.text_label")} value={outcome.text} onClose={() => setTextExpanded(false)}
        actions={<>{textActions}{error ? <ErrorBanner error={error} /> : null}{savedPath ? <span className="file-meta">{t("extract.saved_as")} {savedPath}</span> : null}</>} /> : null}
      {imageExpanded && outcome?.kind === "file" && outcome.imagePreview?.status === "available" ? <ImageWorkspace
        name={outcome.suggestedName} src={`data:image/jpeg;base64,${outcome.imagePreview.data}`}
        onClose={() => setImageExpanded(false)} onError={() => {
          setImageExpanded(false);
          setOutcome({ ...outcome, imagePreview: { type: outcome.imagePreview!.type, status: "unavailable", reason: "decode" } });
        }} /> : null}
      <h1 className="screen-title">{t("extract.title")}</h1>
      <p className="screen-sub">{t("extract.sub")}</p>

      <div className="card card-pad">
        <div className="pane-header">
          <h2 className="section-label" style={{ margin: 0 }}>
            {t("hide.where")}
          </h2>
          {carrier ? (
            <button type="button" className="btn btn-subtle" onClick={reset}>
              {t("hide.carrier_change")}
            </button>
          ) : null}
        </div>
        {carrier ? (
          <div className="content-summary" style={{ minHeight: 96 }}>
            <div className="file-glyph">
              {/\.(wav|flac)$/i.test(carrier.name) ? (
                <IconAudio size={28} />
              ) : (
                <IconImage size={28} />
              )}
            </div>
            <div className="file-name" title={carrier.name}>
              {carrier.name}
            </div>
            <div className="file-meta">{carrier.sizeBytes === null ? t("hide.size_unknown") : fmt(carrier.sizeBytes)}</div>
          </div>
        ) : (
          <DropZone ariaLabel={t("extract.drop_title")} onDrop={(file) => { generation.current += 1; setVerified(false); setError(null); setCarrier(file); }}>
            <IconImage size={26} strokeWidth={1.4} />
            <div className="dropzone-title">{t("extract.drop_title")}</div>
            <div className="dropzone-hint">{t("extract.drop_hint")}</div>
            <button type="button" className="btn btn-ghost" onClick={pickCarrier}>
              {t("common.choose_file")}
            </button>
          </DropZone>
        )}
      </div>

      {carrier && phase !== "done" ? (
        <div className="card card-pad" style={{ marginTop: 12 }}>
          <PasswordField
            id="extract-password"
            label={t("hide.password")}
            value={password}
            onChange={(value) => { if (phase !== "busy") { setPassword(value); setVerified(false); } }}
            onEnter={() => void runExtract()}
            autoFocus
          />
        </div>
      ) : null}

      {error ? <ErrorBanner error={error} title={t("extract.failure_title")} /> : null}
      {verified ? <p className="success-verified" role="status"><IconCheckCircle size={16} />{t("extract.verified")}</p> : null}

      {carrier && phase !== "done" ? (
        <div className="action-row">
          <button
            type="submit"
            className="btn btn-primary btn-big"
            disabled={!carrier || password.length < 12 || phase === "busy"}
          >
            {phase === "busy" ? <IconSpinner size={17} className="spin" /> : <IconShield size={17} />}
            {phase === "busy" ? t("hide.busy", { sec: 0 }) : t("extract.submit")}
          </button>
          <button type="button" className="btn btn-ghost btn-big" onClick={() => void verifyContainer()}
            disabled={!carrier || password.length < 12 || phase === "busy" || busy}>
            {t("extract.verify")}
          </button>
        </div>
      ) : null}

      {phase === "done" && outcome ? (
        <div className="card card-pad" style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          <div className="row">
            <IconCheckCircle size={20} style={{ color: "var(--green)" }} />
            <strong>{outcome.kind === "text" ? t("extract.text_label") : t("extract.found_file")}</strong>
            <span className="file-meta">· {fmt(outcome.audit.originalSize)}</span>
          </div>

          {outcome.kind === "text" ? (
            <>
              {outcome.textOmitted ? <p role="status">{t("extract.text_omitted")}</p> : <>
                <textarea className="textarea mono" style={{ minHeight: 120, width: "100%" }} readOnly value={outcome.text} aria-label={t("extract.text_label")} />
                <button type="button" className="btn btn-subtle" onClick={() => setTextExpanded(true)}>{t("text.expand")}</button>
              </>}
              <div className="row">{textActions}</div>
              {savedPath ? <div className="file-meta">{t("extract.saved_as")} {savedPath}</div> : null}
            </>
          ) : (
            <>
              <div className="content-summary" style={{ minHeight: 0 }}>
                <div className="file-glyph">
                  <IconFile size={28} />
                </div>
                <div className="file-name">{outcome.suggestedName}</div>
                {outcome.imagePreview ? <div className="file-meta">{outcome.imagePreview.type} · {fmt(outcome.audit.originalSize)}</div> : null}
                {outcome.imagePreview?.status === "available" ? <button type="button" className="btn btn-ghost" onClick={() => setImageExpanded(true)}>{t("extract.show_image")}</button> : null}
                {outcome.imagePreview?.status === "unavailable" ? <p className="hint" role="status">{t(outcome.imagePreview.reason === "size" ? "extract.image_size" : "extract.image_decode")}</p> : null}
                <div className="mono" style={{ color: "var(--secondary)", overflowWrap: "anywhere" }}>
                  {t("extract.digest", { digest: outcome.audit.sha256 })}
                </div>
              </div>
              {savedPath ? (
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <span className="success-verified">
                    <IconCheckCircle size={16} /> {t("extract.saved_as")}
                  </span>
                  <span className="mono" style={{ overflowWrap: "anywhere" }}>
                    {savedPath}
                  </span>
                  <button type="button" className="btn btn-ghost" onClick={() => void bridge.revealPath(savedPath).catch(() => undefined)}>
                    <IconFolder size={16} />
                    {t("hide.open_folder")}
                  </button>
                </div>
              ) : (
                <>
                  <div className="row">
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={saveFilePayload}
                      disabled={busy || password.length < 12}
                    >
                      {busy ? <IconSpinner size={16} className="spin" /> : null}
                      {t("extract.save_as")}
                    </button>
                  </div>
                </>
              )}
            </>
          )}

          <div className="success-meta" title={outcome.audit.sha256}>
            {t("extract.audit", {
              version: String(outcome.audit.containerVersion),
              kdf: outcome.audit.kdf,
            })}
            {" · "}
            {outcome.audit.sha256.slice(0, 12)}…
          </div>
          <div className="row">
            <button type="button" className="btn btn-subtle" onClick={reset}>
              {t("extract.again")}
            </button>
          </div>
        </div>
      ) : null}
    </form>
  );
}
