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

## New results

Pending the attribution run. No product fix is claimed from the existing data.
