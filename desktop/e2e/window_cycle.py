#!/usr/bin/env python3
"""Window E2E: the Metadata → Hide → Extract scenario through the installed app.

Drives the *installed* PHANTOM Desktop window (Tauri WebDriver on Linux/Xvfb)
through the exact user scenario from the brief:

1. open a test PNG with GPS in the Metadata tab, save the cleaned copy;
2. use the cleaned copy as the carrier in the Hide tab, hide a test file;
3. open the hidden PNG in the Extract tab (via the Hide success handoff),
   extract the file and save it.

Then verifies automatically, outside the app:

* GPS is gone from the cleaned copy (and orientation survives, pixels match);
* the original PNG is byte-identical (SHA-256 unchanged);
* the extracted file matches the test original byte-for-byte (SHA-256).

Platform: Linux (Ubuntu 24.04) under Xvfb — the one platform where CI can
stably control the installed window.  macOS has no WKWebView WebDriver
(Apple ships no driver), and Windows needs an Edge Driver version-matched to
the runner's Edge; both are documented blockers for a window test, not
failures of this scenario.  See ``desktop/README.md`` ("Window E2E").

Native file dialogs cannot be driven by WebDriver, and ``rfd`` on Linux uses
the XDG Desktop Portal which is absent in a headless runner (the dialog
silently cancels).  The shell therefore has a test-only seam: with
``PHANTOM_E2E=1`` the pick/save commands return the next path from a queue
file instead of opening a dialog (see ``src-tauri/src/main.rs``).  The window,
the React UI and the bundled bridge are all real; only the OS selection step
— which lives outside the app window — is queued.

Usage (CI, after the .deb is installed)::

    xvfb-run -a -s "-screen 0 1280x1024x24" \\
        python desktop/e2e/window_cycle.py --app /usr/bin/phantom \\
            --out window-cycle.json --shots window-e2e-shots

Exit codes: 0 = every check passed, 1 = a check failed,
2 = blocker (missing tauri-driver / WebKitWebDriver / app binary, or the
WebDriver session could not start).  A blocker is reported in the JSON report
with its cause; it is not a scenario failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_PASSWORD = "window-e2e-password-7f3a9c1e"  # test container, not a real secret
WEBDRIVER_HOST = "127.0.0.1"
WEBDRIVER_PORT = 4444
WEBDRIVER_URL = f"http://{WEBDRIVER_HOST}:{WEBDRIVER_PORT}"

RESULTS: list[dict[str, object]] = []
BLOCKER: str | None = None


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(condition), "detail": detail})
    verdict = "PASS" if condition else "FAIL"
    suffix = f"  {detail}" if detail else ""
    print(f"{verdict}  {name}{suffix}", flush=True)
    return bool(condition)


def blocker(message: str) -> int:
    global BLOCKER
    BLOCKER = message
    print(f"BLOCKER  {message}", flush=True)
    return 2


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- fixtures
def texture(width: int = 640, height: int = 480, *, seed: int = 11) -> np.ndarray:
    """Deterministic RGB texture, large enough to carry a small secret."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    base = (np.sin(xx / 5.0) * 40 + np.cos(yy / 7.0) * 25 + 128).astype(np.int16)
    channels = [
        np.clip(base + rng.integers(-6, 7, base.shape), 0, 255),
        np.clip(base + 18 + rng.integers(-6, 7, base.shape), 0, 255),
        np.clip(base - 14 + rng.integers(-6, 7, base.shape), 0, 255),
    ]
    return np.stack(channels, axis=-1).astype(np.uint8)


def png_with_gps(path: Path, *, seed: int = 11) -> Path:
    """A PNG with a camera dossier: GPS, orientation, author, comment, copyright."""
    exif = Image.Exif()
    exif[0x010F] = "PhantomCam"
    exif[0x0110] = "PhantomCam One"
    exif[0x0112] = 6  # Orientation: rotate 90 CW (technical, must survive)
    exif[0x0131] = "phantom-software 1.0"
    exif[0x013B] = "Ada Lovelace"
    exif[0x0132] = "2024:05:06 07:08:09"
    exif[0x8298] = "(c) 2024 Ada Lovelace"
    exif[0xA431] = "SN-0001-42"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (Fraction(50), Fraction(27), Fraction(0))
    gps[3] = "E"
    gps[4] = (Fraction(30), Fraction(31), Fraction(0))
    info = PngInfo()
    info.add_text("Comment", "shot at home, kitchen wifi")
    info.add_text("Title", "Holiday")
    info.add_text("Copyright", "(c) 2024 Ada Lovelace")
    with Image.fromarray(texture(seed=seed), "RGB") as image:
        image.save(path, format="PNG", pnginfo=info, exif=exif, compress_level=1)
    return path


def secret_bytes() -> bytes:
    """Deterministic test payload (~550 bytes, fits any reasonable carrier)."""
    header = b"PHANTOM window E2E secret\n"
    body = bytes(range(256)) * 2
    return header + body


# --------------------------------------------------------------------------- helpers
def wait_port(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def find_app_binary(explicit: str | None) -> Path | None:
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None
    for name in ("phantom", "PHANTOM", "phantom-desktop"):
        for directory in ("/usr/bin", "/usr/local/bin"):
            candidate = Path(directory) / name
            if candidate.is_file():
                return candidate
    return None


def find_tauri_driver() -> Path | None:
    found = shutil.which("tauri-driver")
    if found:
        return Path(found)
    cargo_bin = Path.home() / ".cargo" / "bin" / "tauri-driver"
    return cargo_bin if cargo_bin.is_file() else None


# --------------------------------------------------------------------------- scenario
def drive_window(driver, shots: Path, cleaned_name: str) -> None:
    """Click through the installed window.  Raises on timeout (test failure)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, 60)

    def shot(name: str) -> None:
        try:
            driver.save_screenshot(str(shots / f"{name}.png"))
        except Exception as error:  # noqa: BLE001 - a screenshot must never fail the test
            print(f"  screenshot {name} skipped: {error}", flush=True)

    def clickable_testid(testid: str):
        return wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, f"[data-testid='{testid}']")))

    def click_testid(testid: str) -> None:
        clickable_testid(testid).click()

    def click_nav(tab: str) -> None:
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, f"[data-tab='{tab}']"))).click()

    # --- first launch: language picker --------------------------------------
    try:
        first_run = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='firstrun-en']"))
        )
        first_run.click()
        print("  first-run: chose English", flush=True)
    except Exception:  # noqa: BLE001 - already past first launch
        print("  first-run: no picker (already configured)", flush=True)
    shot("01-after-firstrun")

    # --- Metadata: open the GPS PNG -----------------------------------------
    click_nav("metadata")
    click_testid("meta-open")  # pick queue #1: source.png
    wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(., 'Before saving')]")))
    shot("02-metadata-inspected")

    # --- Metadata: save the cleaned copy ------------------------------------
    click_testid("meta-save")  # save queue #1: cleaned.png
    wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(., 'Cleaned copy saved')]")))
    shot("03-metadata-cleaned")

    # --- Hide: pick the secret file -----------------------------------------
    click_nav("hide")
    click_testid("hide-pick-file")  # pick queue #2: secret.bin
    # A built-in carrier is auto-selected; the "choose your own" button is the
    # gate to a custom carrier.  Probe briefly, then fall back to visible text.
    try:
        WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='hide-choose-own']"))
        ).click()
    except Exception:  # noqa: BLE001 - DropZone variant shows a text button
        wait.until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Choose your own file')]"))
        ).click()
    # The click above opened the carrier picker; the queued path (pick queue
    # #3: cleaned.png) is probed by the app and shown by name.
    wait.until(EC.presence_of_element_located((By.XPATH, f"//*[contains(., '{cleaned_name}')]")))
    shot("04-hide-carrier")

    # --- Hide: password + submit --------------------------------------------
    password = driver.find_element(By.CSS_SELECTOR, "#hide-password")
    password.clear()
    password.send_keys(TEST_PASSWORD)
    click_testid("hide-submit")  # save queue #2: hidden.png
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='hide-open-extract']")))
    shot("05-hide-done")

    # --- Extract: handoff from Hide success ---------------------------------
    click_testid("hide-open-extract")
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#extract-password")))
    shot("06-extract-open")

    # --- Extract: password + submit + save ----------------------------------
    extract_password = driver.find_element(By.CSS_SELECTOR, "#extract-password")
    extract_password.clear()
    extract_password.send_keys(TEST_PASSWORD)
    click_testid("extract-submit")
    wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(., 'Found a file')]")))
    shot("07-extract-found")
    click_testid("extract-save-as")  # save queue #3: extracted.bin
    wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(., 'Saved to')]")))
    shot("08-extract-saved")


def verify_files(work: dict[str, Path], source_sha_before: str, secret_sha: str) -> None:
    """Automatic checks outside the app: GPS gone, source intact, bytes match."""
    source = work["source"]
    cleaned = work["cleaned"]
    hidden = work["hidden"]
    extracted = work["extracted"]

    check("source PNG is byte-identical after the whole scenario",
          source.is_file() and sha256_hex(source) == source_sha_before,
          f"sha256={sha256_hex(source)[:16] if source.is_file() else 'missing'}")

    # GPS inspection through the real core (the same code the installed bridge
    # runs).  The window already proved the installed bridge works; this reads
    # the files the window produced.
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from phantom.metadata import inspect_path  # noqa: PLC0415

        report = inspect_path(str(cleaned))
        keys = {item.key for item in report.sorted_fields()}
        check("GPS is gone from the cleaned copy",
              "png.gps.0x0002" not in keys and "png.gps.0x0004" not in keys,
              f"{len(keys)} fields remain")
        check("orientation survives the clean", "png.exif.0x0112" in keys)
        check("copyright survives the clean", "png.exif.0x8298" in keys)
    except Exception as error:  # noqa: BLE001 - a failed inspection is a failed check
        check("GPS is gone from the cleaned copy", False, f"inspect failed: {error}")

    # Pixels must be identical: the clean rewrites containers, never pixels.
    try:
        with Image.open(source) as a, Image.open(cleaned) as b:
            a.load()
            b.load()
            identical = np.array_equal(np.array(a), np.array(b))
        check("decoded pixels are identical (source vs cleaned)", identical)
    except Exception as error:  # noqa: BLE001
        check("decoded pixels are identical (source vs cleaned)", False, str(error))

    check("hidden PNG was written by the Hide step",
          hidden.is_file() and hidden.stat().st_size > 0)
    check("extracted file matches the test original (SHA-256)",
          extracted.is_file() and sha256_hex(extracted) == secret_sha,
          f"extracted={sha256_hex(extracted)[:16] if extracted.is_file() else 'missing'} "
          f"expected={secret_sha[:16]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default=None, help="path to the installed PHANTOM binary")
    parser.add_argument("--out", default=None, help="where to write the JSON report")
    parser.add_argument("--shots", default="window-e2e-shots", help="screenshot directory")
    parser.add_argument("--keep-work", action="store_true", help="keep the temp work dir (debugging)")
    args = parser.parse_args()

    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    app = find_app_binary(args.app)
    if app is None:
        code = blocker(f"installed PHANTOM binary not found (looked for {args.app or '/usr/bin/phantom'})")
        write_report(args.out, app=None, work=None)
        return code

    tauri_driver = find_tauri_driver()
    if tauri_driver is None:
        code = blocker("tauri-driver not found in PATH or ~/.cargo/bin (install: cargo install tauri-driver --locked)")
        write_report(args.out, app=str(app), work=None)
        return code

    webkit_driver = shutil.which("WebKitWebDriver")
    if webkit_driver is None:
        code = blocker("WebKitWebDriver not found in PATH (install: sudo apt-get install -y webkit2gtk-driver)")
        write_report(args.out, app=str(app), work=None)
        return code

    try:
        import selenium  # noqa: F401  # noqa: PLC0415
    except ImportError:
        code = blocker("selenium is not installed (install: pip install selenium)")
        write_report(args.out, app=str(app), work=None)
        return code

    tmp = tempfile.mkdtemp(prefix="phantom-window-")
    work_dir = Path(tmp)
    work: dict[str, Path] = {
        "source": work_dir / "source.png",
        "secret": work_dir / "secret.bin",
        "cleaned": work_dir / "cleaned.png",
        "hidden": work_dir / "hidden.png",
        "extracted": work_dir / "extracted.bin",
    }
    pick_queue = work_dir / "pick_queue.txt"
    save_queue = work_dir / "save_queue.txt"

    source_sha_before = ""
    secret_sha = ""
    driver_process: subprocess.Popen | None = None
    driver_log = shots / "tauri-driver.log"
    started = time.perf_counter()

    try:
        # --- fixtures -------------------------------------------------------
        png_with_gps(work["source"])
        work["secret"].write_bytes(secret_bytes())
        source_sha_before = sha256_hex(work["source"])
        secret_sha = sha256_bytes(secret_bytes())
        check("fixtures: GPS PNG and secret written",
              work["source"].is_file() and work["secret"].is_file(),
              f"source={source_sha_before[:16]} secret={secret_sha[:16]}")

        pick_queue.write_text(
            "\n".join([str(work["source"]), str(work["secret"]), str(work["cleaned"])]) + "\n",
            encoding="utf-8",
        )
        save_queue.write_text(
            "\n".join([str(work["cleaned"]), str(work["hidden"]), str(work["extracted"])]) + "\n",
            encoding="utf-8",
        )

        # --- tauri-driver + installed app -----------------------------------
        env = dict(os.environ)
        env["PHANTOM_E2E"] = "1"
        env["PHANTOM_E2E_PICK_QUEUE"] = str(pick_queue)
        env["PHANTOM_E2E_SAVE_QUEUE"] = str(save_queue)
        env["PYTHONUNBUFFERED"] = "1"

        with driver_log.open("w", encoding="utf-8") as log_handle:
            driver_process = subprocess.Popen(
                [str(tauri_driver)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
            if not wait_port(WEBDRIVER_HOST, WEBDRIVER_PORT, timeout=30.0):
                code = blocker(
                    f"tauri-driver did not listen on {WEBDRIVER_URL} within 30s "
                    f"(see {driver_log}); exit={driver_process.poll()}"
                )
                write_report(args.out, app=str(app), work={k: str(v) for k, v in work.items()})
                return code

            from selenium import webdriver  # noqa: PLC0415
            from selenium.webdriver.chrome.options import Options  # noqa: PLC0415

            options = Options()
            options.set_capability("tauri:options", {"application": str(app)})
            options.set_capability("browserName", "wry")
            try:
                driver = webdriver.Remote(command_executor=WEBDRIVER_URL, options=options)
            except Exception as error:  # noqa: BLE001 - session failure is a blocker
                code = blocker(f"WebDriver session failed to start against {app}: {error}")
                write_report(args.out, app=str(app), work={k: str(v) for k, v in work.items()})
                return code

            try:
                drive_window(driver, shots, work["cleaned"].name)
                check("window scenario completed (Metadata → Hide → Extract)", True)
            except Exception as error:  # noqa: BLE001 - timeouts land here
                check("window scenario completed (Metadata → Hide → Extract)", False, str(error)[:500])
                try:
                    driver.save_screenshot(str(shots / "99-failure.png"))
                except Exception as shot_error:  # noqa: BLE001
                    print(f"  failure screenshot skipped: {shot_error}", flush=True)
                try:
                    (shots / "99-failure-page.html").write_text(
                        driver.page_source, encoding="utf-8", errors="replace"
                    )
                except Exception as dump_error:  # noqa: BLE001
                    print(f"  failure page dump skipped: {dump_error}", flush=True)
            finally:
                try:
                    driver.quit()
                except Exception as quit_error:  # noqa: BLE001
                    print(f"  driver quit skipped: {quit_error}", flush=True)

        # --- automatic verification -----------------------------------------
        verify_files(work, source_sha_before, secret_sha)
    finally:
        if driver_process is not None and driver_process.poll() is None:
            driver_process.terminate()
            try:
                driver_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                driver_process.kill()
        if not args.keep_work:
            shutil.rmtree(tmp, ignore_errors=True)

    write_report(
        args.out,
        app=str(app),
        work={k: str(v) for k, v in work.items()},
        duration=time.perf_counter() - started,
    )
    failed = [row for row in RESULTS if not row["ok"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 0 if not failed else 1


def write_report(
    out: str | None,
    *,
    app: str | None,
    work: dict[str, str] | None,
    duration: float = 0.0,
) -> None:
    if not out:
        return
    report = {
        "app": app,
        "work": work,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "blocker": BLOCKER,
        "duration_seconds": round(duration, 2),
        "passed": sum(1 for row in RESULTS if row["ok"]),
        "failed": sum(1 for row in RESULTS if not row["ok"]),
        "checks": RESULTS,
    }
    Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
