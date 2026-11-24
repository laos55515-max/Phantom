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
