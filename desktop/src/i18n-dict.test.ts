import { describe, expect, it } from "vitest";
import { dictionaries, SUPPORTED_LANGUAGES } from "./i18n-dict";
import ukLocale from "../../src/phantom/locales/uk.json";
import enLocale from "../../src/phantom/locales/en.json";
import ruLocale from "../../src/phantom/locales/ru.json";

const PYTHON_LOCALES: Record<string, Record<string, string>> = {
  uk: ukLocale,
  en: enLocale,
  ru: ruLocale,
};

describe("i18n dictionaries", () => {
  const [first, ...rest] = SUPPORTED_LANGUAGES;
  const reference = Object.keys(dictionaries[first]).sort();

  it("has exactly one dictionary per supported language", () => {
    for (const language of SUPPORTED_LANGUAGES) {
      expect(dictionaries[language], language).toBeDefined();
    }
  });

  it("keeps every key in every language (no missing translations)", () => {
    for (const language of rest) {
      const keys = Object.keys(dictionaries[language]).sort();
      expect(keys, `${language} vs ${first}`).toEqual(reference);
    }
  });

  it("has no empty values", () => {
    for (const language of SUPPORTED_LANGUAGES) {
      for (const [key, value] of Object.entries(dictionaries[language])) {
        expect(value.trim().length, `${language}:${key}`).toBeGreaterThan(0);
      }
    }
  });

  it("interpolates the same placeholders in every language", () => {
    const placeholders = (value: string) =>
      [...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
    for (const key of Object.keys(dictionaries[first])) {
      const expected = placeholders(dictionaries[first][key]);
      for (const language of rest) {
        expect(
          placeholders(dictionaries[language][key]),
          `${language}:${key}`,
        ).toEqual(expected);
      }
    }
  });

  it("defines every meta.* key the Python core can emit, in the core's own words", () => {
    // The core sends a locale key for a structured reason ("kept by policy"), a
    // prose value ("12 bytes") and every category/format/provenance label.  A
    // missing key would show a raw identifier inside a translated card, so the
    // desktop dictionaries carry the same strings the Tkinter panel uses.
    for (const language of SUPPORTED_LANGUAGES) {
      const locale = PYTHON_LOCALES[language];
      for (const [key, value] of Object.entries(locale)) {
        if (!key.startsWith("meta.")) continue;
        expect(dictionaries[language][key], `${language}:${key}`).toBe(value);
      }
    }
  });

  it("defines every err.* key the Python core can emit", () => {
    const coreCodes = [
      "wrong_password_or_no_data",
      "capacity",
      "unsupported_format",
      "unsupported_carrier",
      "integrity",
      "cancelled",
      "invalid_password",
      "invalid_input",
      "invalid_unicode",
      "io",
      "output_is_carrier",
      "output_exists",
      "source_overwrite",
      "unsafe_path",
      "audio_lossy",
      "audio_format",
      "audio_silence",
      "signed_media",
      "metadata",
      "metadata_conflict",
      "internal",
    ];
    for (const language of SUPPORTED_LANGUAGES) {
      for (const code of coreCodes) {
        expect(dictionaries[language][`err.${code}`], `${language}:err.${code}`).toBeTruthy();
      }
    }
  });
});
