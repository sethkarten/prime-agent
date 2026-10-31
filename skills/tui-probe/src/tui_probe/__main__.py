"""CLI: python3 -m tui_probe <run|render|equiv|selftest> [options] -- cmd...

The CLI form exists so long captures run through bash() with the turn
ended (the [bash-done] wake resumes the session) instead of parking the
kernel; short captures are fine directly from python_repl.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys

from . import capture as capmod
from . import compare as cmpmod


def _screen_grid(frame):
    return {
        "rows": frame.rows,
        "cols": frame.cols,
        "chars": frame.chars,
        "fg": [f.tobytes().hex() for f in frame.fg],
        "bg": [f.tobytes().hex() for f in frame.bg],
        "attr": [f.hex() for f in frame.attr],
    }


def _run_summary(cap, cmd_argv, rows, cols, term, seconds, include_raw,
                 include_grids):
    summary = {
        "cmd": cmd_argv,
        "rows": rows,
        "cols": cols,
        "term": term,
        "seconds": seconds,
        "exit_status": cap.exit_status,
        "killed": cap.killed,
        "duration": cap.duration,
        "raw_bytes": len(cap.raw),
        "screen_text": cap.screen.text,
        "fg_counts": {str(k): v for k, v in
                      sorted(cap.screen.color_counts().items(), key=lambda kv: -kv[1])},
        "frames": cmpmod.frames_report(cap) if cap.frames else [],
    }
    if include_raw:
        summary["raw_b64"] = base64.b64encode(cap.raw).decode()
    if include_grids and cap.frames:
        summary["grids"] = [
            {"t": t, **_screen_grid(f)} for t, f in zip(cap.frame_times, cap.frames)
        ]
    return summary


def _cmd_run(args):
    argv = list(args.cmd)
    if argv and argv[0] == "--":
        argv = argv[1:]
    cap = capmod.run(
        argv[0], *argv[1:],
        rows=args.rows, cols=args.cols, term=args.term, seconds=args.seconds,
        keys=args.keys.encode() if args.keys else b"",
        snapshots=args.snapshot,
    )
    summary = _run_summary(cap, argv, args.rows, args.cols, args.term,
                           args.seconds, args.raw, args.grids)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(summary, fh)
        print(f"wrote {args.json}: {cap!r}")
    else:
        print(json.dumps({k: v for k, v in summary.items()
                          if k not in ("screen_text", "frames", "grids", "raw_b64")}, indent=2))
        print("--- screen ---")
        print(cap.screen.text)


def _cmd_render(args):
    data = open(args.file, "rb").read()
    screen = capmod.render(data, rows=args.rows, cols=args.cols)
    if args.json:
        summary = {
            "file": args.file,
            "screen_text": screen.text,
            "fg_counts": {str(k): v for k, v in
                          sorted(screen.color_counts().items(), key=lambda kv: -kv[1])},
        }
        with open(args.json, "w") as fh:
            json.dump(summary, fh)
        print(f"wrote {args.json}")
    else:
        print(screen.text)


def _cmd_equiv(args):
    cmds = list(args.cmds)
    if cmds and cmds[0] == "--":
        cmds = cmds[1:]
    sep = cmds.index("--")
    a = cmds[:sep]
    b = cmds[sep + 1:]
    report = cmpmod.equiv(a, b, seconds=args.seconds,
                          input=args.input.encode() if args.input else None)
    print(json.dumps(report, default=lambda o: o.hex() if isinstance(o, bytes) else str(o), indent=2))
    return 0 if report["equal"] else 1


def _cmd_selftest(args):
    from .selftest import main as selftest_main
    return selftest_main()


def build_parser():
    p = argparse.ArgumentParser(prog="tui_probe",
                                description="TUI/CLI characterization toolkit")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="capture a command under a pty")
    pr.add_argument("--rows", type=int, default=24)
    pr.add_argument("--cols", type=int, default=80)
    pr.add_argument("--term", default="xterm-256color")
    pr.add_argument("--seconds", type=float, default=2.0)
    pr.add_argument("--keys", default="")
    pr.add_argument("--snapshot", type=float, default=0.0)
    pr.add_argument("--json", default="")
    pr.add_argument("--raw", action="store_true")
    pr.add_argument("--grids", action="store_true")
    pr.add_argument("cmd", nargs=argparse.REMAINDER)
    pr.set_defaults(func=_cmd_run)

    pn = sub.add_parser("render", help="render a raw capture file to a screen")
    pn.add_argument("file")
    pn.add_argument("--rows", type=int, default=24)
    pn.add_argument("--cols", type=int, default=80)
    pn.add_argument("--json", default="")
    pn.set_defaults(func=_cmd_render)

    pe = sub.add_parser("equiv", help="byte-compare two CLI invocations")
    pe.add_argument("--seconds", type=float, default=5.0)
    pe.add_argument("--input", default="")
    pe.add_argument("cmds", nargs=argparse.REMAINDER)
    pe.set_defaults(func=_cmd_equiv)

    ps = sub.add_parser("selftest", help="validate the toolkit in-place")
    ps.set_defaults(func=_cmd_selftest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command in ("run", "equiv"):
        argv_list = getattr(args, "cmd", None) or getattr(args, "cmds", None)
        if not argv_list or (args.command == "equiv" and "--" not in argv_list):
            print("error: a command is required after --", file=sys.stderr)
            return 2
    rc = args.func(args)
    return 0 if rc is None else rc


if __name__ == "__main__":
    sys.exit(main())
