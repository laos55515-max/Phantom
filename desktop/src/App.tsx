import { useEffect, useState } from "react";
import { bridge, isDemoMode } from "./bridge";
import { FirstRun } from "./FirstRun";
import { I18nProvider, useI18n } from "./i18n";
import { Sidebar, type Tab } from "./Sidebar";
import { ExtractScreen } from "./screens/ExtractScreen";
import { HideScreen } from "./screens/HideScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { IconInfo } from "./icons";

function AppInner() {
  const { language, t } = useI18n();
  const [tab, setTab] = useState<Tab>("hide");
  const [version, setVersion] = useState("2.1.0");

  useEffect(() => {
    let cancelled = false;
    bridge
      .appInfo()
      .then((info) => {
        if (!cancelled && info?.app) setVersion(info.app);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="app">
      <a className="skip-link" href="#main-content">
        {t("skip_to_content")}
      </a>
      <Sidebar tab={tab} onNavigate={setTab} version={version} />
      <main className="main" id="main-content">
        <div className="content">
          {isDemoMode() ? (
            <div className="demo-banner" role="note">
              <IconInfo size={14} />
              <span>{t("demo.banner")}</span>
            </div>
          ) : null}
          {tab === "hide" ? <HideScreen onGoExtract={() => setTab("extract")} /> : null}
          {tab === "extract" ? <ExtractScreen /> : null}
          {tab === "settings" ? <SettingsScreen /> : null}
        </div>
      </main>
      {language === null ? <FirstRun /> : null}
    </div>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <AppInner />
    </I18nProvider>
  );
}
