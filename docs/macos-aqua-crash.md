# The macOS crash on window build: what it was, what changed, what is left

Branch `arena/a18be16e-phantom`, PR #17 (open, not merged). Base `ba6b038`
(`main`), head at the time of writing: `525c0cc`.

Everything below was measured on a real Aqua session: the CI job `gui (macOS Aqua)`
runs `pytest -m gui` on `macos-latest` (arm64, macOS 26.7.2, Python 3.12, bundled
Tk 8.6.16) on every push. Xvfb on Linux is not evidence for a macOS fix and is not
used as such here.

## 1. What was crashing

The GUI suite died with SIGSEGV while a window was being built, not while it was
being used: the crash happened before any test body ran, on the first
`PhantomApp(...)` of the session, and only on Aqua.

**Established cause.** The build drove the display synchronously from inside the
constructor and, on a hidden window, drove it again for a window that Tk had no
surface for. On Aqua both land inside Tk's own idle drawing pass, and that is
where Tk's open crash reports live: an idle task that still holds a window pointer
whose window has gone away (`Tk_UnmapWindow`/`Tk_UnmaintainGeometry` recursion,
unretained `NSWindow` references passed to `showRootWindow()`). The relevant Tk
maintainer statements are cited in the code comments; the shape of the crash —
`update idletasks` from script level, sitting inside Aqua's draw loop — matches the
minimal segfault in Tk ticket `4994f5db` (fixed in Tk 8.6.13) and the class of bug
reopened in Tk 9.1 (`update idletasks` no longer guarantees a display on Aqua).

Three specific things in the build produced those calls:

1. `PhantomApp.__init__` called the layout flush directly, so a constructor drove
   the event loop before the window existed on screen.
2. A window that was not viewable (`withdraw()`n root, which the test fixture used
   and an embedding application may use) was still asked for a display pass.
3. `ctk.set_appearance_mode()` was called *after* the widgets were built, and every
   nested CustomTkinter scrollbar flushes the whole window when the mode changes —
   measured at about 120 nested `update_idletasks` calls for one dark build.

## 2. What changed in the product

| Change | Where | Why |
| --- | --- | --- |
| No display pass from the constructor; one is scheduled for the first idle callback | `ui/app.py` (`__init__`, `_after_first_frame`) | a constructor must not run the event loop, and the pass lands after Tk has drawn the first frame itself |
| A non-viewable toplevel is never asked to display; the owed pass runs once on `<Map>` | `ui/app.py` (`_schedule_layout_flush`, `_on_root_mapped`) | Aqua has no surface for a hidden window: exactly the state the crash reports describe |
| Appearance mode applied before the widgets exist | `ui/app.py` (`_apply_theme` call order) | removes ~120 nested `update_idletasks` per dark build |
| `PHANTOM_QUIET_FLUSH=0` turns the quiet-flush batching off | `ui/widgets.py` | an A/B switch for anyone bisecting a display problem: same build, old flush behaviour |
| Every `after` timer is cancelled on close | `ui/app.py` (`_on_close`) | a callback that fires between `destroy()` and exit reaches widgets that are gone |

Constraints were respected: no crypto, STC, LSB-matching or container-format
change; no weakened metadata or integrity policy; no new feature; no refactor of
the scroll or responsive work; no test skipped, xfailed, disabled or given a
longer timeout.

## 3. What changed in the tests

Seven regression tests, all of them asserting behaviour that the crash depended on:

- `test_the_build_never_enters_the_event_loop` — the constructor must not pump Tk.
- `test_building_a_dark_window_runs_no_display_pass_at_all` — dark builds included.
- `test_a_hidden_window_is_never_asked_to_display` — a withdrawn root gets no pass.
- `test_a_second_mapping_does_not_repeat_the_pass` — the owed pass runs once.
- `test_windows_survive_being_built_and_destroyed_repeatedly` — create/destroy ×N.
- `test_closing_the_window_leaves_no_callbacks_of_its_own` — no leftover `after`s.
- `test_a_chain_of_jobs_gives_the_collector_back` — two jobs in a row leave the
  collector enabled (see §5).

Harness work that this required:

- `tests/conftest.py` enables `faulthandler(all_threads=True)`, so a native crash
  prints every thread instead of killing the process silently.
- The `app` fixture maps the window, the way a real launch leaves it, and hands the
  collector back after `shutdown()`.
- `_pump()` — the test's "wait until the window is idle" — now waits for the
  worker, the app's own busy flag, the collector pause **and** the debouncer: a job
  that finishes often schedules the next one (the carrier probe schedules the size
  estimate), and waiting only for the worker made the test blame a window that was
  busy for good reason. On failure it reports the operation, the status line, the
  armed `after` scripts, how many times the poll callback ran, how long the event
  loop spent per `update`, and the exception the poll swallowed.

## 4. Test results

`gui (macOS Aqua)` — real Aqua, Tk 8.6.16, `pytest -m gui`:

```
115 passed, 3 skipped, 1683 deselected
```

The three skips are pre-existing and platform-conditional: Tk 8.6 has no
`<TouchpadScroll>` event to compare against. No SIGSEGV, no timeout, in three
consecutive full runs of the workflow (`44630074539`, plus two reruns of it).

Other jobs on the same commit: `tests (Python 3.11)` green, `tests (Python 3.12)`
green, `lint and types` green (ruff + `mypy src`, 60 files), `slow benchmarks`
green (all seven slow tests).

Locally (no tkinter in this sandbox, so no GUI tests): `ruff check src tests` clean,
`mypy src` clean, `pytest` headless `1682 passed, 1 skipped, 1 failed` — the failure
is `test_100kib_in_24mp_carrier`, which is known to trip only on this machine's
CPU/memory and is green in CI.

## 5. Three defects found on the way, and how they were established

These were found because the GUI suite kept failing after the crash was gone. Each
one was established from data, not guessed:

1. **The collector pause was recorded per window.** `_resume_collection_after_job`
   deferred while `worker.busy` (the thread outlives its DONE event) and then gave
   up; a chained job started inside that window, re-read the collector — off,
   because the first job had turned it off — and recorded "it was never enabled",
   so the last resume switched nothing back on. Evidence: two tests failing with
   `gc.isenabled()` False with nothing in flight, plus the code path above. The
   record is now process-wide and the resume retries until the thread is gone.

2. **A job that never started could leave the window busy.** `_start_job` set the
   busy flag before anything that can fail (repaint, progress reset, collector
   pause, starting the thread). Evidence: the state signature `worker=False,
   app=True, operation='estimate', poll_error=''`. The bookkeeping is now rolled
   back before the error is re-raised.

3. **The poll chain could die with the window still busy.** It was re-armed only
   while the *worker* reported busy, so a poll that failed in the gap after the
   last report left `_busy` true with nothing left to clear it. It now re-arms while
   either side says a job is in flight, and it records the exception it swallowed.

The remaining `still busy after 30 s` failures turned out to be the harness, not the
product (see `_pump` above): the report showed `iterations=12, update_total=0.3s`
— the loop was not starved, it had returned, and the follow-up job started on the
final `update()`. That is why the test now also waits for the debouncer.

## 6. Limitations

- The crash reproduced on the owner's Apple Silicon machine (Tk 8.6.16) and on the
  Aqua CI runner. It has **not** been reproduced on Tk 9.1 with a sanitizer; the
  Tk 9 job in the workflow (`homebrew python-tk@3.12`, `continue-on-error`) is a
  smoke test, not a proof for every Aqua build.
- "No crash in three consecutive runs" is evidence, not a guarantee: a native
  crash that needs a specific widget/layout state could still be hiding behind a
  different code path. `PHANTOM_QUIET_FLUSH=0` and the faulthandler dump in
  `tests/conftest.py` exist so the next occurrence produces a stack, not a mystery.
- The geometry test asserts the *order* (the saved geometry is applied while the
  root has no children of its own), not the size the window manager settles on:
  the app's minimum size is larger than the runner's 980×720 display, so the WM
  grows the window to the screen there and honours the request on X11.
- A window that is destroyed without going through `_on_close` (a crash, an
  embedding application) still leaves its own `after` timers to the interpreter;
  the collector is given back in that case, the timers are not.

## 7. Updating a local checkout

```bash
cd ~/Phantom
git fetch origin
git checkout arena/a18be16e-phantom
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
python -m phantom
```

Diagnostics, if anything still misbehaves on macOS:

```bash
# The single test that used to crash the session, with every thread in the dump:
python -X faulthandler -u -m pytest tests/test_gui.py::test_clipboard_timer_clears_the_secret_natively -vv -s

# A/B the flush batching (same build, old behaviour):
PHANTOM_QUIET_FLUSH=0 python -m pytest -m gui -x -q

# Which Tk is this?
python -c "import tkinter; print(tkinter.TkVersion, tkinter.TclVersion)"

# Native crash reports from the last runs:
ls -t ~/Library/Logs/DiagnosticReports/Python* | head -3
```

## 8. Commits

```
9274d7d ui(macos): never force a display pass from the constructor or on a hidden window
143aecb tests+ci: fix the new GUI expectations, cancel every timer on close, real macOS runner
4fd0f88 ci(macos): macOS ships no GNU timeout; pytest-timeout and the step cap already bound the run
dc71f27 tests(gui): never pump a withdrawn window — on Aqua that call does not return
d7c9116 tests(gui): map the geometry window before reading its geometry
26d48e6 ui: the poll chain cannot die while the window believes a job runs
ec63e24 tests(gui): assert the geometry *order*, then the size the window took
3088875 ui: give the collector back when a job's thread is still winding down
a06dc0f ui: a job that never started cannot leave the window busy
d681bcb tests(gui): measure the event loop, and stop asking the WM for a size it refuses
525c0cc tests(gui): wait for the job the finished one schedules
```
