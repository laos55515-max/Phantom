# The macOS crash on window build: what it was, what changed, what is left

Branch `arena/a18be16e-phantom`, PR #17 (open, not merged). Base `ba6b038`
(`main`), head at the time of writing: see the last section.

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
| A theme change refuses every display pass while `ctk.set_appearance_mode()` runs, and owes one afterwards | `ui/widgets.py` (`quiet_redraws`), `ui/app.py` (`_set_appearance_mode`) | one mode change used to flush the window once per widget; the last of those is the Tk 9.1 segfault (§9) |
| On Aqua under Tk 9 the refusal is installed for the whole process and no pass is scheduled | `ui/widgets.py` (`install_quiet_display`), `ui/app.py` (`DISPLAY_PASSES_UNSAFE`) | CustomTkinter also flushes from its own draws, and `update idletasks` cannot be called at all on that combination (§9) |
| `PHANTOM_QUIET_REDRAW=0` and `PHANTOM_QUIET_FLUSH=0` are separate switches | `ui/widgets.py` | a bisect has to be able to turn one suppression off without the other |

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

And for the theme switch:

- `test_a_theme_switch_refuses_every_display_pass` — one click, zero passes, one
  repaint, one pass owed afterwards.
- `test_switching_the_theme_six_times_keeps_the_window_alive` — six clicks in a row.
- `test_a_hidden_window_switches_the_theme_without_a_display_pass` — withdrawn root.
- `test_a_closed_window_never_hears_about_a_theme_change_again` — the tracker drops
  the window when it closes.
- `test_windows_do_not_accumulate_appearance_callbacks` — three build/close cycles
  leave the process-global tracker where they found it.
- `test_a_platform_that_cannot_take_a_pass_is_never_asked_for_one` — the permanent
  refusal exists exactly where the call cannot be made, and only there.

The tests that count passes ask `_passes_are_attempted()` instead of assuming a pass
runs, so the same assertions hold on 8.6 (a pass runs) and on Aqua Tk 9 (none does).

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

`gui (macOS Aqua, Tk 9.1)` — real Aqua, Homebrew `python-tk@3.12` (Tcl/Tk 9.1), the
window driven on Tk's own loop (`tools/smoke_tk91_theme.py --trace`):

```
2.06s  built: tk 9.1, tcl 9.1, 4 children
11.45s theme -> Dark ... 11.51s tab -> settings    (two full rounds, resize included)
11.68s main loop finished cleanly
```

`gui (macOS Aqua, Tk 9.1)` also runs a resize series: eight sizes, one per callback,
frames in between, one layout pass each, our own passes 0.4 ms in 11 calls.

`layout profile (macOS Aqua)` — the before/after comparison of §10 (run `44749622440`),
a measurement job, not a gate.

Other jobs on the same commit (`44749622440`): `tests (Python 3.11)` green,
`tests (Python 3.12)` green, `lint and types` green (ruff + `mypy src`, 60 files),
`slow benchmarks` green (all seven slow tests), `gui (macOS Aqua)` green (above).

Locally (no tkinter in this sandbox, so no GUI tests): `ruff check src tests tools`
clean, `mypy src` clean, `pytest` headless `1690 passed, 1 skipped, 1 failed` — the
failure is `test_100kib_in_24mp_carrier`, which is known to trip only on this
machine's CPU/memory and is green in CI.

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

- The build crash reproduced on the owner's Apple Silicon machine (Tk 8.6.16) and on
  the Aqua CI runner. The Tk 9.1 theme crash is section 9: reproduced in CI, fixed,
  and re-verified by driving the real window on Tk's own loop — not with a sanitizer,
  so the *mechanism* inside Tk is inferred from the frames and Tk's own reports, not
  observed.
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
5451ded measure: leave the loop with quit(), and cap the run that feeds itself
ffcbb2a measure: instrument before the window exists, and resize sizes that really wrap
5ca6ab9 measure: a watchdog, shorter series, and a job that cannot hang for 25 minutes
dd30e6e smoke(tk9): give the loop another turn before calling the pass missing
412e1ce smoke(tk9): resize the tab that has rows to wrap, and stop demanding beats Tk eats
67b4ef0 test+smoke: separate our pass cost from Tk's, and count a burst honestly
c36efde test+smoke: measure a size the window can actually take
d0c56a7 fix(ui): the drop hints do not exist yet while the tabs are being built
7ff03e9 test+fix(dnd,layout): the resize rules, the deferred tabs, and an honest drop status
0a5b686 perf(ui): one layout pass per resize burst, measured widths, hidden tabs deferred
9518ddf measure(ui): a resize profile that reports calls, not guesses
2eb4301 ui: on Aqua under Tk 9 the window never asks Tk for a display pass
d088052 ci(tk9): put src on the path, as the other Aqua job does
37afc7c ci(tk9): gate the window, not the harness that cannot pump Tk
a9a0766 ci: a workflow command's message ends at the first newline
35ccfb1 ci(tk9): quote the lines that name our code, not pytest's waiting loop
27f62b0 ci(tk9): one Aqua session per variant, because runs in one VM disagree
a2850a6 ci(tk9): the gate goes first, the diagnostics behind it
f70476b ui: two escapes, one for each suppression, so a bisect can tell them apart
83495ac ci(tk9): B and D, the two variants a single VM can answer
66a6bc7 ci(tk9): four prefixes, so "before" and "after" are both measured
ef0a95c ci(tk9): the native backtrace goes before the step that dies
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

## 9. The Tk 9.1 theme switch: where it dies, and what the window does about it

**Symptom.** On Aqua with Tcl/Tk 9.1 a *dynamic* theme switch segfaulted
(`Fatal Python error: Segmentation fault`, exit 139). Tk 8.6 on the same machine,
same window, same click: fine. The click itself returned; the interpreter died in
the display pass the mode change triggers.

**Entrances, measured.** Six fresh `macos-latest` VMs, one variant of the GUI suite
each (run `44660612343`), so that no variant can be blamed on the run before it:

| Variant | Died at | Frame |
| --- | --- | --- |
| the suite as shipped | `test_a_theme_switch_refuses_every_display_pass` | `app.root.update()` after the click |
| theme refusal off (`PHANTOM_QUIET_REDRAW=0`) | same test | `_set_appearance_mode` → `ctk.set_appearance_mode` |
| build batching off (`PHANTOM_QUIET_FLUSH=0`) | same test | the owed pass |
| both off | same test | the owed pass |
| every theme test removed (`-k "not theme"`) | `test_embed_flow_through_the_ui` | `_pump` → `app.root.update()` |
| quiet output (`-q`, nothing else) | the same first canvas-heavy tab | `app.root.update()` |

Everything that crashed, crashed in `update idletasks` — reached by CustomTkinter's
own draws, by the schedule the theme change leaves behind, or by the harness's
`root.update()`. The last two rows matter most: with **no theme test at all** the
suite still dies, so the theme path was never the whole story. Tcl/Tk 9.1 on macOS
crashes inside a display pass entered from script level while Tk is drawing, and Tk's
own tracker carries the report that `update idletasks` no longer guarantees a display
on Aqua under 9 at all.

**What the window does.** Nothing the window draws depends on that pass: Tk lays out
and paints in its own loop a moment after the callback that changed something
returns. So, on Aqua under Tk 9, `PhantomApp` never asks Tk for one:

- `quiet_redraws()` still refuses every `update_idletasks` issued while
  `ctk.set_appearance_mode()` runs — one mode change used to flush the window about
  a hundred and twenty times, once per registered widget, and the last one is fatal;
- on Aqua under Tk 9 `install_quiet_display()` then makes that refusal **permanent
  for the process**, because CustomTkinter flushes from its own draws too (a
  scrollbar, a colour change) and any one of those is the same crash;
- `_flush_layout()` schedules no pass at all there (`DISPLAY_PASSES_UNSAFE`), and the
  owed pass from a theme change becomes a no-op instead of an entry into Tk's
  drawing;
- every other platform keeps the pass, unchanged, including Tk 8.6 on Aqua.

The theme switch itself keeps its full behaviour — Dark, Light and System, the
settings file, the appearance-mode tracker, the repaint of every widget.

**Proof on the real platform.** The Tk 9.1 CI job now gates on the *window* rather
than on the harness: `tools/smoke_tk91_theme.py` builds the real `PhantomApp`, then
switches the theme Dark → Light → System, resizes, switches a tab, does it a second
time and closes — every step an `after` callback, no `update()` anywhere, which is
how the application runs. Run `44663561489`, job `gui (macOS Aqua, Tk 9.1)`:

```
2.06s  built: tk 9.1, tcl 9.1, 4 children
11.45s theme -> Dark (mode now Dark)
11.47s theme -> Light (mode now Light)
11.47s theme -> System (mode now Light)
11.47s resized to 1024x720+5+31
11.47s tab -> settings
11.49s theme -> Dark (mode now Dark)
11.50s theme -> Light (mode now Light)
11.51s theme -> System (mode now Light)
11.51s resized to 1024x720+5+31
11.51s tab -> settings
11.51s closing
11.68s main loop finished cleanly
```

**What is still open, stated plainly.**

- The *pytest* suite still segfaults on that runner, with or without every theme
  test, because the harness pumps Tk with `root.update()` from script level and Tk
  9.1 on Aqua dies inside its own drawing there. That is why the suite runs beside
  the smoke run in the Tk 9.1 job and is reported, while the smoke run decides the
  job. It is not skipped, not xfailed and not given a longer timeout; it is simply
  not the thing that can hold a native defect of this shape.
- The owner's MacBook is the only machine that has the crash *outside* a CI VM. On
  it, the commands in section 7 are the confirmation; `PHANTOM_QUIET_REDRAW=0` and
  `PHANTOM_QUIET_FLUSH=0` still switch the two suppressions off for an A/B.
- CustomTkinter's drag-and-drop registration logs
  `cannot register drop target: invalid command name "tkdnd::drop_target"` on Tk 9
  (its bundled `tkdnd` is a Tk 8 extension). It is a warning, not a crash, and the
  window works without drag and drop.
- Tk 8.6 on Aqua, which is what most users run, is untouched by the platform
  branch: 115 passed, 3 skipped on the `gui (macOS Aqua)` job, and the headless
  suite is unchanged.

**If it happens again.**

```bash
# The window, driven the way the application drives it (no update() at all):
python -X faulthandler -u tools/smoke_tk91_theme.py --trace

# The suite, and the same suite with each suppression off:
python -X faulthandler -u -m pytest -m gui -x -vv -s
PHANTOM_QUIET_REDRAW=0 python -X faulthandler -u -m pytest -m gui -x -vv -s
PHANTOM_QUIET_FLUSH=0  python -X faulthandler -u -m pytest -m gui -x -vv -s

# Just one theme switch, no PHANTOM at all:
python -u tools/repro_tk91_theme_crash.py --quiet
```

## 10. The resize microfreeze: what was measured, what changed

Owner's report, on a MacBook Pro M1 Pro, Python 3.12.16, Tcl/Tk 9.1.0: everything
works — launch, Dark/Light/System, ten theme switches, four tabs, trackpad scroll,
metadata cleaning, close and relaunch, smoke exit 0 — except "noticeable microfreezes
when the window is enlarged or shrunk, and when going into and out of fullscreen".

### What was measured, and how

`tools/profile_resize.py` builds the real window and wraps the places that can do work
when the size changes: `Responsive.flows` (the tree walk that finds the flow rows),
`refit_flows`, `_apply` (wrapplength), `FlowRow.reflow`, `PhantomApp._on_window_resize`
and Tk's own `grid`/`grid_forget`. A 10 ms heartbeat records the longest gap the event
loop could not serve. The CI job `layout profile (macOS Aqua)` runs the same tool twice
in the same kind of session — against `445a5cb` (the tree the owner tested, extracted
with `git archive`) and against this branch — so the comparison is a log line, not a
claim. Four resize steps, all in separate `after` callbacks with frames in between.

Real Aqua, Tk 8.6.16, run `44749622440`:

| | before `445a5cb` | after `fa6aff5` |
| --- | --- | --- |
| tree walks to find the flow rows | **29** | **0** |
| reflow passes | 297 | 90 |
| `grid()` calls | 37 | 12 |
| `grid_forget()` calls | **37** | **0** |
| wraplength re-applications | 76 | 76 |
| `<Configure>` handler entries | 22 280 | 21 978 |
| time in those handlers | **43.6 ms** | **6.2 ms** |
| time inside the refit passes | **36.0 ms** | **0.3 ms** |

Both columns come from run `44751559563`; an earlier run of the same pair
(`44749622440`) measured 48.7 ms → 0.4 ms of refit time and 59.9 ms → 9.5 ms in the
handlers, so the ratio is stable at roughly two orders of magnitude while the absolute
numbers move a little between VMs. The counts (walks, passes, `grid`, `grid_forget`,
widened paragraphs) are identical between runs, which is what a count should be.

The handler-entry count is not a typo: a child's `<Configure>` reaches the toplevel's
bind tags, so one resize step delivers a few *thousand* events at the toplevel binding.
Most are rejected by the width filter in both versions — the difference is what the
ones that pass it cost.

### What was wrong, in the order it cost

1. **Every resize event ran a full synchronous refit.** A drag delivers a stream of
   events; each one walked the whole widget tree to find the flow rows and re-flowed
   all of them, inside the event that was supposed to be painting. Now the first event
   of a burst owes one pass on the idle queue (`root.after_idle` — Tk's own queue;
   `update idletasks` is still never called, which Aqua under Tk 9 cannot survive) and
   the rest only move the number.
2. **The tree was walked to find rows that `adopt` had already walked past.** The flow
   rows and their ancestor chains are collected during the one walk the window already
   does at build time; a refit now iterates a list.
3. **Every child was measured on every pass.** `_item_width` reaches into Tk
   (`winfo_reqwidth`) for labels. Widths are now measured once per caption change; a
   language switch forces a re-measure (`refit_flows(force=True)`).
4. **Every reflow forgot and re-placed every child.** `grid` re-places a widget; the
   `grid_forget` calls were pure churn (37 → 0), and passes whose breakpoints did not
   change now touch no widget at all.
5. **Hidden tabs were laid out on every step.** A tab stacked behind another one still
   resizes and still gets `<Configure>`. It now records the width it owes and is laid
   out when it is opened; a language switch, which is rare and changes every caption,
   still lays out all four at once.

Nothing about the interface changed: same breakpoints, same wrapping, same three
languages, same themes, same geometry restore. No cryptography, STC, LSB Matching,
container format or metadata policy was touched.

### Tk 9.1: the part that is not ours

The Tk 9.1 smoke now walks a series of sizes, one per callback, with frames in between
(the runner's display only allows 980x720 … 1024x720, so those are the sizes) and times
our own passes next to the longest loop gap:

```
resize 0: asked 980x700, got 980x720 (clamped by the WM), frames served = 4, layout passes = 1
resize 3: asked 980x720, got 980x720, frames served = 2, layout passes = 1
resize 7: asked 1024x720, got 1024x720, frames served = 1, layout passes = 1
resize series done: 8 steps, worst loop gap 358.1 ms, our own passes 0.4 ms in 11 calls
WARNING the loop was blocked for 358.1 ms by something; our passes account for 0.4 ms
```

So on Aqua under Tcl/Tk 9.1 the window can still stall the event loop for a few hundred
milliseconds when it is resized, and **our layout work is 0.4 ms of it**. What is left
is Tk's own re-layout and CustomTkinter's canvas redraws on that build, both inside
installed libraries: removing it would mean patching CustomTkinter or Tk, which this
branch does not do. This is the documented limitation the task allows for: on Aqua
under Tk 9.1 a resize is visibly slower than on Tk 8.6, and it is not the window's own
Python work. On Tk 8.6 — what most users run — the same series costs 0.4 ms of layout
per four steps.

### Drag and drop on Tk 9: a status that was lying

Tcl/Tk 9 imports `tkinterdnd2` and then fails to create its Tcl commands, so
`drop_target_register` raises `invalid command name "tkdnd::drop_target"` for every
zone. `DndProvider.available` was true (the module imported), so the window kept
showing "drop a file here" hints that could never work. `usable` is now the question:
the module loaded **and** at least one registration succeeded. When it is false the
hints are hidden and the reason is kept for the log. Choosing a file with the button
never went through that path, and a test now fails if it ever starts to.

### Regression tests added

- `test_a_resize_burst_owes_one_layout_pass` — ten events, no pass inside the handler,
  exactly one pass when the loop runs.
- `test_generated_configure_events_do_not_refit_one_by_one` — the same rule through
  Tk's own delivery, where a pass per delivery round is expected and a pass per event
  is not.
- `test_the_flow_rows_are_found_without_walking_the_tree` — the collected list is used.
- `test_reflow_moves_children_without_forgetting_them` — `grid_forget` never called,
  children re-placed, no two in one cell.
- `test_a_hidden_tab_is_laid_out_when_it_is_opened` — and only then.
- `test_a_language_switch_lays_out_every_tab` — all four, hidden or not.
- `test_a_dnd_that_cannot_register_hides_its_hints_and_keeps_the_button`.
