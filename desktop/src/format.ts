const KI = 1024;
const MI = 1024 * 1024;
const GI = 1024 * 1024 * 1024;

export interface SizeUnits {
  b: string;
  kb: string;
  mb: string;
  gb: string;
}

export function formatBytes(bytes: number, units: SizeUnits, digits = 1): string {
  if (!Number.isFinite(bytes) || bytes < 0) return `0 ${units.b}`;
  if (bytes < KI) return `${bytes} ${units.b}`;
  if (bytes < MI) return `${trim(bytes / KI, digits)} ${units.kb}`;
  if (bytes < GI) return `${trim(bytes / MI, digits)} ${units.mb}`;
  return `${trim(bytes / GI, digits)} ${units.gb}`;
}

function trim(value: number, digits: number): string {
  return value.toFixed(digits).replace(/\.0$/, "");
}

export function formatChars(count: number): string {
  return new Intl.NumberFormat().format(count);
}

export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** Rough packed-size estimate: payload + container metadata + worst-case zlib slack. */
export function estimatePackedBytes(userBytes: number, nameBytes: number): number {
  // PAYLOAD_META_OVERHEAD (39) + GCM tag (16, reserved in the capacity maths) + ~0.4% slack.
  return Math.ceil(userBytes + 39 + 16 + nameBytes + userBytes / 256);
}

export type Strength = "weak" | "fair" | "strong";

export function passwordStrength(password: string): Strength {
  const length = password.length;
  if (length < 12) return "weak";
  const classes =
    Number(/[a-z]/.test(password)) +
    Number(/[A-Z]/.test(password)) +
    Number(/[0-9]/.test(password)) +
    Number(/[^A-Za-z0-9]/.test(password));
  if (length >= 20 || (length >= 16 && classes >= 3) || (length >= 14 && classes >= 4)) {
    return "strong";
  }
  return "fair";
}

export function generatePassword(length = 20): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  // Rejection sampling: 2^32 is not a multiple of 62, so a plain modulo would
  // slightly favour the first few characters.
  const limit = Math.floor(0xffffffff / alphabet.length) * alphabet.length;
  const out: string[] = [];
  const buffer = new Uint32Array(length * 2);
  while (out.length < length) {
    crypto.getRandomValues(buffer);
    for (let i = 0; i < buffer.length && out.length < length; i += 1) {
      if (buffer[i] < limit) out.push(alphabet[buffer[i] % alphabet.length]);
    }
  }
  return out.join("");
}
