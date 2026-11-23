import { describe, expect, it } from "vitest";
import { dictionaries, SUPPORTED_LANGUAGES } from "./i18n-dict";

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
      "source_overwrite",
      "unsafe_path",
      "audio_lossy",
      "audio_format",
      "audio_silence",
      "signed_media",
      "internal",
    ];
    for (const language of SUPPORTED_LANGUAGES) {
      for (const code of coreCodes) {
        expect(dictionaries[language][`err.${code}`], `${language}:err.${code}`).toBeTruthy();
      }
    }
  });
});
