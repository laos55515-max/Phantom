# Changelog

All notable changes to PHANTOM are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Long operations now say what they are doing.** With v5 as the default a 24 MP
  photo takes about half a minute to embed, and the interface used to show a
  progress bar frozen at 0 % for the first stretch of it — indistinguishable from
  a hang. Four changes fix that:

  * a new `Stage.ANALYSING` for the wavelet cost map, the one phase that cannot
    report a fraction (it is a single opaque pass over the image). `Stage` values
    double as locale keys, so it is named in all three languages, and it joins
    `Stage.KDF` in `INDETERMINATE_STAGES` — the interface already knew how to show
    a spinner for those, it just had nothing to spin for;
  * the engine announces it before the cost map is computed (both for the v5
    trellis and for the v4 greedy layout) and only when the carrier is an opaque
    image, so audio and transparent-pixel carriers do not claim an analysis they
    never run;
  * `_read_stc_payload` reports every block chunk instead of only the endpoints:
    extracting a 24 MP file used to sit at 0 % for ten seconds and then jump to
    100 %;
  * the footer carries an elapsed-time counter (`C.STAGE_TICK_MS`, one repaint per
    second), so a phase that reports nothing still visibly moves. It stops and
    resets when the operation ends — including on cancel and failure, which used
    to leave the last "embedding · 74 %" line on screen forever — and a language
    switch mid-operation repaints the live line in the new language rather than
    reverting it to "idle".

  The line itself is built by `ui.format.stage_line`, which is free of Tk, so the
  wording is covered by the headless tests; the tests also pin that the clock is a
  suffix (`Встраивание  ·  25% · 12 с`), that an indeterminate phase never shows a
  percentage, and that a stuck phase keeps counting past a minute without changing
  units.

- **`SECURITY.md` states the price of the half-resolution cost map** — 0.25 dB at
  0.2 bpp, 0.18 dB at 0.4 bpp, measured against the exact map — instead of leaving
  it in the CHANGELOG only. The READMEs mention the trade-off and the new progress
  feedback.

- **Experimental syndrome-trellis layout (v5).** The adaptive layout picked the
  cheapest samples and wrote one bit into each — greedy, and blind to the fact
  that one ±1 change can satisfy several parity constraints at once. The new
  `phantom.stego.stc` module embeds with a **syndrome-trellis code**: it looks for
  the change vector `e` that minimises the total distortion `Σ ρᵢeᵢ` subject to
  `H e = σ`, where `σ` is the syndrome carrying the payload. `Ĥ` is a
  `height × width` binary matrix placed block-wise on the diagonal, the trellis
  has `2**height` states holding the block's partial syndrome, and a Viterbi pass
  finds the shortest path; with `height ≤ 8` and blocks of 6–256 samples the whole
  thing runs as a handful of NumPy operations per layer, batched across every
  block at once. Each file uses the widest block it can afford, because a wider
  block carries a bit with fewer changes (`≈ 1 / log₂ width` per bit).

  `Ĥ` is derived from the password, so the syndromes cannot even be computed
  without it, and — unlike v4 — **nothing about extraction depends on the image**:
  the trellis comes from the carrier size, the payload length and the key, so the
  layout cannot drift and needs no verification, no mask invariant and no fallback
  check. `engine.embed_payload(..., container_version=5)` writes it, and since the
  change below it is what the build writes by default.

  Same eight carriers, same payloads, all three layouts, extraction verified on
  every file (`96 embeds in 142.7 s, 0 unreadable`). `Σρ/бит` is the total cost
  of the changes divided by the payload under one shared cover-derived map — the
  number a trellis is built to minimise:

  | load, bpp | layout | PSNR, dB | changed px, % | Σρ/bit | χ² p | RS p̂ | detected |
  |---|---|---|---|---|---|---|---|
  | 0.05 | v3 keyed | 68.93 | 2.48 | 0.50 | 0.000 | 0.012 | 0/6 |
  | 0.05 | v4 greedy | 68.95 | 2.38 | 0.46 | 0.000 | 0.024 | 0/6 |
  | 0.05 | **v5 trellis** | **72.82** | **1.01** | **0.19** | 0.000 | 0.012 | 0/6 |
  | 0.10 | v3 | 65.89 | 4.94 | 0.50 | 0.000 | 0.004 | 0/6 |
  | 0.10 | v4 | 65.92 | 4.72 | 0.46 | 0.000 | 0.037 | 0/6 |
  | 0.10 | **v5** | **70.27** | **1.80** | **0.17** | 0.000 | 0.011 | 0/6 |
  | 0.20 | v3 | 62.90 | 9.67 | 0.50 | 0.000 | 0.009 | 0/6 |
  | 0.20 | v4 | 62.91 | 9.26 | 0.46 | 0.000 | 0.063 | 0/6 +3 suspect |
  | 0.20 | **v5** | **67.35** | **3.51** | **0.17** | 0.000 | **0.008** | 0/6 |
  | 0.40 | v3 | 59.90 | 18.68 | 0.50 | 0.000 | 0.007 | 0/6 |
  | 0.40 | v4 | 59.90 | 17.61 | 0.47 | 0.000 | 0.085 | 0/6 +3 suspect |
  | 0.40 | **v5** | **63.25** | **8.74** | **0.22** | 0.000 | **0.010** | 0/6 |

  The code wins on every axis it can control: three to four decibels more PSNR,
  2–2.9× lower total distortion, 2–3× fewer changed samples, chi-square still at
  zero. The RS estimator — which reads the *density* of changes and had climbed to
  0.085 (three carriers in the "suspect" band) under greedy v4 — comes back to the
  v3 level, 0.008–0.012, and no carrier is flagged at any load. Spreading a
  smaller number of changes over the whole carrier beats concentrating a larger
  number in the cheapest corner, even though the greedy layout wins the placement
  columns (`перцентиль`, `в гладких 50%`): the cost model is additive and does not
  know that a dense cluster is easier to see than the same changes spread out.

  Limits, stated in the module: blocks are independent, so the optimum is
  per-block rather than global (the published trellis slides by one sample, which
  is hundreds of times slower in pure Python and buys a fraction of a percent);
  and the *number* of changes is public — only their placement is keyed.

- **Experimental adaptive layout (v4).** LSB matching spreads its changes
  blindly, and blind changes are what a texture-aware detector looks for. The new
  `phantom.stego.adaptive` module computes a cost map from the image itself — a
  two-level Daubechies-8 wavelet decomposition (db4, four vanishing moments, so a
  smooth gradient produces almost no response), the detail energy of the HL/LH/HH
  bands, a HILL-style spread over a 15×15 neighbourhood, and a cost of
  `1 / (energy + floor)` normalised to a median of 1 — and the payload goes to the
  cheapest samples: foliage, edges, noise, never the sky. Costs are log-quantised
  into six buckets, ordered inside each bucket by a password-derived permutation,
  so the layout is a function of the key and the image and never of the payload.
  `engine.embed_payload(..., container_version=4)` writes it; v3 remains the
  default and every existing file still opens.
  
  The one structural problem was reproducibility: the receiver has no cover, so
  it must rebuild the layout from the stego image, and a cost map computed after
  the embedding is not the same map (measured: 1–1255 positions drifted
  depending on bucket count, which corrupts the payload). The fix is a **mask
  invariant**: a change is written in the direction that keeps the sample inside
  its 16-value group (low nibble `0000` goes up, `1111` goes down, otherwise a
  keyed coin), so
  `value >> 4` never moves, the coarse image is identical on both sides, and the
  costs — computed from that coarse image — are provably identical. The engine
  still re-derives the layout after writing and falls back to v3 if it ever
  disagrees, but with the mask invariant it never does: 64/64 benchmark embeds
  reproduced themselves.
  
  Benchmarked with `tools/bench_steganalysis.py --layout both` on the same eight
  carriers at the same payload sizes (percentile = median cost percentile of the
  changed samples, 50 % = blind):

  | load, bpp | layout | PSNR, dB | changed px, % | percentile | smooth half | χ² p | RS p̂ | detected |
  |---|---|---|---|---|---|---|---|---|
  | 0.05 | v3 | 68.95 | 2.46 | 49.9 | 49.9 % | 0.000 | 0.009 | 0/6 |
  | 0.05 | v4 | 68.93 | 2.40 | 4.7 | 1.5 % | 0.000 | 0.025 | 0/6 |
  | 0.10 | v3 | 65.93 | 4.89 | 50.1 | 50.3 % | 0.000 | 0.005 | 0/6 |
  | 0.10 | v4 | 65.90 | 4.71 | 5.4 | 0.8 % | 0.000 | 0.040 | 0/6 +1 suspect |
  | 0.20 | v3 | 62.89 | 9.70 | 50.2 | 50.2 % | 0.000 | 0.011 | 0/6 |
  | 0.20 | v4 | 62.90 | 9.25 | 7.6 | 0.4 % | 0.000 | 0.071 | 0/6 +3 suspect |
  | 0.40 | v3 | 59.90 | 18.67 | 49.9 | 49.9 % | 0.000 | 0.024 | 0/6 |
  | 0.40 | v4 | 59.89 | 17.64 | 8.4 | 0.2 % | 0.000 | 0.088 | 0/6 +3 suspect |

  Distortion is identical, placement is not: v4 puts its changes in the cheapest
  tenth of the cost map and almost never in the smooth half, where v3 is at 50 %
  by construction. Chi-square stays at 0.000 for both. RS, an estimator built for
  LSB replacement and already known to wander on LSB matching, reads *higher* for
  v4 at 0.1–0.4 bpp (0.040–0.088 against 0.005–0.024): concentrating changes in
  the noisiest regions is exactly where its group model is least reliable, so the
  number has to be reported as it is, not as a win. Both stay far below the 0.25
  "flag" line, and no carrier was called detected.
  
  Limits, stated in the module: without syndrome-trellis codes the selection is a
  hard subset, so the *set* of touched samples is visible to a detector that knows
  the scheme; the masked writer forces the direction on 2 of 16 samples, which is
  a small asymmetry in the ±1 distribution; and audio carriers, RGBA images with
  transparent pixels and payloads above four million bits fall back to v3. Not
  wired into the GUI — `container_version` is still read from `core.constants`, and
  the interface keeps writing v3 until the layout earns its place.

- The interface names the container layout: the extract tab's audit card shows a
  `FORMAT` row (`v3 · keyed positions` or `v2 · public header · older file`,
  localised), and the embed tab says which layout this build writes. The version
  is authenticated inside the header, so it is reported only after a payload was
  opened; every number comes from `phantom.core.constants`, never a locale file.
- Copied secrets now really leave the system clipboard. Tk's `clipboard_clear`
  only reaches it while Tk owns the selection, and on macOS the text survives the
  window, so the 30 s timer and the window close both call the platform's own tool
  (`pbcopy`, `clip`, `wl-copy`, `xclip`, `xsel`) through an argument vector — never
  a shell — with a 2 s timeout. The clear runs only when the clipboard still holds
  the text PHANTOM put there, so another application's content is never wiped.
- `core.security.secure_zeroize` is the single checked zeroizer behind every key
  wipe: `kdf._zero`, `kdf.wipe_all`, `MessageKeys.wipe()` and therefore both
  envelopes. It zeroes a `bytearray` in place and raises `TypeError` for `str`,
  `bytes` or anything else immutable, so a wipe of a value that cannot be wiped
  fails loudly instead of passing silently. `SECURITY.md` spells out what this
  does not cover: the typed `str`, interpreter copies and swap.
- The audit card's `SIZE` row now reports the share of the carrier the payload
  occupies: `284 B → 306 B · 12 % of the carrier`. It is exact bookkeeping, not a
  gauge — `stego.engine.Capacity.carrier_share` counts the header bits plus eight
  bits per ciphertext byte against the carrier's usable bits, and
  `core.payload.carried_bytes` rebuilds the ciphertext length from an extracted
  payload (blob metadata + name + zlib stream + GCM tag). The heuristic
  `fill_percent` behind the fill bar is left alone, and an unknown carrier prints
  no share at all.
- `tools/bench_steganalysis.py` answers the question the capacity warnings only
  gesture at. Eight generated 512×512 carriers are embedded through the real v3
  engine (LSB matching), written to PNG, read back, and handed to the two classic
  blind detectors — Westfeld–Pfitzmann chi-square and Fridrich's RS. Every carrier
  is measured untouched as well, so a detector that already fires on the cover
  cannot be mistaken for a find, and the run opens with a calibration block on
  synthetic LSB replacement to prove both detectors still work (χ² 0.00 → 1.00,
  RS 0.00 → 0.99 as the replacement rate climbs):

  | load, bpp | PSNR, dB | changed pixels, % | χ² p | RS p̂ | detected |
  |---|---|---|---|---|---|
  | 0.05 | 68.9 | 2.5 | 0.000 | 0.008 | 0/6 |
  | 0.10 | 65.9 | 4.9 | 0.000 | 0.007 | 0/6 |
  | 0.20 | 62.9 | 9.7 | 0.000 | 0.016 | 0/6 |
  | 0.40 | 59.9 | 18.8 | 0.000 | 0.025 | 0/6 |

  LSB matching moves samples by ±1 and keeps the histogram, and the histogram is
  exactly what those detectors look for, so nothing fires at any payload the
  scheme can carry: the ceiling run reaches 2.9 bpp (86 % of the pixels, PSNR
  51 dB) with chi-square still reading 0.000 and RS flagging one carrier out of
  six. Two carriers stay out of the denominator because RS fires on the untouched
  cover, and the report says so instead of hiding it. The distortion columns are
  reproducible, the detector columns wander by ±0.02 between runs because every
  embed draws a fresh nonce. SPA, calibrated RS or SRM is the escalation path if
  a real detector is wanted.
- `tools/bench_positions.py` measures what the v3 bootstrap key costs next to the
  Argon2id run the container already pays for: one extra PBKDF2-HMAC-SHA256
  (600000 iterations) derivation, ~0.11 s on the development machine and
  independent of carrier size (0.1 % of a 12 MP embed, which is dominated by PNG
  I/O).

### Changed

- **v5 (syndrome trellis) is now the layout this build writes.** Embedding from
  the interface, the API or the engine with no `container_version` produces a v5
  file; the embed tab says so before the user presses Save
  (`v5 · синдромный решётчатый код`) and the audit card names the layout of an
  opened file. `C.CONTAINER_VERSION` moved to 5 and a new
  `C.KEYED_CONTAINER_VERSION` (3) names the older keyed layout, which is now the
  *fallback*: audio carriers, images with transparent pixels and payloads no block
  layout fits still fall back to it, and the header then says v3. v2, v3 and v4
  files remain readable — the suite writes one of each and extracts it next to a
  v5 file.

  Making it the default needed the cost map to stop being the bottleneck. The
  embedder now builds costs with `adaptive.embedding_costs`: single precision,
  reusable buffers, no full-plane temporaries, and **one wavelet scale** instead
  of two. A single scale turned out to be not just 2× cheaper but slightly
  better — on the eight benchmark covers at 0.2/0.4 bpp it selects changes with
  PSNR 66.89/63.04 dB and 4.02/9.71 % of pixels touched, against 66.77/62.76 dB and
  4.17/10.48 % for the two-scale map, because the coarse scale mostly steered
  changes into mid-frequency texture where they buy nothing. (The half-resolution
  step described further down takes the same map down by another factor of four.)
  `cost_map` — the float64 map the v4 layout reproduces from the stego image — is
  untouched, so existing v4 files still open.

  Two structural fixes came out of the same work. The trellis body is now walked a
  chunk of blocks at a time (`stc.sliced`, `STC_CHUNK_SLOTS`): a 24 MP carrier has
  72 M usable slots, and materialising the keyed order for all of them cost 576 MB
  of int64 indices plus another 288 MB for the costs — enough to get the process
  OOM-killed at 4 GB. And the Feistel round now assembles its AES input in one
  buffer instead of a zeros/astype/view/tobytes chain, which is bit-identical
  output (checked against the previous implementation on 5 000 and 3 000 000
  indices) for a third less time.

  Measured on a 24 MP carrier with a 100 KiB payload through the public API,
  extraction round-tripped and verified:

  | | before v5 became the default | now |
  |---|---|---|
  | embed (API, includes the read-back check) | did not finish (OOM at 4 GB) | 27.5 s |
  | extract (API) | — | 10.2 s |
  | peak RSS | — | 728 MB |
  | cost map alone | 43.9 s / 2.1 GB | 2.1 s / ~0.7 GB |

- **The trellis cost map is computed at half resolution.** A cost map is a smooth
  field — it says "this region is busy", not "this pixel is" — so `embedding_costs`
  now averages every channel 2×2 down, runs the wavelet chain on the quarter-size
  plane and repeats each cost back over its 2×2 block. Together with the single
  detail scale that is 16× less arithmetic than the original two-scale float64 map:
  on a 24 MP photo the map itself takes **2.1 s** instead of 43.9 s, and the whole
  API embed drops from 61 s to **27.5 s** with a peak of 728 MB instead of 984 MB.

  It costs a quarter of a decibel and nothing else that the benchmark can see: on
  the eight covers at 0.2/0.4 bpp the full-resolution map selects changes with PSNR
  66.89/63.04 dB and 4.03/9.69 % of pixels touched, the half-resolution one
  66.64/62.86 dB and 4.36/10.18 % (a two-scale map at the same resolution is
  slightly worse and twice as expensive: 66.59/62.76 dB). Across the full
  three-layout benchmark the v5 columns barely move — PSNR within 0.02 dB of the
  previous run at every load, `Σρ/bit` unchanged at 0.17–0.22 — while the run itself
  drops from 229.1 s to 142.7 s. The ranking agrees with the slow map, too:
  correlation 0.977, and the cheapest fifth of the fast map sits at the 20th
  percentile of the full-resolution one. There is no size threshold; carriers under
  `COST_MIN_SIZE` (8 px) keep the exact map because a 2×2 average would leave the
  wavelet nothing to look at.

- `tools/bench_steganalysis.py` and `tools/bench_positions.py` name the layouts
  explicitly (`v3` is the keyed layout, not "the current one"), the steganalysis
  header lists all three with the default marked, and the positions benchmark
  gained a v5 row.

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
