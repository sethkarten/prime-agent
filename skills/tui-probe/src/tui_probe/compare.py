"""Comparison helpers: screen diffs, frame stats, deterministic equivalence."""

from __future__ import annotations

from collections import Counter

from .capture import Capture, run
from .vt import Screen, truecolor_rgb

_PALETTE_NAMES = {
    0: "black", 1: "red", 2: "green", 3: "yellow",
    4: "blue", 5: "magenta", 6: "cyan", 7: "white",
    8: "bright-black", 9: "bright-red", 10: "bright-green", 11: "bright-yellow",
    12: "bright-blue", 13: "bright-magenta", 14: "bright-cyan", 15: "bright-white",
}


def color_name(code: int) -> str:
    """Human name for a color-count byte: 0xFF default, 0xFE truecolor."""
    if code == 0xFF:
        return "default"
    if code == 0xFE:
        return "truecolor"
    if code in _PALETTE_NAMES:
        return f"{code}-{_PALETTE_NAMES[code]}"
    return f"256-{code}"


def screen_diff(a: Screen, b: Screen, limit: int = 50):
    """First cell mismatches between two same-size screens.

    Returns ([(row, col, cell_a, cell_b), ...], total_mismatch_count).
    Cells are (char, fg, bg, attr); screens of different sizes are
    compared over their overlap.
    """
    rows = min(a.rows, b.rows)
    cols = min(a.cols, b.cols)
    out = []
    total = 0
    for r in range(rows):
        for c in range(cols):
            ca = a.cell(r, c)
            cb = b.cell(r, c)
            if ca != cb:
                total += 1
                if len(out) < limit:
                    out.append((r, c, ca, cb))
    return out, total


def equiv(cmd_a, cmd_b, *args, seconds=5.0, input=None, env=None, cwd=None):
    """Byte-exact comparison of two CLI invocations (pipe mode).

    Runs both commands on pipes with the same stdin/env/args and reports
    exit codes, stdout, and stderr equality. Use for the deterministic
    surface of a reimplementation: version output, help text, error
    paths, flag handling.

    Returns a dict: {equal: bool, exit: (a, b), stdout_equal: bool,
    stderr_equal: bool, stdout_len: (a, b), stdout_diff_at: int|None,
    first_diff: (a-bytes, b-bytes)|None}.
    """
    res = {}
    caps = []
    for cmd in (cmd_a, cmd_b):
        caps.append(run(cmd, *args, seconds=seconds, pty=False,
                       input=input, env=env, cwd=cwd, term=None))
    (ca, cb) = caps
    stdout_equal = ca.raw == cb.raw
    stderr_equal = ca.stderr == cb.stderr
    diff_at = None
    first_diff = None
    if not stdout_equal:
        for i, (x, y) in enumerate(zip(ca.raw, cb.raw)):
            if x != y:
                diff_at = i
                break
        else:
            diff_at = min(len(ca.raw), len(cb.raw))
        first_diff = (ca.raw[max(0, diff_at - 40):diff_at + 40],
                      cb.raw[max(0, diff_at - 40):diff_at + 40])
    return {
        "equal": ca.exit_status == cb.exit_status and stdout_equal and stderr_equal,
        "exit": (ca.exit_status, cb.exit_status),
        "stdout_equal": stdout_equal,
        "stderr_equal": stderr_equal,
        "stdout_len": (len(ca.raw), len(cb.raw)),
        "stdout_diff_at": diff_at,
        "first_diff": first_diff,
    }


def frames_report(capture: Capture):
    """Per-frame summary of a snapshotted capture.

    For each frame: time, cells that changed since the previous frame,
    foreground color distribution (as raw byte codes; see color_name),
    and how many distinct colors are in play. The animated-vs-static
    question is answered by whether changed_cells stays > 0.
    """
    out = []
    prev = None
    for t, frame in zip(capture.frame_times, capture.frames):
        changed = 0
        if prev is not None and prev.rows == frame.rows and prev.cols == frame.cols:
            for r in range(frame.rows):
                for c in range(frame.cols):
                    if prev.cell(r, c) != frame.cell(r, c):
                        changed += 1
        counts = frame.color_counts()
        out.append({
            "t": t,
            "changed_cells": changed,
            "distinct_colors": len(counts),
            "fg_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        })
        prev = frame
    return out


def compare_frames(ca: Capture, cb: Capture):
    """Compare two snapshotted captures of two binaries.

    Aligns frames by index (the two runs should use the same snapshot
    cadence and duration) and reports: per-pair changed-cells difference,
    mean color-count deltas, and frame-rate estimates. For animated TUIs
    expect pairwise cell equality to fail (frame timing differs); the
    useful signals are the color palette, the per-frame change volume,
    and any static region (title bar, borders) that does match.
    """
    n = min(len(ca.frames), len(cb.frames))
    pairs = []
    for i in range(n):
        fa = ca.frames[i]
        fb = cb.frames[i]
        _, mism = screen_diff(fa, fb)
        pairs.append({
            "index": i,
            "t_a": ca.frame_times[i],
            "t_b": cb.frame_times[i],
            "mismatched_cells": mism,
        })
    def _palette(capture):
        agg = Counter()
        for f in capture.frames:
            agg.update(f.color_counts())
        return {color_name(k): v for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:12]}
    return {
        "frames": pairs,
        "palette_a": _palette(ca),
        "palette_b": _palette(cb),
        "fps_a": _fps(ca),
        "fps_b": _fps(cb),
    }


def _fps(capture: Capture):
    if len(capture.frame_times) < 2:
        return None
    span = capture.frame_times[-1] - capture.frame_times[0]
    return round((len(capture.frame_times) - 1) / span, 2) if span > 0 else None
