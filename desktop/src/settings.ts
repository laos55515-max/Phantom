export type Theme = "light" | "dark" | "system";

export interface DraftSettings {
  /** null until the first-launch language choice is made */
  language: "uk" | "en" | "ru" | null;
  theme: Theme;
}

const STORAGE_KEY = "phantom.settings.draft";
const LANGUAGES = new Set(["uk", "en", "ru"]);

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark" || value === "system";
}

export function loadDraft(): DraftSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { language: null, theme: "system" };
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) {
      return { language: null, theme: "system" };
    }
    const record = parsed as Record<string, unknown>;
    const language =
      typeof record.language === "string" && LANGUAGES.has(record.language)
        ? (record.language as "uk" | "en" | "ru")
        : null;
    const theme = isTheme(record.theme) ? record.theme : "system";
    return { language, theme };
  } catch {
    return { language: null, theme: "system" };
  }
}

export function saveDraft(draft: DraftSettings): void {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...draft, savedAt: new Date().toISOString() }),
    );
  } catch {
    // localStorage unavailable (quota / private mode): the session keeps the
    // values in memory; nothing sensitive is lost because nothing sensitive
    // is stored here at all.
  }
}

function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function resolveTheme(theme: Theme): "light" | "dark" {
  if (theme === "system") return systemPrefersDark() ? "dark" : "light";
  return theme;
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = resolveTheme(theme);
  document.documentElement.dataset.themeChoice = theme;
}

/** Live-follow the OS while the choice is "system". */
export function watchSystemTheme(onChange: () => void): () => void {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const handler = () => onChange();
  media.addEventListener("change", handler);
  return () => media.removeEventListener("change", handler);
}
