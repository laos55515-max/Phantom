# Tk 9.1 startup: application stall or measurement interference?

## Scope and evidence

Product tree: `f788e2a310482b66b21806ac283534da8a6ea5f9` (current main at the
start of this investigation). No application code or required gate threshold is
changed by this investigation. In particular, the heartbeat limit remains
**2000 ms**; a diagnostic exit code is not a replacement for that gate.

### Requested failure

[Run 45710287074, job 139542623562](https://github.com/laos55515-max/Phantom/actions/runs/45710287074/job/139542623562)
failed solely on `startup frame`: `pr-6: heartbeat blocked for 3397.2 ms`.
The job's annotations report:

| Process | Build | First loop callback | First idle | Largest heartbeat gap |
|---|---:|---:|---:|---:|
| pr-2 | 831.5 ms | 898.7 ms | 925.3 ms | 1592.1 ms |
| pr-3 | 848.0 ms | 926.0 ms | 959.7 ms | 1752.3 ms |
| **pr-6** | **640.1 ms** | **690.1 ms** | **706.2 ms** | **3397.2 ms** |

Thus the failing 3.3972 seconds are a **heartbeat service gap during the event
loop**, not a 3.4-second construction of the application or delay entering the
loop. Loop service began 50.0 ms after construction. All three metadata clicks
completed in 0.0–0.1 ms, all tabs became mapped, and all closes completed
(139.6–169.2 ms). The GUI suite, theme smoke and editor smoke passed; no crash
report was written. Both `main` and `pr` trees were the **same commit** in this
push run, so these labels do not describe two different product versions.

Limitations: the failing process's exact gap endpoints/native stack were not
published in the retained annotations. The global first-eight watchdog limit
can consume the samples during construction/early loop activity, before a later
stall. GitHub also caps annotations per step: the detailed final `pr-6` row was
not retained, although the aggregate and error were. The artifact API lists only
`gui-log-3.11` and `gui-log-3.12` for this run: **no startup JSON, six per-process
logs, or native startup samples were uploaded**. Full job logs/artifact downloads
redirect to Actions storage hosts inaccessible from this sandbox (EOF); API
annotations and the artifact inventory were read instead. We cannot honestly
claim to have read raw startup artifacts that were not uploaded.

The existing `lldb` step targets a **different process** (`harness_churn`), not
the startup process. Its frames cannot explain this heartbeat gap.

### Comparison with prior work

[PR #26](https://github.com/laos55515-max/Phantom/pull/26), especially
`docs/tk91-pause-diagnostics.md` at `c52f1dfc91faae7dfb7868db9849007b7368b5ca`,
already compared fixed revisions with/without capture. In its
[run 45428259255](https://github.com/laos55515-max/Phantom/actions/runs/45428259255),
a captured merge launch had a 2221.8 ms gap (2277.8–4499.6 ms). The seven Python
samples were in `CTk.mainloop -> tkinter.mainloop`; visible no-capture rows were
shorter (769.9–1077.3 ms). PR #26 correctly did **not** identify a native cause or
claim a fix. Its raw artifact is listed by the API, but download has the same
storage-host restriction here. This investigation reuses its opt-in diagnostic
gap association and matched no-focus-push comparison, not its unmerged product tree.

[PR #30's successful run 45701998801](https://github.com/laos55515-max/Phantom/actions/runs/45701998801)
reported PR gaps **1565.4, 1934.3, 1179.6 ms**; its base-tree gaps were
1917.2, 2031.0, 1128.0 ms (only PR rows decide the gate). The 1934.3 ms interval
ended at 4182.8 ms, so it began at 2248.5 ms, after the first screenshot timestamp
1136.4 ms. This is close to the threshold, not evidence the intermittent problem
was fixed. The successful PR was about metadata, not startup timing.

## One new attribution experiment

Workflow: `Startup pause attribution (Tk 9.1)`.

- Fix the product tree to the failed main SHA above. Record the instrument SHA,
  OS, Python and installed packages alongside every result.
- Six **sequential fresh processes** on one macOS Tk 9.1 runner: three pairs,
  capture/on then off, off then on, on then off. No concurrent application
  windows. Both diagnostic modes omit the conditional focus push; the metadata
  click and close workload stays the same (as in PR #26).
- Keep the 10 ms heartbeat. Record each >=300 ms interval, its main-thread CPU
  time, and timestamped Python watchdog samples strictly inside that interval.
- Separately time `screencapture`, Python PNG decoding and pixel sampling, with
  wall and capture-thread CPU time. Incomplete operations remain explicitly
  incomplete (no invented end time). Overlap is **correlation**, not causation.
- An **independent macOS `sample` process**, at 10 ms cadence for up to 10 seconds,
  records native stacks of the actual measured Python PID in both modes. It does
  not need the app's GIL to make progress. This supplies the native evidence
  missing in PR #26; the sampling itself is an intervention and can add overhead.
- Save raw JSON, logs, native profiles and environment. Emit four annotations per
  pair (not twelve in one step) so GitHub's per-step cap does not discard the last
  process. A crash, timeout or failed native sample is an incomplete experiment,
  not a pass. No-capture visibility is explicitly unknown.

`first_paint_ms` remains the **start** of a screenshot later judged non-uniform,
not a compositor completion timestamp or proof that every widget has painted.
Native `sample` is an aggregated call tree, not a timestamped trace of every
individual stall. Absence of a particular stack in that sample is not proof the
work never happened. A clean experiment alone does not establish a repair.

The normal CI job now uploads `startup-frame.json`, `frame-*.json` and
`frame-*.log` even when its unchanged startup gate fails. That fixes the confirmed
**loss of diagnostic evidence**, not a claimed product freeze.

## New results — one completed experiment, six sequential processes

[Run 45894273662](https://github.com/laos55515-max/Phantom/actions/runs/45894273662),
[job 140164178545](https://github.com/laos55515-max/Phantom/actions/runs/45894273662/job/140164178545).
Instrument: `30d542f9c33bd414ed8c8147e15d10fc96ebef1d`. Product SHA is unchanged.
All six application exits and all six native samplers returned 0; every metadata
tab answered and every window closed. Artifact **startup-pause-attribution-tk91**
(ID `15152443613`, 801378 bytes) retains raw data/native profiles/environment.
The table is transcribed from the API-visible measurement annotations, not from
an unavailable artifact download.

Times are ms from each process's measurement origin, except CPU and durations.

| Pair / mode | Build | Gap start | Gap end | Gap duration | Main-thread CPU in gap |
|---|---:|---:|---:|---:|---:|
| 1 capture | 4139.2 | 6028.6 | 7811.4 | 1782.8 | 1086.8 |
| 1 no capture | 1696.9 | 2259.7 | 3382.1 | 1122.4 | 1029.1 |
| 2 no capture | 1543.5 | 2036.7 | 2881.5 | 844.8 | 824.7 |
| 2 capture | 1783.5 | 2846.1 | 4775.5 | 1929.4 | 1083.1 |
| 3 capture | 1888.0 | 3143.0 | 4794.0 | 1651.0 | 1061.2 |
| 3 no capture | 1782.6 | 2303.3 | 3156.2 | 852.9 | 837.2 |

### What this adds (and does not add)

- This was **not a reproduction of the 3397.2 ms failure**. All six heartbeat
  maxima stayed below 2000 ms, but that is not a claim of a repair: the product
  tree is literally the old one. The diagnostics do not replace/rerun the gate
  with weaker criteria or drop its failures.
- The capture rows are longer than their paired no-capture rows by 660.4,
  1084.6 and 798.1 ms. This reinforces PR #26's interference evidence, with a
  matched workload and new native/CPU data, but six runs do not attribute the
  old outlier or exclude runner/sampling variability.
- There is **real application-thread work without capture**: 824.7–1029.1 ms of
  main-thread CPU during 844.8–1122.4 ms service gaps. Removing screenshots does
  not remove all heartbeat latency; calling the whole problem merely a slow
  screenshot command would be unjustified.
- Captured native main-thread profiles contain
  `Tcl_DoOneEvent -> TclServiceIdle -> ArrangeGrid -> Tk_MoveResizeWindow ->
  XMoveResizeWindow -> GenerateConfigureNotify -> TkDoConfigureNotify ->
  Tk_HandleEvent -> TkBindEventProc`. They also contain callbacks through
  `PythonCmd -> PyEval_RestoreThread -> take_gil -> _pthread_cond_wait`.
  These identify native layout/configure work and GIL waiting, rather than just
  Python's opaque `mainloop`. They are **aggregate profiles of the run**, not
  time-localised proof that any one frame caused the worst interval.
- Capture timing separates mechanisms: during pair 2's 1929.4 ms gap, three
  PNG decodes lasted 251.6 / 282.2 / 279.6 ms, using 240.6 / 233.8 / 247.0 ms
  of capture-thread CPU; `screencapture` calls lasted 133.4 / 162.0 / 168.3 ms
  and pixel sampling 3.4–8.0 ms. It is not one 1.9-second blocking capture call.
  The decodes and GIL-wait samples are consistent with interference, not yet a
  complete attribution of the original failure.
- The first cold instrumented construction took **4139.2 ms**, before the
  heartbeat was armed; later builds took 1543.5–1888.0 ms. The independent native
  sampler runs during construction too, unlike the old gate. These wall times
  must not be substituted for uninstrumented Mac startup times. This also makes
  an unchanged green heartbeat result insufficient to assert a fast first frame.

### User-facing conclusion / review status

If the reported 3397.2 ms service gap occurred in an ordinary Mac launch, its
window would temporarily fail to service timer/event callbacks (potentially
appear frozen); the data do not establish that an ordinary, uninstrumented user
actually gets that entire delay, or that a click is needed to recover. In the
requested failing run, construction/loop entry were already complete around
0.64/0.69 seconds, and the later click and close did work.

We have established a post-construction service gap, real Tk/application work,
measurement interference consistent across three pairs, and a confirmed loss of
raw diagnostic evidence in the old workflow. **We have not established the cause
of the rare 3.3972-second gap or fixed a product freeze.** The only confirmed fix
here is retaining the ordinary gate's raw evidence; the added diagnostic measures
make the remaining alternatives distinguishable rather than declaring victory
from green reruns. A next investigation needs a reproduction above 2000 ms with
native samples localised to that exact interval before changing application or
capture architecture. The PR remains open for review; the 2000 ms threshold and
product source are unchanged.
