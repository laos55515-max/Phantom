import { useId, useState } from "react";
import { useI18n } from "./i18n";
import type { BridgeError } from "./bridge";
import { generatePassword, passwordStrength } from "./format";
import {
  IconAlert,
  IconDice,
  IconEye,
  IconEyeOff,
} from "./icons";

export function ErrorBanner({
  error,
  title,
}: {
  error: BridgeError;
  title?: string;
}) {
  const { t } = useI18n();
  const key = `err.${error.code}`;
  const message = t(key);
  const text = message === key ? t("err.internal") : message;
  return (
    <div className="banner banner-error" role="alert">
      <IconAlert size={17} />
      <div className="grow">
        {title ? <strong>{title}</strong> : null}
        <div className="banner-text">{text}</div>
        {error.detail ? (
          <details className="banner-details">
            <summary>{t("common.details")}</summary>
            <pre>
              {error.code}: {error.detail}
            </pre>
          </details>
        ) : null}
      </div>
    </div>
  );
}

export function PasswordField({
  id,
  label,
  value,
  onChange,
  onEnter,
  autoFocus,
}: {
  id?: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  onEnter?: () => void;
  autoFocus?: boolean;
}) {
  const { t } = useI18n();
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const [visible, setVisible] = useState(false);
  const strength = passwordStrength(value);
  const segments = strength === "weak" ? (value.length === 0 ? 0 : 1) : strength === "fair" ? 2 : 3;
  const strengthKey = `hide.strength_${strength}`;

  return (
    <div>
      <label className="section-label" htmlFor={inputId}>
        {label}
      </label>
      <div className="password-row">
        <div className="input-shell">
          <input
            id={inputId}
            className="input mono"
            style={{ paddingRight: 8 }}
            type={visible ? "text" : "password"}
            value={value}
            autoFocus={autoFocus}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && onEnter) {
                event.preventDefault();
                onEnter();
              }
            }}
            aria-describedby={`${inputId}-hint ${inputId}-strength`}
            aria-invalid={value.length > 0 && value.length < 12}
          />
        </div>
        <button
          type="button"
          className="btn-icon"
          onClick={() => setVisible((current) => !current)}
          aria-label={visible ? t("hide.password_hide") : t("hide.password_show")}
          aria-pressed={visible}
        >
          {visible ? <IconEyeOff size={18} /> : <IconEye size={18} />}
        </button>
        <button
          type="button"
          className="btn-icon"
          onClick={() => onChange(generatePassword(20))}
          aria-label={t("hide.password_generate")}
          title={t("hide.password_generate")}
        >
          <IconDice size={18} />
        </button>
      </div>
      <div className="strength" id={`${inputId}-strength`}>
        <div className="strength-segments" aria-hidden="true">
          {[1, 2, 3].map((index) => (
            <span
              key={index}
              className={`strength-seg${index <= segments ? ` on-${segments}` : ""}`}
            />
          ))}
        </div>
        <span className="strength-text">
          {t("hide.strength_label", { level: t(strengthKey) })}
        </span>
      </div>
      <p className="hint" id={`${inputId}-hint`}>
        {t("hide.password_hint")}
      </p>
    </div>
  );
}

export function CapacityBar({
  needBytes,
  capacityBytes,
}: {
  needBytes: number;
  capacityBytes: number;
}) {
  const { t } = useI18n();
  const pct = capacityBytes > 0 ? Math.min(100, (needBytes / capacityBytes) * 100) : 100;
  const over = needBytes > capacityBytes;
  const fillClass = over ? "capacity-fill over" : pct > 80 ? "capacity-fill warn" : "capacity-fill";
  return (
    <div className="capacity">
      <div
        className="capacity-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct)}
        aria-label={t("hide.capacity_aria", { pct: Math.round(pct) })}
      >
        <div className={fillClass} style={{ width: `${pct}%` }} />
      </div>
      <div className="capacity-labels">
        <span>{t("hide.capacity_need", { used: formatCapacity(needBytes, t) })}</span>
        <span>{t("hide.capacity_has", { cap: formatCapacity(capacityBytes, t) })}</span>
      </div>
      {over ? (
        <div className="banner banner-error" role="alert" style={{ marginTop: 8 }}>
          <IconAlert size={16} />
          <div className="banner-text">
            {t("hide.capacity_exceeds", {
              used: formatCapacity(needBytes, t),
              cap: formatCapacity(capacityBytes, t),
            })}
          </div>
        </div>
      ) : (
        <p className="capacity-note">{t("hide.capacity_note")}</p>
      )}
    </div>
  );
}

function formatCapacity(bytes: number, t: (key: string) => string): string {
  if (bytes < 1024) return `${bytes} ${t("unit.b")}`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} ${t("unit.kb")}`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} ${t("unit.mb")}`;
}
