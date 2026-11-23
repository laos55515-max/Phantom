import { useEffect, useRef, useState } from "react";
import { bridge } from "../bridge";
import { LANGUAGE_NAMES, SUPPORTED_LANGUAGES, useI18n, type Language } from "../i18n";
import { IconCheck, IconCheckCircle, IconMonitor, IconMoon, IconSun } from "../icons";
import type { Theme } from "../settings";

export function SettingsScreen() {
  const { t, language, setLanguage, theme, setTheme } = useI18n();
  const [savedAt, setSavedAt] = useState(0);
  const [coreVersion, setCoreVersion] = useState("2.1.0");
  const first = useRef(true);

  // Show the "draft saved" pill whenever language or theme changes.
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setSavedAt(Date.now());
  }, [language, theme]);

  useEffect(() => {
    let cancelled = false;
    bridge
      .appInfo()
      .then((info) => {
        if (!cancelled && info?.core) setCoreVersion(info.core);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const themeOptions: Array<{ id: Theme; label: string; icon: JSX.Element }> = [
    { id: "light", label: t("settings.theme_light"), icon: <IconSun size={15} /> },
    { id: "dark", label: t("settings.theme_dark"), icon: <IconMoon size={15} /> },
    { id: "system", label: t("settings.theme_system"), icon: <IconMonitor size={15} /> },
  ];

  return (
    <div className="screen">
      <h1 className="screen-title">{t("settings.title")}</h1>
      <p className="screen-sub">{t("settings.saved")}</p>

      <section className="card card-pad settings-group" aria-labelledby="settings-appearance">
        <div className="settings-head">
          <h2 className="section-label" id="settings-appearance" style={{ margin: 0 }}>
            {t("settings.appearance")}
          </h2>
          {savedAt > 0 ? (
            <span className="draft-pill" key={savedAt} role="status">
              <IconCheckCircle size={13} />
              {t("settings.draft_saved")}
            </span>
          ) : null}
        </div>
        <div className="section-label" style={{ color: "var(--secondary)" }}>
          {t("settings.theme")}
        </div>
        <div className="segmented" role="group" aria-label={t("settings.theme")}>
          {themeOptions.map((option) => (
            <button
              key={option.id}
              type="button"
              className="segment"
              aria-pressed={theme === option.id}
              onClick={() => setTheme(option.id)}
            >
              {option.icon}
              {option.label}
            </button>
          ))}
        </div>
      </section>

      <section className="card card-pad settings-group" aria-labelledby="settings-language">
        <div className="settings-head">
          <h2 className="section-label" id="settings-language" style={{ margin: 0 }}>
            {t("settings.language")}
          </h2>
        </div>
        <div className="language-list" role="radiogroup" aria-label={t("settings.language")}>
          {SUPPORTED_LANGUAGES.map((code) => {
            const active = language === code;
            return (
              <button
                key={code}
                type="button"
                role="radio"
                aria-checked={active}
                className="language-option"
                onClick={() => setLanguage(code as Language)}
              >
                <span className="radio" aria-hidden="true">
                  <IconCheck size={12} strokeWidth={3} />
                </span>
                <span className="language-name">{LANGUAGE_NAMES[code]}</span>
                <span className="language-code">{code.toUpperCase()}</span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="card card-pad settings-group" aria-labelledby="settings-about">
        <h2 className="section-label" id="settings-about" style={{ margin: "0 0 10px" }}>
          {t("settings.about")}
        </h2>
        <dl className="about-table">
          <dt>PHANTOM</dt>
          <dd>{t("settings.version", { core: coreVersion })}</dd>
          <dt>{t("settings.license")}</dt>
          <dd>MIT</dd>
        </dl>
        <div className="section-label" style={{ margin: "14px 0 8px", color: "var(--secondary)" }}>
          {t("settings.notes")}
        </div>
        <ul className="notes-list">
          <li>{t("settings.notes_1")}</li>
          <li>{t("settings.notes_2")}</li>
          <li>{t("settings.notes_3")}</li>
        </ul>
        <p className="hint" style={{ marginTop: 12 }}>
          {t("settings.stored")}
        </p>
      </section>
    </div>
  );
}
