# PHANTOM 2.0.0

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
  Dark / Light / System themes, drag & drop
- No network calls, no telemetry, no cloud

`phantom.__version__ == "2.0.0"`, stage: release candidate.

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
   audit card then names the layout the file was written with — v3 (positions
   derived from the password) or v2 (older public header, still readable) —
   together with the cipher, KDF profile, integrity check and sizes. The embed
   tab states the layout this build writes before you press Save.

### Editing, scrolling and reading results

Text fields behave like native ones on every platform:

- `Cmd+C/V/X/A` on macOS, `Ctrl+C/V/X/A` elsewhere, plus a right-click
  (two-finger tap) menu with Paste, Copy, Cut and Select all. Every shortcut is
  bound on the inner Tk widget, so CustomTkinter's proxies cannot swallow it.
- `PASTE` and `COPY KEY` buttons next to both password fields, for systems where
  the shortcut never reaches the application.
- The mouse wheel and the trackpad scroll the whole tab, not just the scrollbar:
  the event is routed from any child widget to the enclosing scrollable frame
  (macOS deltas are inverted and never rounded to zero). A text box keeps the
  wheel while it can still scroll itself.
- `EXPAND` opens the result in a window at 80 % of the screen with the text
  selectable and read-only, plus `COPY TEXT`, `COPY PATH`, `SHOW IN FINDER`
  (macOS `open -R`, Windows `explorer /select,`, Linux `xdg-open`) and `CLOSE`.

Capacity warnings: above 70 % of the carrier the bit plane becomes
statistically noticeable; above 90 % the app warns that the change is large.
Both thresholds are constants, not opinions.

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
src/phantom/ui/            CustomTkinter app, worker, view model, theme
src/phantom/locales/       uk / ru / en JSON, 205 keys with parity
tests/                     safety net, fixtures, property tests
tools/                     fixture generators
```

## License

MIT — see [`LICENSE`](LICENSE). You may use, modify and redistribute the code,
including commercially, provided the copyright notice and permission notice are
kept. The software is provided "as is", without warranty of any kind.
