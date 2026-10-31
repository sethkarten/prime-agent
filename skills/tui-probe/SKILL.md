---
name: tui-probe
description: Characterize a terminal program's behavior - run a TUI/CLI executable under a controlled pty with fixed size/TERM and keystrokes, render its escape stream to screen grids, take frame snapshots, diff screens, and compare a reimplementation against the original. Use when reverse-engineering or rebuilding an executable with observed behavior (ProgramBench-style tasks), or whenever you need programmatic terminal capture instead of hand-rolled pty/VT tooling.
---

# tui-probe

A bundled, zero-dependency characterization toolkit for terminal programs.
It replaces the from-scratch pty-runner / VT-emulator / grid-diff tooling that
reverse-engineering tasks otherwise burn dozens of turns building in /tmp.

The `tui_probe` module is pre-imported in every session kernel; the same code
runs via `python3 -m tui_probe` for shell use.

## Workflow for "rebuild this executable" tasks

1. **Deterministic surface first, byte-exact.** For version/help/error/flag
   output that does not animate, compare on pipes:

```python
tui_probe.equiv("./executable", "./mybuild", "-V")
# -> {equal, exit, stdout_equal, stderr_equal, stdout_diff_at, first_diff}
```

Iterate until `--help`, `-V`, unknown-flag errors, and short-input behavior are
byte-identical (exit codes included). This is the cheapest, most decisive
evidence you can get.

2. **TUI capture through a real pty.** Animated or full-screen behavior:

```python
cap = tui_probe.run("./executable", rows=24, cols=80, seconds=2.0,
                    term="xterm-256color", snapshots=0.2)
cap.screen.text          # final screen as plain text
cap.screen.cell(0, 0)    # (char, fg, bg, attr); fg/bg: -1 default,
                         # 0-255 palette, 0x01000000|rgb truecolor
cap.screen.color_counts() # foreground color histogram
cap.frames               # per-snapshot Screen objects (cap.frame_times)
cap.raw                  # raw bytes of the whole session
```

Keystrokes: `keys=b"h\x1b"` sent `key_after` seconds in (default 0.3).
The process is terminated at the deadline, so infinite screensavers and
dashboards are safe to run.

3. **Compare your build against the original.** Captures of the two binaries
   (same rows/cols/term/seconds/snapshots) line up frame by frame:

```python
rep = tui_probe.compare_frames(cap_orig, cap_mine)
# per-frame mismatched_cells, color palettes, fps; static regions
# (borders, title bars) must match; animated regions are compared by
# structure, not frame timing
```

`tui_probe.screen_diff(a, b)` gives (mismatches, total) for two screens, and
`tui_probe.color_name(code)` decodes histogram codes ("default",
"46-green"/"256-196"/"truecolor", basic names 0-15).

4. **Statistical frame checks for animation.** `tui_probe.frames_report(cap)`
   yields per-frame changed-cell counts and color distributions - use it to
   pin down frame rate, stream lengths, and color rules that byte comparison
   cannot see.

## Long captures: keep the turn discipline

`tui_probe.run()` blocks the kernel for the capture window. For anything
longer than a few seconds, run the CLI through bash() and end the turn:

```python
h = bash("""python3 -m tui_probe run --rows 24 --cols 80 --seconds 30 \
  --snapshot 0.5 --json /tmp/cap.json -- ./executable""")
# end the turn; the [bash-done] wake resumes the session
d = json.load(open("/tmp/cap.json"))
```

The JSON carries exit status, duration, screen text, foreground histogram,
frame report, and (with `--grids`) full per-frame grids; `--raw` adds the
base64 byte stream for later re-rendering (`tui_probe.render(raw)`).
`python3 -m tui_probe equiv -- ./a --help -- ./b --help` prints an equiv
report for shell use.

## What the renderer covers

CUP/CUU/CUD/CUF/CUB/CHA/VPA, ED/EL/ECH, IL/DL/ICH/DCH, SU/SD/RI/IND/NEL,
scroll regions (DECSTBM), alt-screen and cursor-visibility modes (grid
rendering is unaffected by them), DECSC/DECRC, tabs/wrap/BS/CR/LF, SGR bold
through strike, 16/256/truecolor fg+bg, and UTF-8 text. OSC/DCS/APC strings
and unknown CSI finals are consumed and dropped, never rendered. If a real
capture renders something odd, check `cap.raw` against the grid.

## Task rules you must still respect

For an original binary you are characterizing: interact with it only through
its normal interface - the pty captures here are exactly that. No
decompilers, no strace/ltrace on the original. Analysis of binaries YOU
produced during development is unrestricted and is where the A/B comparisons
belong: point both sides of `equiv`/`compare_frames` at your build once it
exists.
