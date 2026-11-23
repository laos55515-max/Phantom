import { useState, type FormEvent } from "react";
import { bridge, type BridgeError, type FilePick } from "../bridge";
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
  | { kind: "text"; text: string; audit: AuditInfo }
  | { kind: "file"; suggestedName: string; audit: AuditInfo };

export function ExtractScreen() {
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

  const reset = () => {
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
      setCarrier(picked);
      setError(null);
      setOutcome(null);
      setSavedPath(null);
    }
  }

  async function runExtract(event?: FormEvent) {
    event?.preventDefault();
    if (!carrier || password.length < 12 || phase === "busy") return;
    setPhase("busy");
    setError(null);
    setSavedPath(null);
    const res = await bridge.extract({
      carrierPath: carrier.path,
      password,
      mode: "info",
    });
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
      setOutcome({ kind: "text", text: res.text ?? "", audit });
    } else {
      setOutcome({ kind: "file", suggestedName: res.suggestedName, audit });
    }
    // The password stays in memory only until the terminal action
    // (save-as completes or the screen is reset): the save step reuses it for
    // the second, byte-identical decrypt.  It is never logged or persisted.
    setPhase("done");
  }

  async function saveFilePayload() {
    if (outcome?.kind !== "file" || !carrier) return;
    setBusy(true);
    setError(null);
    const outputPath = await bridge.saveFile(outcome.suggestedName, "file");
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
    setBusy(true);
    setError(null);
    const outputPath = await bridge.saveFile("phantom-extracted.txt", "text");
    if (outputPath) {
      const textBlob = new Blob([outcome.text], { type: "text/plain;charset=utf-8" });
      // Browser demo cannot write files; in Tauri we go through the bridge so
      // the bytes never round-trip through the webview more than needed.
      if ("__TAURI_INTERNALS__" in window) {
        const res = await bridge.extract({
          carrierPath: carrier.path,
          password,
          mode: "save_file",
          outputPath,
        });
        if (res.ok) {
          setSavedPath(outputPath);
          setPassword("");
        } else {
          setError(res.error);
        }
      } else {
        const url = URL.createObjectURL(textBlob);
        const a = document.createElement("a");
        a.href = url;
        a.download = outputPath.split(/[\\/]/).pop() ?? "phantom-extracted.txt";
        a.click();
        URL.revokeObjectURL(url);
        setSavedPath(outputPath);
      }
    }
    setBusy(false);
  }

  const digestShort = outcome ? `${outcome.audit.sha256.slice(0, 16)}…` : "";

  return (
    <form className="screen" onSubmit={runExtract} aria-busy={phase === "busy" || busy}>
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
            <div className="file-meta">{fmt(carrier.sizeBytes)}</div>
          </div>
        ) : (
          <DropZone ariaLabel={t("extract.drop_title")} onDrop={(file) => setCarrier({ ...file, sizeBytes: file.sizeBytes ?? 0 })}>
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
            onChange={setPassword}
            onEnter={() => void runExtract()}
            autoFocus
          />
        </div>
      ) : null}

      {error ? <ErrorBanner error={error} title={t("extract.failure_title")} /> : null}

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
              <textarea className="textarea mono" style={{ minHeight: 120 }} readOnly value={outcome.text} aria-label={t("extract.text_label")} />
              <div className="row">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => {
                    try {
                      void navigator.clipboard?.writeText(outcome.text);
                    } catch {
                      /* keep readonly textarea as the copy surface */
                    }
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 1500);
                  }}
                >
                  {copied ? <IconCheckCircle size={16} /> : <IconCopy size={16} />}
                  {copied ? t("common.copied") : t("common.copy")}
                </button>
                <button type="button" className="btn btn-ghost" onClick={saveTextPayload} disabled={busy}>
                  {busy ? <IconSpinner size={15} className="spin" /> : null}
                  {t("extract.save_as")}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="content-summary" style={{ minHeight: 0 }}>
                <div className="file-glyph">
                  <IconFile size={28} />
                </div>
                <div className="file-name">{outcome.suggestedName}</div>
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

          <div className="success-meta">
            {t("extract.audit", {
              version: String(outcome.audit.containerVersion),
              kdf: outcome.audit.kdf,
            })}
            {" · "}
            {outcome.audit.sha256.slice(0, 12)}…
            <span className="sr-only">{digestShort}</span>
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
