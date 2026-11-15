# Security model

This document describes what PHANTOM 2.0.0 protects, what it does not protect,
and which numbers are constants in the source code. It is written to be checked
against `src/phantom/core/constants.py`; `tests/test_security_claims.py` fails if
the two drift apart.

## Summary

- Cipher: **AES-256-GCM** (`cryptography` crate bindings only)
- Key derivation: **Argon2id · m=96 MiB · t=3 · p=1**, 16-byte random salt per
  operation, 12-byte random nonce per encryption
- Alternate profile: PBKDF2-HMAC-SHA256, 600000 iterations (`KDF_PROFILE_PBKDF2`),
  selected explicitly when embedding (`profile=`) and recognised from the
  authenticated header when extracting. There is **no automatic fallback**: if
  Argon2id is unavailable, the operation fails instead of silently downgrading,
  and a file embedded with PBKDF2 is never re-encrypted behind the user's back.
- Password normalisation: NFKC, minimum 12 characters
- Integrity: GCM tag (16 bytes) plus a SHA-256 digest of the plaintext stored in
  the payload header, both checked before anything is written to disk
- Payload pipeline: `type | name | sha256 | length | zlib(plaintext)`
- No network access, no telemetry, no analytics, no update check

## Assets

1. The plaintext payload (text or file contents).
2. The existence of the payload in the carrier.
3. The password.

## Adversary

The realistic adversary reads the file, knows the format, and has steganalysis
tools. The realistic channel is a messenger or a shared folder, where the file
may be re-encoded before it reaches the recipient.

## What is protected

- **Content.** Without the password, recovering the plaintext requires breaking
  AES-256-GCM or guessing the password. Password guesses cost one Argon2id
  evaluation each (96 MiB, t=3), which is deliberately expensive.
- **Content, and now the layout.** Since format v3 every bit of the carrier —
  the 50-byte header included — sits at a position derived from the password:

  ```
  B = Feistel(position_key(password), N)      # one keyed bijection over the carrier
  header bit i   -> B(i)                      i  < 400
  payload bit j  -> B(400 + π(j))             π keyed by the per-message key
  ```

  `position_key` is PBKDF2-HMAC-SHA256 (600000 iterations) over the password
  with the fixed domain salt `POSITION_KDF_SALT`, HKDF-separated from the
  message keys. A reader without the password therefore has no region to read:
  the header is not at a documented offset any more, and a wrong password
  produces different positions whose bits fail the tag check exactly like a
  carrier with no payload. There is still no magic string or version mark in
  clear text.
- **Documented trade-off of the fixed salt.** The per-message salt lives inside
  the header, so the header cannot be located before the position key exists —
  hence a fixed, public salt for that derivation and no per-message salt. The
  consequence: for one password and one carrier size, the position set is the
  same in every file. It reveals nothing about the payload, but two carriers of
  the same size hidden with the same password share their layout. Changing the
  password, or the carrier size, changes every position.
- **Compatibility.** Carriers written in the v2 layout (header at
  `public_permutation(N)[:400]`, payload keyed) are still read: extraction tries
  the v3 layout first and, if no profile authenticates, the v2 layout. The
  version is authenticated inside the header, so a v2 header never passes as v3
  or the other way round. Writing v2 is possible through the engine's
  `container_version` argument for carriers an older build has to open; the app
  itself always writes the current version.
- **Tampering.** Any modification of the header, the ciphertext, or the
  authenticated data (header bytes, container version, KDF profile id) makes
  decryption fail with an integrity error. The app never returns partially
  decrypted data.
- **Path traversal.** Every file name read from a container is reduced to a
  single path segment (`basename`, with `\` treated as a separator, control and
  Windows-reserved characters replaced, and `""`, `.` and `..` rejected), the
  joined path is re-checked against the resolved output directory, and a
  pre-existing symlink at the destination is refused instead of followed.
- **Accidental self-destruction.** The output extension is forced, so a check on
  the requested name would miss the file that is really written; therefore both
  the carrier and the hidden source file are compared against every path the
  forced extension can produce, through `realpath` and inode identity, before a
  byte is written.
- **Data loss on failure.** Extraction writes to a sibling temp file, reads it
  back, compares the SHA-256 sealed in the container and only then replaces the
  destination, so a failed or attacked write leaves an existing file untouched.
  Restored files are created `0600` on POSIX (Windows only has the read-only bit;
  there `os.chmod` is intentionally not used to promise more than it can do).

## What is not protected

- **The fact of steganography, in general.** LSB steganography is detectable by
  statistical steganalysis (chi-square, RS analysis, sample-pair analysis).
  Embedding a large payload changes the statistics of the lowest bit plane.
  Keeping the payload below ~70 % of capacity reduces, but does not remove, the
  signal. The experimental adaptive layouts (v4, v5) reduce the signal — v5 uses
  about half the changes of the default layout and concentrates them where the
  image is busy — but they do not make embedding undetectable, and a detector
  written against this scheme specifically would still have something to work
  with: the number of changes is public, only their placement is keyed. This is a
  property of the method, not a bug to be patched.
- **Metadata.** PNG chunks outside `IDAT` keep whatever the carrier had. File
  timestamps, size and EXIF-like metadata survive.
- **Re-encoding.** JPEG export, messenger "photo" mode, resizing, cropping,
  screenshots and audio transcoding destroy the payload. Send the file as a
  document, not as a photo.
- **Compromised endpoints.** A keylogger, screen capture or memory dump on
  either machine defeats any file encryption.
- **Chosen-carrier attacks.** An adversary who supplies the carrier and later
  gets the stego file back can compare the two.
- **Traffic analysis.** The fact that two parties exchanged a file is visible to
  the channel.

## Wiping

"Shred" overwrites the file once (`WIPE_PASSES = 1`), in 1 MiB chunks, and calls
`fsync` after each chunk, then deletes it. On SSDs with wear levelling, on
copy-on-write filesystems (APFS, Btrfs, ZFS) and inside journaling filesystems,
older copies may persist in blocks the application cannot address.

Treat wiping as **risk reduction, not a guarantee**. The confirmation dialog says
exactly that; the code does not claim otherwise.

**Memory buffers.** Key material and the encoded password are `bytearray`s and are
zeroed with `core.security.secure_zeroize` as soon as they are no longer needed —
`MessageKeys.wipe()` after sealing or opening, the password bytes right after the
KDF run. Python cannot wipe an immutable `str`: the characters the user typed, and
any copy the interpreter made, stay in memory until the garbage collector reuses
the page, and they may have been written to swap. The helper raises `TypeError`
instead of silently doing nothing when it is handed an immutable value, so a
mistaken call cannot look like it worked.

**Clipboard.** A copied password, key or extracted text is cleared after
`CLIPBOARD_CLEAR_SECONDS` (30 s) and again when the window closes, through the
platform's own tool — `pbcopy` on macOS, `clip` on Windows, `wl-copy`, `xclip` or
`xsel` on Linux — rather than through Tk alone, because Tk can only reach the
system clipboard while it owns the selection and on macOS the text outlives the
window. The clear runs only when the clipboard still holds the text PHANTOM put
there; another application's content is left alone. Clipboard history, cloud
clipboard sync (Universal Clipboard, Windows clipboard history) and screenshots
are outside our reach: a copied secret may survive there.

## Password guidance

- Minimum 12 characters; the generator produces 20.
- Passwords are NFKC-normalised, so visually identical Unicode passwords match.
- A passphrase of several unrelated words is stronger and easier to retype than
  a short string of symbols.
- Losing the password loses the payload. There is no recovery path, no backdoor
  and no escrow.

## Legacy versions

### Version 2 (PHANTOM 2.0.0)

Readable, never written by the app: the identical AEAD container with the header
at constant-seeded positions, which is the weakness v3 removes. Extraction costs
one extra KDF pass on such files, because the v3 layout is tried first.

### Version 1

Files written by PHANTOM 1.x are still readable: PBKDF2-HMAC-SHA256 with 200000
iterations, sequential LSB, no magic. This path is read-only, isolated in
`phantom.stego.legacy`, and covered by golden fixtures generated from the
original code at commit `4920c65`. v1 is weaker than v2 (sequential placement,
lower KDF cost); prefer re-embedding old secrets in the v2 format.

## Reporting

Open an issue describing the impact and the affected version. Do not include
real payloads, passwords or personal media. There is no bug bounty.
