"""In-place validation: python3 -m tui_probe selftest.

Exercises the VT renderer, pty/pipe capture, and comparison helpers with
synthetic and real subprocess runs. Exits 0 when everything passes.
"""

from __future__ import annotations

import os
import sys
import tempfile

from .capture import run, render
from .compare import equiv, frames_report, screen_diff
from .vt import VtParser

_SELFTESTS = []


def test(fn):
    _SELFTESTS.append(fn)
    return fn


def _py(code):
    return [sys.executable, "-c", code]


# ----------------------------------------------------------------- renderer

@test
def parser_text_and_cursor():
    p = VtParser(4, 20)
    p.feed(b"hello")
    assert p.snapshot().text.splitlines()[0].rstrip() == "hello"
    p2 = VtParser(2, 10)
    p2.feed(b"\x1b[2;5Hx")
    s = p2.snapshot()
    assert s.cell(1, 4) == ("x", -1, -1, 0), s.cell(1, 4)
    assert s.text.splitlines()[1][4] == "x"


@test
def parser_colors():
    p = VtParser(2, 30)
    p.feed(b"\x1b[1;38;5;196mR\x1b[0;38;2;10;20;30mT\x1b[92mG\x1b[0m ")
    s = p.snapshot()
    c0 = s.cell(0, 0)
    assert c0[0] == "R" and c0[1] == 196 and c0[3] & 0x01, c0
    c1 = s.cell(0, 1)
    assert c1[0] == "T" and c1[1] == 0x010A141E, hex(c1[1])
    c2 = s.cell(0, 2)
    assert c2[0] == "G" and c2[1] == 10, c2


@test
def parser_split_sequences():
    p = VtParser(3, 10)
    p.feed(b"\x1b[3;2")
    p.feed(b";1H")
    p.feed(b"ok")
    s = p.snapshot()
    assert s.cell(2, 1)[0] == "o" and s.cell(2, 2)[0] == "k", s.text


@test
def parser_erase_and_scroll():
    p = VtParser(3, 5)
    p.feed(b"aaaaabbbbbccccc")
    p.feed(b"\x1b[2J\x1b[1;1Hhello")
    assert p.snapshot().text.splitlines()[0].rstrip() == "hello"
    p2 = VtParser(2, 5)
    p2.feed(b"one1\r\ntwo2")  # explicit CRLF: row0 then row1
    lines = p2.snapshot().text.splitlines()
    assert lines[0].rstrip() == "one1" and lines[1].rstrip() == "two2", lines
    p3 = VtParser(2, 5)
    p3.feed(b"two2\r\n33333")  # CRLF at the bottom row scrolls one line up
    lines3 = p3.snapshot().text.splitlines()
    assert lines3[0].rstrip() == "two2" and lines3[1].rstrip() == "33333", lines3


@test
def parser_alt_screen_and_modes():
    # Alt-screen enter/leave and cursor hide must not corrupt the grid.
    p = VtParser(2, 10)
    p.feed(b"\x1b[?1049h\x1b[?25l\x1b[1;3Hhide\x1b[?25h\x1b[?1049l")
    s = p.snapshot()
    assert s.cell(0, 2)[0] == "h"
    assert p.cursor_visible is True


@test
def parser_osc_swallowed():
    p = VtParser(1, 10)
    p.feed(b"\x1b]0;title\x07ok")
    assert p.snapshot().text.startswith("ok")


@test
def render_from_file():
    s = render(b"\x1b[2J\x1b[3;4Hz", rows=5, cols=10)
    assert s.cell(2, 3)[0] == "z"


# ------------------------------------------------------------------- capture

@test
def pipe_mode_stdout_stderr():
    code = "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"
    cap = run(_py(code), seconds=5.0, pty=False, term=None)
    assert cap.raw == b"out", cap.raw
    assert cap.stderr == b"err", cap.stderr
    assert cap.exit_status == 3, cap.exit_status


@test
def pipe_mode_stdin_input():
    code = "import sys; data = sys.stdin.read(); sys.stdout.write(data.upper())"
    cap = run(_py(code), seconds=5.0, pty=False, term=None, input=b"abc")
    assert cap.raw == b"ABC", cap.raw


@test
def pty_mode_size_and_merge():
    code = ("import sys\n"
            "line = sys.stdin.readline()\n"
            "print(line.strip() + '|' + str(__import__('os').environ.get('COLUMNS')) + "
            "'|' + str(__import__('os').environ.get('LINES')))")
    cap = run(_py(code), rows=10, cols=42, seconds=5.0, keys=b"x\r", term="xterm-256color")
    text = cap.screen.text
    # The pty echoes the keystroke; the program prints the parsed line below it.
    assert text.splitlines()[0].startswith("x"), text
    assert "|42|10" in text, text


@test
def pty_mode_exit_and_kill():
    cap = run(_py("raise SystemExit(7)"), seconds=5.0, term=None)
    assert cap.exit_status == 7, cap.exit_status
    cap2 = run(_py("import time; time.sleep(30)"), seconds=1.0, term=None)
    assert cap2.killed and cap2.exit_status == -15, cap2


@test
def pty_mode_snapshots():
    code = ("import time, sys\n"
            "for i in range(3):\n"
            "    print('tick', i)\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.25)")
    cap = run(_py(code), seconds=2.0, snapshots=0.2, term=None)
    assert len(cap.frames) >= 3, len(cap.frames)
    report = frames_report(cap)
    assert report[-1]["t"] >= 0.4
    assert any(f["changed_cells"] > 0 for f in report), report


@test
def real_tui_via_ansi():
    # A tiny hand-rolled TUI: clear, paint colored header, loop frames.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(
            "import sys, time\n"
            "w = sys.stdout\n"
            "w.write('\\x1b[2J')\n"
            "for i in range(6):\n"
            "    w.write('\\x1b[1;1H\\x1b[1;38;5;46m frame %d' % i)\n"
            "    w.write('\\x1b[5;1H\\x1b[0m body')\n"
            "    w.flush()\n"
            "    time.sleep(0.15)\n")
        path = fh.name
    try:
        cap = run(_py(open(path).read()), rows=12, cols=40, seconds=1.5,
                  snapshots=0.2)
        line0 = cap.screen.text.splitlines()[0]
        assert "frame" in line0, line0
        counts = cap.screen.color_counts()
        assert counts.get(46, 0) > 0, counts
        assert "body" in cap.screen.text.splitlines()[4]
        rep = frames_report(cap)
        assert any(f["changed_cells"] > 0 for f in rep), rep
    finally:
        os.unlink(path)


# ------------------------------------------------------------------ compare

@test
def screen_diff_works():
    a = render(b"same \x1b[38;5;196mX", rows=1, cols=10)
    b = render(b"same \x1b[38;5;196mY", rows=1, cols=10)
    mismatches, total = screen_diff(a, b)
    assert total == 1 and mismatches[0][2][0] == "X" and mismatches[0][3][0] == "Y"


@test
def equiv_identical_and_differing():
    code_a = "print('v1.2'); print('usage: x')"
    report = equiv(_py(code_a), _py(code_a), seconds=5.0)
    assert report["equal"] and report["stdout_equal"], report
    report2 = equiv(_py(code_a), _py("print('v1.3')"), seconds=5.0)
    assert not report2["equal"] and report2["stdout_diff_at"] is not None, report2


def main():
    failures = 0
    for fn in _SELFTESTS:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(_SELFTESTS) - failures}/{len(_SELFTESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
