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

- `Cmd+C/V/X/A` on macOS, `Ctrl+C/V/X/A` elsewhere, plus a right-click
  (two-finger tap) menu with Paste, Copy, Cut and Select all. Every shortcut is
  bound on the inner Tk widget, so CustomTkinter's proxies cannot swallow it.
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
  (Aqua 8.6 reports ±1 per notch, Aqua 9 and Windows ±120, X11 is one unit). A text
  box keeps the wheel while it can still scroll itself. Set
  `PHANTOM_SCROLL_DEBUG=1` (or a file path) to log every scroll event: type, Tk
  version, backend, raw and decoded deltas, the widget under the pointer, the
  chosen area, and the `yview` before and after.
- The clipboard is treated as public. A copied password, key or result is cleared
  after 30 s and again when the window closes, with the platform's own tool
  (`pbcopy`, `clip`, `wl-copy`, `xclip`, `xsel`) instead of Tk alone, because Tk
  only reaches the system clipboard while it owns it. If another application has
  replaced the content in the meantime, it is left alone.
- `EXPAND` opens the result in a window at 80 % of the screen with the text
  selectable and read-only, plus `COPY TEXT`, `COPY PATH`, `SHOW IN FINDER`
  (macOS `open -R`, Windows `explorer /select,`, Linux `xdg-open`) and `CLOSE`.

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
source is refused. Publishing is atomic: the copy is written to a staged sibling,
verified, and only then renamed onto the destination; on any failure the staged
file is discarded and the destination is untouched.

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

The report can be copied or exported as TXT or JSON — that export contains the
personal values you were about to remove, which is why it only happens when you
ask for it. Diagnostics log counts, never values.

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
