import { useEffect, useState } from "react";
import { bridge, isDemoMode } from "./bridge";
import { FirstRun } from "./FirstRun";
import { I18nProvider, useI18n } from "./i18n";
import { Sidebar, type Tab } from "./Sidebar";
import { ExtractScreen } from "./screens/ExtractScreen";
import { HideScreen } from "./screens/HideScreen";
import { MetadataScreen } from "./screens/MetadataScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { IconInfo } from "./icons";

function AppInner() {
  const { language, t } = useI18n();
  const [tab, setTab] = useState<Tab>("hide");
  const [extractHandoff, setExtractHandoff] = useState<{ path: string } | null>(null);
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
          {/* Screens stay mounted and are only hidden: whatever the user
              typed or picked survives a trip to Settings or a language
              switch.  Nothing here touches persistent storage — the draft
              lives and dies in this process, passwords included. */}
          <div hidden={tab !== "hide"}>
            <HideScreen onGoExtract={(path) => { setExtractHandoff({ path }); setTab("extract"); }} />
          </div>
          <div hidden={tab !== "extract"}>
            <ExtractScreen handoff={extractHandoff} />
          </div>
          <div hidden={tab !== "metadata"}>
            <MetadataScreen />
          </div>
          <div hidden={tab !== "settings"}>
            <SettingsScreen />
          </div>
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
