# PHANTOM Desktop (Tauri 2) — first stage

The desktop prototype of PHANTOM: a Tauri 2 window (React 18 + TypeScript) for
macOS, Windows and Linux, wrapping the existing Python core **unchanged** —
same algorithms, same container format (v5 syndrome trellis by default, v2/v3/v4
readable), same AES-256-GCM / Argon2id primitives, same `phantom.core.api`
calls. This folder is deliberately separate from the CustomTkinter app and
from the SwiftUI experiments.

Stage one ships exactly one finished scenario, end to end: **hide a PDF inside
a PNG — and extract the same PDF back, byte for byte**. Everything else
arrives after this path is verified on real machines.

## Layout

```
desktop/
├── src/                    React 18 + TS frontend (Vite)
│   ├── screens/            HideScreen · ExtractScreen · SettingsScreen
│   ├── bridge.ts           UI ↔ sidecar glue (+ a clearly-labelled browser demo)
│   ├── i18n-dict.ts        Українська · English · Русский (key parity enforced by tests)
│   └── …                   DropZone, password field with strength meter, …
├── src-tauri/              Rust shell: window, system dialogs, sidecar spawn
│   ├── src/main.rs         bridge_call / pick_file / save_file / reveal_path / app_info
│   └── tauri.conf.json     window 980×720, CSP, externalBin = phantom-bridge
├── phantom-bridge.spec     PyInstaller spec for the sidecar
├── phantom-bridge-entry.py entry point for the frozen build
├── e2e/full_cycle.py       stage-scenario check: hide PDF → extract → SHA-256 compare
├── e2e/screenshots.mjs     CI screenshots of the web bundle (980×720)
└── e2e/layout_check.mjs    objective 980×720 check: primary CTA fully visible,
                            carrier hint before selection, capacity verdict only after
```

The Python side of the contract lives in `src/phantom/bridge.py` — a stdio
JSON-lines protocol, one request per line, one response per line.

### Security rules (enforced in code)

- Passwords and document contents travel **only on stdin** of the sidecar —
  never in `argv`, never in logs, never in the Tauri IPC as command-line text.
  `phantom-bridge` ignores `argv` entirely.
- The sidecar writes one short line per command to stderr (`cmd`, `ok`, error
  code). `PHANTOM_BRIDGE_DEBUG=1` adds a traceback — still never on stdout.
- File payloads are extracted to the user-chosen path with `0600` permissions,
  never via a temp file in the clear.
- The UI wipes the password from component state after the hide/save job
  completes; it is never persisted anywhere.  Screens stay mounted (they are
  only hidden), so anything typed or picked survives a trip to Settings or a
  language switch — that is the draft, held **in memory only**.  On disk the
  only persisted entry is `phantom.settings.draft` (language and theme —
  nothing else); passwords and document contents are never written to
  `localStorage`, logs or files by the shell.

## Prerequisites

- Python 3.11+ and `pip install -r requirements.txt pyinstaller` (repo root)
- Node.js 22+
- Rust stable (only for building the shell; not for frontend work)
- Linux only: `libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev libsoup-3.0-dev
  libayatana-appindicator3-dev` (or the equivalent for your distro)
- macOS: Xcode CLT · Windows: VS 2022 Build Tools / MSVC (as shipped on GitHub runners)

## Run the frontend in a browser (demo mode)

```bash
npm ci
npm run dev        # http://localhost:1420
```

In a plain browser the app is a **demo**: a banner says operations are
simulated, file pickers use `<input type=file>`, and no bytes are encrypted.
Inside Tauri everything is real.

## Run the real app

```bash
# repo root — freeze the sidecar
pyinstaller --noconfirm desktop/phantom-bridge.spec
mkdir -p desktop/src-tauri/binaries
cp dist/phantom-bridge desktop/src-tauri/binaries/phantom-bridge-$(rustc -vV | awk '/^host:/{print $2}')   # + .exe on Windows

cd desktop
npm ci
npx tauri dev      # needs cargo; or: npx tauri build
```

## Verify the stage scenario

```bash
python desktop/e2e/full_cycle.py --bridge dist/phantom-bridge --out cycle.json
```

Expected: every step `PASS`, including
`extracted PDF is byte-identical (SHA-256)`.

## Verify the frontend behavior

```bash
cd desktop
npm run build      # tsc --noEmit + production build
npm test           # vitest: i18n parity + carrier-hint gate + in-memory draft scenarios
node e2e/layout_check.mjs --base http://127.0.0.1:4173 [--out screenshots]
```

`layout_check.mjs` drives the built bundle in Chromium at exactly 980×720,
in file and text mode, in light and dark themes, and fails unless the
primary button is fully on-screen in every case.  It also checks the carrier
gate: «Выберите картинку или аудио» before any carrier is chosen (no capacity
verdict yet), the capacity bar only after one is probed.

`App.test.tsx` covers the draft contract: type text / pick a PDF → open
Settings → switch language → come back → same data; and the persistent draft
is checked key-by-key — language, theme, savedAt, nothing else.

## CI

`.github/workflows/desktop.yml` — see the PR description for per-OS results.
Every OS job freezes the bridge, builds the installer, runs `full_cycle.py`
**twice** (fresh bridge, then the bridge shipped inside the built package) and
uploads its artifacts.  The macOS artifact (`macos-arm64-packages`)
also carries `verification-report.txt` (every `codesign`/`spctl` invocation,
before and after signing, plus the mounted-DMG gate output) and
`PHANTOM-aarch64.dmg.sha256`.  The screenshots job also runs
`layout_check.mjs`; if the primary button does not fit at 980×720 anywhere,
that job fails the build — screenshots are capture, this one is a gate.

## Known limits of this stage

- No wiping of the source file after embedding (the Python core supports it;
  the shell does not expose it yet).
- The Extract screen shows text payloads inline; file payloads require a save
  location chosen in the native dialog.
- macOS builds are **ad-hoc signed only** (nested `phantom-bridge` first,
  then the `.app`, then the `.dmg` is packed); `codesign --verify --deep
  --strict` is enforced in CI.  There is no Developer ID signature and no
  notarization — Gatekeeper will warn or block; Developer ID + notarization
  are explicitly out of scope until a certificate exists.
- Windows builds are not Authenticode-signed.
- The demo carrier for "Попробовать на примере" is 300×300 RGB — plenty for
  the bundled 809-byte example PDF, not for large attachments.
