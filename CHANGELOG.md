# Changelog

All notable changes to PHANTOM are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The interface names the container layout: the extract tab's audit card shows a
  `FORMAT` row (`v3 · keyed positions` or `v2 · public header · older file`,
  localised), and the embed tab says which layout this build writes. The version
  is authenticated inside the header, so it is reported only after a payload was
  opened; every number comes from `phantom.core.constants`, never a locale file.
- `tools/bench_positions.py` measures what the v3 bootstrap key costs next to the
  Argon2id run the container already pays for: one extra PBKDF2-HMAC-SHA256
  (600000 iterations) derivation, ~0.11 s on the development machine and
  independent of carrier size (0.1 % of a 12 MP embed, which is dominated by PNG
  I/O).

### Security

- Container format v3: the positions of **every** bit in the carrier, the sealed
  header included, are derived from the password, so nothing sits at a place a
  reader without the password can compute. Previously the header was placed by
  a constant-seeded permutation, which told an attacker exactly which 400 bits
  to read before knowing anything. The position key is PBKDF2-HMAC-SHA256
  (600000 iterations) over the password with a fixed domain salt — the
  per-message salt lives inside the header, which cannot be found before the key
  exists — and is HKDF-separated from the message keys, so the choice of KDF
  profile for the payload does not move a single bit. Trade-off documented in
  `SECURITY.md`: for one password and one carrier size the layout is the same in
  every file.
- Version 2 carriers still open: extraction tries the v3 layout, then the v2
  one, and the version is authenticated inside the header, so one layout can
  never pass as the other. `stego.engine.embed_payload(container_version=…)`
  writes the v2 layout for files an older build has to open; the app itself
  always writes v3.
- New tests pin the property that mattered: two passwords place the header in
  different slots, the header never overlaps the payload, a v3 carrier does not
  authenticate through the v2 positions, and a golden v2 fixture written before
  this change keeps extracting (text, file name and SHA-256 unchanged).
- Refuse an output path that resolves to the file being hidden, including the
  path the forced output extension produces and paths reached through symlinks;
  the check now runs before anything is written, with or without secure deletion.
- Sanitise every file name read from a container: one path segment, no
  separators, no control or Windows-reserved characters, `""`, `.` and `..`
  rejected, the joined path re-checked against the resolved output directory.
- `EmbedOutcome.verified` now reports a self-check that actually ran, a source
  file is never wiped without it, and the UI says "not verified" instead of
  "saved" when verification is off.
- Restored files are written to a temp file, read back, compared with the sealed
  SHA-256 and only then replaced, so a failed or attacked write keeps an existing
  file intact; they are created `0600` on POSIX.
- Corrected the threat-model text: the header positions are known, decryption
  without the password is impossible, but the presence of steganography is
  detectable by statistical analysis. PBKDF2 is documented as an explicitly
  selected profile, never an automatic downgrade.

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
- PNG carriers: 1/2/4/8-bit, colour types 0/2/3/4/6, transparency-aware capacity,
  palette and bitmap notices, lossless expansion of 1/2/4-bit images,
  deliberate refusal of 16-bit and APNG.
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
  trips verified by a second decoder, property tests, GUI smoke tests under
  Xvfb. The GUI tests never open a real modal dialog — dialogs are recorded and
  fail the test with their text, so a stuck message box cannot hang CI.

### Changed

- Layout split into `phantom.core`, `phantom.stego`, `phantom.audio`,
  `phantom.ui`, `phantom.i18n`; `core.py` removed.
- Capacity formula now subtracts the 16-byte GCM tag, so an exactly-sized
  payload no longer overflows the permuted range.
- Locale strings no longer hard-code KDF numbers; they are formatted from
  constants at run time.
- Locales grew to 205 keys (paste/copy buttons, context-menu items, result
  window) with full uk/ru/en parity.

### Fixed

- macOS text editing: `Cmd+C/V/X/A` works in every entry and textbox, a
  right-click menu offers Paste/Copy/Cut/Select all, and `PASTE` / `COPY KEY`
  buttons next to the password fields cover systems where the shortcut never
  arrives. The shortcuts are bound on the inner Tk widget, so they cannot be
  swallowed by CustomTkinter proxies and cannot paste twice.
- Scrolling: the mouse wheel and the trackpad scroll the whole tab instead of
  only the scrollbar. Wheel events are routed from any child widget to its
  scrollable frame, the macOS delta is inverted instead of being divided by 120
  (which rounded a trackpad notch to zero), and a text box keeps the wheel while
  it still has room to scroll itself.
- Reading results: `EXPAND` opens the result at 80 % of the screen with a
  read-only, selectable copy of the text plus `COPY TEXT`, `COPY PATH`,
  `SHOW IN FINDER` (`open -R` / `explorer /select,` / `xdg-open`) and `CLOSE`;
  the main result row gained the same path actions.

- AES-256-GCM is now used correctly (1.x reused salt and nonce in places).
- The GUI smoke tests drive the real widgets: the embed test used to fill only
  the view model, so the app rejected the request and blocked on a modal error
  box. CI also caps the GUI step and echoes its log as annotations.
- Lossy audio is now refused by file signature before the extension is trusted,
  so a `.wav`-named OGG/MP3 (including an `ID3`-prefixed file) cannot slip
  through and be read as PCM.
- A missing `libsndfile`/`soundfile` reports `AudioFormatError` instead of
  escaping as a bare `ImportError` from the FLAC reader.
- LSB matching no longer skips pixels whose channel is 0 or 255.
- v1 extraction no longer reports a spurious integrity failure when the payload
  was written by the original application.

### Removed

- Unreachable validation branches that duplicated a check already enforced by
  the caller (`Payload` filename length, capacity after compression, WAV splice
  length, the password-class guard in `generate_password`), and an unreachable
  FLAC sample-range guard.

- The monolithic `core.py` and the duplicated logic it carried.
- Any wording in the interface that implied undetectability.

## [1.0.0]

Initial release. Single-file script, sequential LSB, PBKDF2-HMAC-SHA256 with
200000 iterations and no versioned container.
