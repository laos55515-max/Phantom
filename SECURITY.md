# Security model

This document describes what PHANTOM 2.0.0 protects, what it does not protect,
and which numbers are constants in the source code. It is written to be checked
against `src/phantom/core/constants.py`; `tests/test_security_claims.py` fails if
the two drift apart.

## Summary

- Cipher: **AES-256-GCM** (`cryptography` crate bindings only)
- Key derivation: **Argon2id · m=96 MiB · t=3 · p=1**, 16-byte random salt per
  operation, 12-byte random nonce per encryption
- Alternate profile: PBKDF2-HMAC-SHA256, 600000 iterations (`KDF_PROFILE_PBKDF2`)
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
- **Existence, up to the password.** The 50-byte sealed header is encrypted and
  authenticated; a wrong password or a carrier without data fails the tag check
  and is indistinguishable from random bits. There is no magic string in clear
  text to search for.
- **Tampering.** Any modification of the header, the ciphertext, or the
  authenticated data (header bytes, container version, KDF profile id) makes
  decryption fail with an integrity error. The app never returns partially
  decrypted data.
- **Path traversal.** Extracted file names are sanitised; the output extension
  is forced; writing over the carrier itself is refused.

## What is not protected

- **The fact of steganography, in general.** LSB steganography is detectable by
  statistical steganalysis (chi-square, RS analysis, sample-pair analysis).
  Embedding a large payload changes the statistics of the lowest bit plane.
  Keeping the payload below ~70 % of capacity reduces, but does not remove, the
  signal. This is a property of the method, not a bug to be patched.
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

## Password guidance

- Minimum 12 characters; the generator produces 20.
- Passwords are NFKC-normalised, so visually identical Unicode passwords match.
- A passphrase of several unrelated words is stronger and easier to retype than
  a short string of symbols.
- Losing the password loses the payload. There is no recovery path, no backdoor
  and no escrow.

## Legacy version 1

Files written by PHANTOM 1.x are still readable: PBKDF2-HMAC-SHA256 with 200000
iterations, sequential LSB, no magic. This path is read-only, isolated in
`phantom.stego.legacy`, and covered by golden fixtures generated from the
original code at commit `4920c65`. v1 is weaker than v2 (sequential placement,
lower KDF cost); prefer re-embedding old secrets in the v2 format.

## Reporting

Open an issue describing the impact and the affected version. Do not include
real payloads, passwords or personal media. There is no bug bounty.
