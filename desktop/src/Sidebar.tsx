import { useRef } from "react";
import { isDemoMode } from "./bridge";
import { useI18n } from "./i18n";
import { IconExtract, IconHide, IconMetadata, IconMoon, IconSettings, IconSun } from "./icons";
import mark from "./phantom-mark.png";

export type Tab = "hide" | "extract" | "metadata" | "settings";

export function Sidebar({
  tab,
  onNavigate,
  version,
}: {
  tab: Tab;
  onNavigate: (tab: Tab) => void;
  version: string;
}) {
  const { t, theme, setTheme } = useI18n();
  const navRef = useRef<HTMLElement>(null);

  const items: Array<{ id: Tab; label: string; icon: JSX.Element }> = [
    { id: "hide", label: t("nav.hide"), icon: <IconHide size={18} /> },
    { id: "extract", label: t("nav.extract"), icon: <IconExtract size={18} /> },
    { id: "metadata", label: t("nav.metadata"), icon: <IconMetadata size={18} /> },
    { id: "settings", label: t("nav.settings"), icon: <IconSettings size={18} /> },
  ];

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const current = items.findIndex((item) => item.id === tab);
    const delta = event.key === "ArrowDown" ? 1 : -1;
    const next = (current + delta + items.length) % items.length;
    event.preventDefault();
    onNavigate(items[next].id);
    const buttons = navRef.current?.querySelectorAll<HTMLButtonElement>("button[data-tab]");
    buttons?.[next]?.focus();
  }

  const dark = theme === "dark" || (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);

  return (
    <aside className="sidebar">
      <div className="brand">
        <img className="brand-mark" src={mark} alt="" width={40} height={40} />
        <div>
          <div className="brand-name">{t("app.name")}</div>
          <div className="brand-tagline">{t("app.tagline")}</div>
        </div>
      </div>

      <nav className="nav" aria-label="PHANTOM" onKeyDown={onKeyDown} ref={navRef}>
        {items.map((item) => (
          <button
            key={item.id}
            data-tab={item.id}
            type="button"
            className="nav-item"
            aria-current={tab === item.id ? "page" : undefined}
            onClick={() => onNavigate(item.id)}
          >
            {item.icon}
            {item.label}
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <span className="app-version">
          v{version}
          {isDemoMode() ? " · demo" : ""}
        </span>
        <button
          type="button"
          className="btn-icon"
          style={{ width: 32, height: 32 }}
          onClick={() => setTheme(dark ? "light" : "dark")}
          aria-label={dark ? t("settings.theme_light") : t("settings.theme_dark")}
          title={dark ? t("settings.theme_light") : t("settings.theme_dark")}
        >
          {dark ? <IconSun size={16} /> : <IconMoon size={16} />}
        </button>
      </div>
    </aside>
  );
}
