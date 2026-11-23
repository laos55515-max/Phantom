import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { dictionaries, LANGUAGE_NAMES, SUPPORTED_LANGUAGES, type Dictionary, type Language } from "./i18n-dict";
import { loadDraft, saveDraft, applyTheme, watchSystemTheme, type Theme } from "./settings";

export { LANGUAGE_NAMES, SUPPORTED_LANGUAGES };
export type { Language, Theme };

export interface I18n {
  /** null → first launch, the language picker is showing */
  language: Language | null;
  /** Dictionary of the active language; before first choice, English. */
  t: (key: string, params?: Record<string, string | number>) => string;
  setLanguage: (language: Language) => void;
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const Context = createContext<I18n | null>(null);

function interpolate(template: string, params?: Record<string, string | number>): string {
  if (!params) return template;
  let out = template;
  for (const [key, value] of Object.entries(params)) {
    out = out.split(`{${key}}`).join(String(value));
  }
  return out;
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState(() => loadDraft());

  const { theme } = draft;
  const language = draft.language;

  useEffect(() => {
    applyTheme(theme);
    const unwatch = watchSystemTheme(() => applyTheme(loadDraft().theme));
    return unwatch;
  }, [theme]);

  useEffect(() => {
    document.documentElement.lang = language ?? "en";
  }, [language]);

  const update = useCallback((patch: Partial<typeof draft>) => {
    setDraft((previous) => {
      const next = { ...previous, ...patch };
      saveDraft(next); // the "draft" the spec asks for: auto-saved, restored on launch
      return next;
    });
  }, []);

  const setLanguage = useCallback(
    (next: Language) => update({ language: next }),
    [update],
  );
  const setTheme = useCallback((next: Theme) => update({ theme: next }), [update]);

  const t = useCallback(
    (key: string, params?: Record<string, string | number>) => {
      const active: Dictionary = dictionaries[language ?? "en"];
      const template = active[key] ?? dictionaries.en[key] ?? key;
      return interpolate(template, params);
    },
    [language],
  );

  const value = useMemo<I18n>(
    () => ({ language, t, setLanguage, theme, setTheme }),
    [language, t, setLanguage, theme, setTheme],
  );

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useI18n(): I18n {
  const context = useContext(Context);
  if (!context) throw new Error("useI18n must be used inside <I18nProvider>");
  return context;
}
