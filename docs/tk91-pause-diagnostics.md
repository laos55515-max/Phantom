# Tk 9.1 heartbeat pause investigation

The fixed comparison is base `f5bb4f7818c201fd1c379d33034072de386da6cb`
versus merge `5f8b84d57efb656f3c1485dbe3440b0de0072cbf`. The
`Tk 9.1 pause diagnostics` workflow archives these exact trees and runs this
branch's *same* instrument against both on one macOS runner. It alternates the
revisions and runs three launches of each with capture and three without it.
All raw per-launch JSON and logs, plus a summary, are uploaded as an artifact.
The existing startup-frame gate, including its 2000 ms threshold, is unchanged.

A gap starts at the previous heartbeat and ends at the next one. This brackets
an unresponsive interval, not its exact cause; a normal heartbeat is scheduled
10 ms later. The watchdog records Python main-thread stacks every 200 ms while a
gap is active; only samples timestamped strictly inside each reported interval
are attached to that interval. Python stack samples can be absent if the GIL is
held, and a Python stack stopped in `mainloop` cannot identify a native Tk or
OS frame. Neither case establishes a root cause. Sampling adds overhead.

`first_paint_ms` is the start time of the first screenshot whose pixels meet the
existing non-uniformity criterion, not an exact compositor timestamp. A missing
capture leaves visibility unknown. Without capture, first paint is deliberately
unknown. Both diagnostic modes omit the conditional focus push (which otherwise
would always fire without capture); the metadata click and remaining workload
are unchanged between those two modes. Thus these runs test capture's influence
on this diagnostic workload, not necessarily on an uninstrumented application.
The original gate still uses its original push behavior. Compare distributions,
not just maxima, and do not infer causation from one noisy runner.

## First comparison (CI 45428259255)

[Run and complete raw artifact](https://github.com/laos55515-max/Phantom/actions/runs/45428259255).
All 12 launches completed; raw JSON contains every gap and its timestamped stack
samples. GitHub exposed only ten measurement annotations for the step; the two
remaining rows are in the artifact. The table below transcribes those ten
annotations (times in ms from process measurement start; paint is unknown without
capture). Each interval is the longest heartbeat gap in that launch.

| Tree / mode / launch | First visible capture | Gap start | Gap end | Gap duration |
|---|---:|---:|---:|---:|
| base capture 1 | 2395.0 | 3880.6 | 5823.4 | 1942.8 |
| base capture 2 | 678.3 | 1481.2 | 3102.2 | 1621.0 |
| base capture 3 | 989.4 | 1847.3 | 3709.0 | 1861.7 |
| merge capture 1 | 1218.2 | 2277.8 | 4499.6 | 2221.8 |
| merge capture 2 | 823.7 | 1989.0 | 3561.7 | 1572.6 |
| merge capture 3 | 909.0 | 1697.5 | 3445.8 | 1748.3 |
| base no capture 1 | unknown | 1294.2 | 2064.1 | 769.9 |
| base no capture 2 | unknown | 1374.6 | 2373.4 | 998.7 |
| merge no capture 1 | unknown | 1515.6 | 2592.9 | 1077.3 |
| merge no capture 2 | unknown | 1307.0 | 2375.4 | 1068.4 |

The merge launch 1 reproduces a >2000 ms gap after the first visible capture.
Its seven watchdog samples during 2586.2–4293.0 ms all show the Python main
thread in `CTk.mainloop -> tkinter.mainloop`. This does not identify the native
cause. Most other longest gaps show the same Python stack; merge capture 2 also
has one sample in a CTk textbox canvas coordinate update. The visible no-capture
runs have shorter gaps than the capture runs, consistent with measurement
interference, but this small sample and the Python-only stacks do **not** prove
what caused it. No product or gate fix is justified from these observations
alone; obtain native stacks and isolate capture/PNG decoding before proposing one.
