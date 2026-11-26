import { LANGUAGE_NAMES, SUPPORTED_LANGUAGES, useI18n } from "./i18n";
import { IconLock, IconMoon, IconSun } from "./icons";
import mark from "./phantom-mark.png";

/** First-launch language picker (Українська · English · Русский). */
export function FirstRun() {
  const { t, setLanguage, theme, setTheme } = useI18n();
  const dark =
    theme === "dark" ||
    (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);

  return (
    <div className="firstrun" role="dialog" aria-modal="true" aria-labelledby="firstrun-title">
      <button
        type="button"
        className="btn-icon firstrun-theme"
        onClick={() => setTheme(dark ? "light" : "dark")}
        aria-label={dark ? t("settings.theme_light") : t("settings.theme_dark")}
      >
        {dark ? <IconSun size={17} /> : <IconMoon size={17} />}
      </button>

      <div className="firstrun-card">
        <img className="firstrun-mark" src={mark} alt="PHANTOM" width={88} height={88} />
        <div className="firstrun-name">PHANTOM</div>
        <p className="firstrun-sub">{t("firstrun.sub")}</p>

        <div className="firstrun-choose" id="firstrun-title">
          {t("firstrun.title")}
        </div>
        <div className="firstrun-languages">
          {SUPPORTED_LANGUAGES.map((code) => (
            <button
              key={code}
              type="button"
              className="firstrun-lang"
              data-testid={`firstrun-${code}`}
              onClick={() => setLanguage(code)}
              autoFocus={code === "uk"}
            >
              {LANGUAGE_NAMES[code]}
              <small>{code.toUpperCase()}</small>
            </button>
          ))}
        </div>
        <p className="firstrun-privacy">
          <IconLock size={12} />
          {t("firstrun.privacy")}
        </p>
      </div>
    </div>
  );
}
