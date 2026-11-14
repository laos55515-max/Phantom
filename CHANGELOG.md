# Changelog

All notable changes to PHANTOM are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [2.0.0] — release candidate

Released under the MIT license (see `LICENSE`).

Complete rewrite of the 1.x single-file application. Existing v1 files remain
readable; new files are written in the v2 format.

### Added

- Versioned, authenticated container: 50-byte sealed header, AES-256-GCM with
  fresh salt and nonce, AAD binding the header, version and KDF profile.
- Argon2id key derivation (m=96 MiB, t=3, p=1) as the default profile, with
  PBKDF2-HMAC-SHA256 (600000 iterations) selectable.
- Feistel permutation spreading the payload across the whole bit plane instead
  of sequential LSB, seeded from the public header.
- PNG carriers: 8-bit, colour types 0/2/3/4/6, transparency-aware capacity,
  palette and bitmap notices, deliberate refusal of 16-bit and APNG.
- Audio carriers: WAV 8/16/24/32-bit and FLAC 16/24-bit, with a hand-written
  RIFF parser that preserves chunk layout, odd-size padding and metadata;
  lossy formats refused before reading.
- Payload metadata `type | name | sha256 | length | zlib`, filename sanitising
  and forced output extensions.
- Self-check before publishing: the output is re-read and the payload extracted
  and compared with SHA-256; failure leaves no file behind.
- Shredding with progress, `fsync` per chunk, and an explicit warning that
  overwriting is risk reduction rather than a guarantee.
- CustomTkinter interface: three languages with key and placeholder parity,
  Dark/Light/System themes, drag & drop, debounced capacity estimation, animated
  progress, clipboard auto-clear after 30 seconds.
- Worker thread with progress queue, cooperative cancellation and a watchdog;
  no Tk call is made from any thread but the main one.
- Test suite: stub scanner, known-answer vectors for Argon2id/PBKDF2/AES-GCM,
  legacy golden fixtures generated from the original commit, WAV/FLAC round
  trips verified by a second decoder, property tests, GUI smoke tests.

### Changed

- Layout split into `phantom.core`, `phantom.stego`, `phantom.audio`,
  `phantom.ui`, `phantom.i18n`; `core.py` removed.
- Capacity formula now subtracts the 16-byte GCM tag, so an exactly-sized
  payload no longer overflows the permuted range.
- Locale strings no longer hard-code KDF numbers; they are formatted from
  constants at run time.

### Fixed

- AES-256-GCM is now used correctly (1.x reused salt and nonce in places).
- LSB matching no longer skips pixels whose channel is 0 or 255.
- v1 extraction no longer reports a spurious integrity failure when the payload
  was written by the original application.

### Removed

- The monolithic `core.py` and the duplicated logic it carried.
- Any wording in the interface that implied undetectability.

## [1.0.0]

Initial release. Single-file script, sequential LSB, PBKDF2-HMAC-SHA256 with
200000 iterations and no versioned container.
