# PHANTOM 2.1.0

Hide an encrypted payload inside a PNG image or a WAV/FLAC audio file, then
extract it on another machine with the same password. The payload is encrypted
before a single bit is written into the carrier, and the app never claims more
than the code actually does.

- Encryption: **AES-256-GCM**, fresh salt and nonce for every operation
- Key derivation: **Argon2id · m=96 MiB · t=3 · p=1** (PBKDF2-HMAC-SHA256
  with 600000 iterations is a separate profile, chosen explicitly at embedding
  time; it is never picked automatically when Argon2id is missing — that would
  be a silent downgrade)
- Container: versioned, authenticated, no magic string in clear text
- UI: CustomTkinter, three languages (Українська / Русский / English),
  Dark / Light / System themes, drag & drop, four tabs
  (`СКРЫТЬ` · `ИЗВЛЕЧЬ` · `МЕТАДАННЫЕ` · `НАСТРОЙКИ`)
- Metadata inspector and privacy cleaner for PNG, WAV and FLAC: what the file
  carries, what can be removed, and a verified cleaned copy
- No network calls, no telemetry, no cloud

`phantom.__version__ == "2.1.0"`, stage: release candidate.

## What it is, and what it is not

This is a **file container with a steganographic shell**. The shell survives
copying, renaming and re-saving the file with a lossless editor. It does **not**
survive anything that re-encodes the media: JPEG export, messenger "photo"
mode, audio transcoding, resizing, cropping, or a screenshot. In messengers
always send the file **as a document**.

LSB steganography is detectable by statistical steganalysis. The app never
claims that a payload cannot be found, because that would be false. What the
format does do is put nothing at a known place: since v3 the positions of every
bit in the carrier — the sealed header included — are derived from the password,
so without it there is no region to read, and a wrong password is
indistinguishable from a carrier with no payload. Carriers written in the v2
layout, whose header sat at constant-seeded positions, are still read. The
*presence* of steganography is a separate question, and statistical analysis can
answer it.

Since **v5** this build writes the payload with a syndrome-trellis code by
default. A Daubechies wavelet cost map measures where the image is busy, and the
trellis then solves for the cheapest *combination* of ±1 changes that carries the
payload, instead of spreading the bits evenly. At the same payload that needs
about half the changes of the v3 keyed layout, spends 2–3× less distortion and
gains 3–4 dB of PSNR, and the RS estimator that greedy placement (v4) pushed to
0.098 falls back to the v3 level (see the CHANGELOG for the three-way table). The
cost map is built on a half-resolution copy of each channel — a cost map describes
regions, not pixels — which buys the same placement for a quarter of the
arithmetic and costs 0.25 dB (66.64 vs 66.89 dB at 0.2 bpp; it is a trade-off, not
a free lunch, and `SECURITY.md` states it as one). Long operations report what they
are doing: the footer names the phase, spins while the cost map runs and counts the
seconds, so a 24 MP photo that takes half a minute does not look like a hang.

Everything older still opens: the audit card names **v2** (old public header),
**v3** (keyed positions), **v4** (greedy adaptive, reachable with
`container_version=4`) or **v5** (syndrome trellis, the default). The trellis
falls back to v3 whenever the carrier cannot support it — audio, images with
transparent pixels, payloads that no block layout fits — and the header then says
v3, so the file is plain v3.

Read [`SECURITY.md`](SECURITY.md) for the threat model, and the in-app
"Threat model" block for the same text in your language.

## Requirements

- Python 3.10+ (developed against 3.11)
- Tk 8.6 available for the GUI (bundled with python.org builds on Windows and
  macOS; on Linux install `python3-tk`)

## Install

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python app.py                   # from the repository root
python -m phantom              # after `pip install -e .`
```

The launcher puts `src/` on `sys.path`; no installation step is required to run
from a checkout.

## Usage

1. Pick a carrier. PNG (1/2/4/8-bit, non-animated) for images; WAV (8/16/24/32-bit)
   or FLAC (16/24-bit) for audio. Lossy formats — MP3, AAC, M4A, OGG, OPUS,
   WMA — are refused up front, because transcoding destroys the payload.
2. Read the capacity line. It is `(usable_bits - 400) / 8 - 16` bytes: 400 bits
   for the sealed header, 16 bytes reserved for the GCM tag. "Usable bits" is
   one bit per colour channel of every fully opaque pixel, or one bit per
   sample channel of the audio stream.
3. Type the secret or choose a file. Text is compressed, files keep their name;
   both are hashed with SHA-256 and authenticated.
4. Enter a password of at least 12 characters. Identical-looking Unicode
   passwords are folded with NFKC, so a password typed on another keyboard
   layout still opens the file. A generated 20-character password is offered.
5. Save. The app writes to a temporary file, re-reads it, extracts the payload
   back and compares the SHA-256 **before** the finished file replaces the
   target. If verification fails, nothing is published.
6. To extract, open the carrier, enter the password, and save the revealed text
   or file. Output file names are sanitised and the extension is forced. The
   audit card then names the layout the file was written with — v5 (syndrome
   trellis, what this build writes), v4 (adaptive), v3 (keyed positions) or v2
   (older public header) — together with the cipher, KDF profile, integrity check
   and sizes. The size
   row also says how much of the carrier the payload takes, counted in bits the
   container really wrote: `284 B → 306 B · 12 % of the carrier`. The embed tab
   states the layout this build writes before you press Save.

### Editing, scrolling and reading results

Text fields behave like native ones on every platform:

- `Cmd+C/V/X/A` and `Cmd+Z` / `Cmd+Shift+Z` on macOS, `Ctrl+C/V/X/A`,
  `Ctrl+Z` / `Ctrl+Y` / `Ctrl+Shift+Z` elsewhere, plus a right-click
  (two-finger tap) menu with Undo, Redo, Cut, Copy, Paste, Select all and — in the
  payload field — Clear field. Every shortcut is bound on the inner Tk widget, so
  CustomTkinter's proxies cannot swallow it, and every command is one handler
  shared with the menu and with Tk's virtual events, so nothing runs twice.
- **An action bar under the payload field**: paste, copy, cut, select all, undo,
  redo, copy all and clear field. Each button calls the same handler as the
  hotkey and the context-menu entry, so a button and `Cmd+C` cannot disagree, and
  availability follows the editor (copy needs a selection, copy all needs text,
  everything is disabled while a job runs or in file mode).
- The payload field takes text of any size before a carrier is chosen, keeps it
  when the carrier changes, and never splits, trims or drops it. One line longer
  than `EDITOR_WRAP_LINE_LIMIT` (20 000 characters) turns word wrap off — a
  display decision, said in a hint under the counters — because Tk's wrap costs
  seconds per 100 000 characters on a single line while costing nothing on text
  that has line breaks.
- `ОЧИСТИТЬ ПОЛЕ` next to the counters empties the field and only the field:
  password, carrier, output path and settings stay. `Cmd+Z` puts the text back.
- The capacity card answers "how much fits" with the engine's own numbers:
  container (name, format, size on disk, resolution or duration), payload
  (characters, UTF-8 bytes, prepared size), capacity (available, occupied, free,
  percent, excess) on one scale, with explicit states for no carrier, no text,
  calculating, fits, does not fit and calculation error. The estimate is
  preliminary by construction — the engine's capacity check at embed time is
  final — and embedding is disabled while the numbers are unknown or too large.
- `PASTE` and `COPY KEY` buttons next to both password fields, for systems where
  the shortcut never reaches the application.
- The mouse wheel and the trackpad scroll the whole tab, not just the scrollbar,
  and they scroll the tab **under the pointer**. One application-level router
  resolves the target from the pointer position at event time (`winfo
  containing`), so the event no longer has to be bound on every widget and a tab
  that is not on screen cannot be scrolled through. The two event kinds are
  handled separately: `<TouchpadScroll>` (Tk 9, macOS and Windows) carries the two
  pixel deltas packed into `%D` and is decoded with `tk::PreciseScrollDeltas`,
  while `<MouseWheel>` is a notch count whose scale is read from the running Tk
  (Aqua 8.6 reports ±1 per notch, Aqua 9 and Windows ±120, X11 is one unit).

  A text box keeps the wheel only while it can still move the way the gesture
  asks: an empty box, one whose whole content is visible, or one already at its
  last line hands the same event to the page underneath, so scrolling never stops
  under a text field. One gesture is answered once — the router is bound on the
  text widgets as well as globally and ignores an event it has already handled —
  and the pointer over a scrollbar, a list, a scale or a hidden tab is left alone.
  Set `PHANTOM_SCROLL_DEBUG=1` (or a file path) to log every scroll event: type,
  Tk version, backend, raw and decoded deltas, the widget under the pointer, the
  chosen area, and the `yview` before and after.
- The clipboard is treated as public. A copied password, key or result is cleared
  after 30 s and again when the window closes, with the platform's own tool
  (`pbcopy`, `clip`, `wl-copy`, `xclip`, `xsel`) instead of Tk alone, because Tk
  only reaches the system clipboard while it owns it. If another application has
  replaced the content in the meantime, it is left alone.
- **The result card copies and exports the whole payload, not the preview.**
  `SELECT ALL`, `COPY SELECTION`, `COPY ALL` and `SAVE AS TXT (UTF-8)` sit next to
  the result; the pane paints at most `RESULT_PREVIEW_CHARS` (200 000)
  characters and says how much it is showing, because a multi-megabyte insert is
  a multi-second display pass on Aqua. Copy-all and the TXT export work from the
  decrypted payload: the file is written verbatim and verified against its
  SHA-256 — nothing is normalised, no character is replaced, no line ending is
  rewritten. A payload that is not valid UTF-8 is reported instead of being
  "repaired".
- `EXPAND` opens the result in a window at 80 % of the screen with the text
  selectable and read-only, plus `COPY TEXT`, `SELECT ALL`, `SAVE AS TXT`,
  `COPY PATH`, `SHOW IN FINDER` (macOS `open -R`, Windows `explorer /select,`,
  Linux `xdg-open`) and `CLOSE`; it holds the complete text even when its own
  pane shows a preview.

### Window size, start-up and language

Nothing in the window is sized by a constant that was guessed for one screen. A
button is measured in its own font, in the current language, and given that width
(with an explicit `width=` acting as a minimum, never as a cap); a row of buttons
flows onto a second line instead of running off the card; a paragraph's
`wraplength` follows the card it sits in. The supported sizes — 980×720 (the
minimum), 1140×900, 1280×800, 1440×900 and maximised — all show every caption in
`en`, `ru` and `uk`, and the GUI suite asserts it by measuring each caption
against its widget after a resize and after a language switch.

Start-up is measured, not guessed: `python tools/bench_startup.py` builds the whole
window three times and reports the median build time, the first frame, how often the
system font list was read and how many font objects were created (`--stages` and
`--profile` say where the time goes); `PHANTOM_STARTUP_DEBUG=1` prints one line per
stage (import, Tk root, each tab, the render passes, the first frame, the moment
the window is on screen) plus a summary, and the window does the work that is not
needed for the first frame (logging, the OS-theme watcher) after the first idle
callback. The font list of the system is read once per window instead of once per
widget, and the saved geometry is applied before the cards are built, so the
window is laid out once at its real size instead of twice.

Capacity warnings: above 70 % of the carrier the bit plane becomes
statistically noticeable; above 90 % the app warns that the change is large.
Both thresholds are constants, not opinions, and they are the cautious kind:
`tools/bench_steganalysis.py` measures what they mean in practice. On eight
generated 512×512 carriers the classic chi-square and RS attacks do not fire at
any payload PHANTOM can carry — LSB matching moves samples by ±1 and leaves the
histogram intact, which is exactly what those detectors look for. The `SIZE` row
uses the same bookkeeping as that table: header bits plus eight bits per
ciphertext byte, against the carrier's usable bits.

### Metadata: what a file carries, and what to do about it

The fourth tab (`МЕТАДАННЫЕ`) reads the metadata of a PNG, WAV or FLAC file and
offers a **selective** clean-up. All of it is local: no cloud, no API, no
telemetry, and nothing is uploaded anywhere.

Detection is by content, not by extension: a JPEG renamed to `.png` is refused
rather than parsed by the wrong reader. Every field is listed with its name,
value, container and one of six categories — personal, descriptive, technical,
rights/attribution, provenance/signatures, unknown — and one of three markers:
*editable* (the cleaner may remove it), *protected* (it stays) or *derived* (a value
computed from other fields: the decimal GPS position, the properties of an XMP
packet). A derived row is there so you can read the file at a glance; it is not an
entry of its own, it is not counted twice, and removing the fields behind it removes
it.

- **Selected by default** are the personal fields the cleaner can remove: GPS
  position, device make/model, serial numbers, personal dates and comments.
- **Offered but not preselected** are attribution fields (EXIF `Artist`, PNG
  `Author`, RIFF `IART`, Vorbis `ARTIST`): a name is personal data, but deleting
  a credit is your decision, so the checkbox is unticked.
- **Removed as a whole block or not at all**: an XMP packet, an iXML chunk, an
  embedded picture. If the block also carries something the policy protects — a
  title, a rights line — it is reported as *protected*, with the reason, instead of
  being offered and then failing halfway.
- **XMP is read, not searched.** The packet is parsed as RDF/XML, so a property
  written as an attribute of `rdf:Description`, inside a `Bag`/`Seq`/`Alt`, in a
  nested node, under a prefix the file chose, or in a namespace this build has
  never seen counts as a property. A packet whose contents cannot be read
  completely — truncated XML, a DTD or an entity declaration, more elements or
  nesting than the limits allow, more than a megabyte — is protected rather than
  guessed about, and several packets in one file are judged together. The
  properties are listed one by one, so "kept" never means "not shown".
- **Never removed** are copyright and licence lines, technical parameters, colour
  profiles, provenance blocks and *unknown* chunks. An unknown chunk is reported
  as unknown and left alone: the cleaner does not delete what it cannot explain.
- **A signed file is refused.** When a C2PA/JUMBF manifest or a signature block is
  present, the tab says so and blocks the clean: rewriting the container would
  break the manifest's hard binding, and a "cleaned" file that still claims a
  signature it no longer satisfies is worse than doing nothing. PHANTOM never
  forges, hides or strips a signature to make a check pass.

Before anything is written you get a pre-report: what will be removed, what will
be kept, and what you asked for but is protected. The cleaned copy is always a
**new file** — the source is never modified, and a path that resolves to the
source is refused.

The destination is resolved *after* the content format is known: `photo.wav` for
a PNG becomes `photo.png`, and the source and existence checks apply to that final
name, through symlinks and hard links as well. A destination that already exists
is refused (`output_exists`) unless overwriting that very name was requested; an
extension-rewritten name is never replaced, because the file it would hit was
never shown to the user. In PHANTOM Desktop the save dialog is reopened with the
resolved path, so what the dialog confirms is exactly what is written.

Publishing is atomic and conditional: the copy is written to a staged sibling,
verified, and only then published in one step that cannot replace an existing
file (`link(2)` where hard links exist; otherwise `renameat2(RENAME_NOREPLACE)`
on Linux, `renamex_np(RENAME_EXCL)` on macOS, `rename` on Windows). If two
cleans race for the same free name, exactly one publishes and the other reports
`output_exists`; on any failure the staged file is discarded and the
destination is untouched. A folder that cannot publish a new file atomically is
refused with `publish_unavailable` and a request to choose another folder —
there is no copy into the destination, so a partial file never gets the final
name.

Verification after cleaning is mandatory, not advisory:

- PNG: every pixel (RGB/RGBA, alpha included), the dimensions and the chunk CRCs;
- WAV/FLAC: every PCM sample, bit depth, sample rate, channel count, frame count
  and duration, and a full re-decode;
- the metadata is re-read to prove the selected fields are gone and the protected
  ones are still there;
- SHA-256 of the input and of the output, plus a separate SHA-256 of the *decoded
  content*.

The two hashes answer two different questions, and the integrity card says so:
the **whole-file hash always changes**, because the metadata bytes changed; the
**decoded-content hash must not change**, because the pixels and the samples are
what must survive. If the decoded content differs, nothing is published.

How the tab reads, top to bottom: the file (with **reset file and results**), what
it carries (a field list with one field per two lines — caption above, wrapped
value below — and protected rows dimmed with the reason they stay), the **plan of
the next operation**, where to save, and the **result of the previous operation**.
The plan and the result are separate cards, so a stale result can never look like
a fresh plan. The save button is disabled while nothing is selected or while the
clean is blocked, with the reason under it; when a file carries nothing personal
the tab says «Nothing to clean» and explains which other fields are still
removable; the verdict of the last check is coloured, shows the field count before
and after, and the full SHA-256 values can be copied, not only the 16-character
teaser. A content credential is reported as *manifest detected* or *manifest
detected, signature not verified* — never as confirmed authenticity.

The report can be copied or exported as TXT or JSON — that export contains the
personal values you were about to remove, which is why it only happens when you
ask for it. The TXT report and every status line follow the language of the window
(`en`/`ru`/`uk`, including the disclaimer); the JSON export stays English and
versioned, because a machine reads it. Diagnostics log counts, never values.

What this tab does **not** do: it does not validate a C2PA signature (there is no
trust list and no certificate chain in this build), it does not remove data hidden
inside pixels or audio beyond the container's metadata system, and a file with no
detected metadata is not proof that no digital trace exists. Metadata removal is
not anonymisation.

## Limits

| Limit | Value |
| --- | --- |
| Maximum plaintext payload | 1 GiB |
| Maximum file name | 8192 bytes |
| Password | minimum 12 characters, generated 20, NFKC-normalised |
| Clipboard auto-clear | 30 seconds |
| Image carriers | PNG only, 1/2/4/8-bit, colour types 0/2/3/4/6, no APNG, no 16-bit |
| Audio carriers | WAV 8/16/24/32-bit, FLAC 16/24-bit |
| Lossy audio | refused (`.mp3 .aac .m4a .ogg .oga .opus .wma .mp2`) |

16-bit PNGs are refused rather than silently clipped; 1/2/4-bit images are
expanded to 8-bit exactly (no loss) and accepted. Palette and bitmap images are
converted with a notice; transparency is preserved and fully transparent pixels
are never used as carrier bits.

## Legacy version 1 files

Files produced by PHANTOM 1.x still open: v1 wrote a bare payload with
sequential LSB and PBKDF2-HMAC-SHA256 at 200000 iterations. That path is kept
as a read-only legacy branch (`phantom.stego.legacy`), tested against golden
fixtures generated from the original commit — not reimplemented from memory.
New files are always written in the v2 format.

## Wiping

"Shred" overwrites the file once (`WIPE_PASSES = 1`, 1 MiB chunks, `fsync`
after every chunk) and then deletes it. On SSDs, copy-on-write filesystems
(APFS, Btrfs, ZFS) and journaling filesystems this is **risk reduction, not a
guarantee**: the storage controller may keep older copies. The app says so in
the confirmation dialog; it does not call the result "secure erase".

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -ra                 # full suite
python -m pytest -m "not slow"       # skip the 24 MP benchmarks
xvfb-run -a python -m pytest -m gui  # GUI smoke test on a headless Linux box
python -m pytest --cov=phantom --cov-branch --cov-report=term-missing
ruff check src tests tools
mypy src
```

Current numbers on Linux/Python 3.11: 844 headless tests plus 8 GUI smoke tests
under Xvfb, and 98% line + branch coverage across `core`, `stego`, `audio` and
`i18n`. The Tk-only modules are exercised by the `gui` job, so a headless-only
run reports them as 0% — that is expected, not missing coverage.

The test suite is a safety net, not decoration:

- `tests/test_no_stubs.py` scans every module for placeholders, silent
  `except: pass`, and banned claim words;
- known-answer tests pin Argon2id, PBKDF2 and AES-GCM against published vectors;
- golden legacy fixtures are generated from the original v1 code by
  `tools/make_legacy_fixtures.py` and `tools/make_legacy_unicode_fixture.py`;
- audio tests verify WAV/FLAC through a second decoder and check that only the
  data chunk changes;
- security-claim tests assert that every number shown in the UI and quoted in
  this README comes from `phantom.core.constants`.

## Layout

```
app.py                     launcher
src/phantom/core/          crypto, container, payload, files, API
src/phantom/stego/         bits, permutation, image carriers, legacy v1 reader
src/phantom/audio/         WAV/FLAC containers, capacity, probing
src/phantom/metadata/      inspector, cleaner, integrity checks, reports
src/phantom/ui/            CustomTkinter app, worker, view model, theme
src/phantom/locales/       uk / ru / en JSON, 302 keys with parity
tests/                     safety net, fixtures, property tests
tools/                     fixture generators, benchmarks, steganalysis
```

## License

MIT — see [`LICENSE`](LICENSE). You may use, modify and redistribute the code,
including commercially, provided the copyright notice and permission notice are
kept. The software is provided "as is", without warranty of any kind.
