"""Pty capture: run a TUI/CLI executable under a controlled terminal.

``run()`` spawns the command in a fresh pseudo-terminal with a fixed size
and TERM, feeds optional keystrokes, reads raw output for a bounded
window, and returns both the raw byte stream and rendered screen
snapshots. The process is terminated at the deadline, so infinite TUIs
(screensavers, editors, dashboards) are safely capturable.

This module uses ``os``-level fork/exec (no ``subprocess``) and is safe
to call from inside a session kernel cell. For captures longer than a few
seconds prefer the CLI form through ``bash()`` and end the turn, so the
kernel is not parked waiting:

    h = bash("python3 -m tui_probe run --json /tmp/cap.json -- ./exe")
    # end the turn; the [bash-done] wake resumes the session

Pipe mode (``pty=False``) is for byte-exact CLI comparisons: stdout and
stderr are captured separately and ``input`` is streamed to stdin.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import struct
import termios
import time

from .vt import VtParser, Screen

_WAIT_KILL_GRACE = 2.0


class Capture:
    """Result of one capture run.

    Attributes:
      raw:         all output bytes (pty mode: merged stream, pipe mode: stdout).
      frames:      Screen snapshots (empty unless ``snapshots`` was set).
      frame_times: seconds offset of each frame from process start.
      screen:      final rendered screen (always present).
      exit_status: process exit code, or None if it could not be collected.
      killed:      True if the deadline stop had to kill the process.
      duration:    seconds the read loop ran.
      stderr:      pipe-mode stderr bytes (pty mode: empty, merged into raw).
      stdout:      alias of raw for pipe mode.
    """

    def __init__(self, raw, frames, frame_times, screen, exit_status, killed,
                 duration, stderr=b""):
        self.raw = raw
        self.frames = frames
        self.frame_times = frame_times
        self.screen = screen
        self.exit_status = exit_status
        self.killed = killed
        self.duration = duration
        self.stderr = stderr
        self.stdout = raw

    def __repr__(self):  # pragma: no cover - debugging helper
        status = self.exit_status if self.exit_status is not None else "?"
        return f"Capture({len(self.raw)}B, exit={status}, frames={len(self.frames)})"


# --------------------------------------------------------------------- spawn

def _set_winsize(fd, rows, cols):
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _resolve_on_path(name):
    """execve has no PATH lookup; resolve a bare command name ourselves."""
    if not name:
        return None
    search = os.environ.get("PATH", os.defpath)
    for d in search.split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, name)
        if os.access(cand, os.X_OK):
            return cand
    return None


def _child_exec(argv, env, cwd, fds, ctty):
    """In the forked child: wire fds, chdir, exec. Never returns."""
    try:
        os.setsid()
        if ctty:  # pty: make the slave the controlling terminal
            fcntl.ioctl(fds[0], termios.TIOCSCTTY, 0)
        for target, source in enumerate(fds):
            os.dup2(source, target)
        for fd in fds:
            if fd > 2:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if cwd:
            os.chdir(cwd)
        os.execve(argv[0], argv, env)
    except BaseException:
        pass
    os._exit(127)


def _spawn(argv, env, cwd, rows, cols, pty, with_stdin):
    """Returns (read_fds, pid, write_fd_or_None)."""
    if pty:
        master, slave = os.openpty()
        _set_winsize(master, rows, cols)
        _set_winsize(slave, rows, cols)
        pid = os.fork()
        if pid == 0:
            os.close(master)
            _child_exec(argv, env, cwd, [slave, slave, slave], True)
        os.close(slave)
        return [master], pid, None
    r_in = w_in = None
    if with_stdin:
        r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    r_err, w_err = os.pipe()
    pid = os.fork()
    if pid == 0:
        for fd in (r_out, r_err):
            os.close(fd)
        if r_in is not None:
            os.close(w_in)
            _child_exec(argv, env, cwd, [r_in, w_out, w_err], False)
        else:
            devnull = os.open(os.devnull, os.O_RDONLY)
            _child_exec(argv, env, cwd, [devnull, w_out, w_err], False)
    for fd in (w_out, w_err):
        os.close(fd)
    if r_in is not None:
        os.close(r_in)
    return [r_out, r_err], pid, w_in


def _decode_status(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return None


def _reap(pid):
    """Collect the child if it already exited; else stop it and collect.

    Returns (status, killed): killed is True only when we had to stop a
    still-live process ourselves.
    """
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return _decode_status(status), False
    except ChildProcessError:
        return None, False
    killed = True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        killed = False
    deadline = time.monotonic() + _WAIT_KILL_GRACE
    while True:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return _decode_status(status), killed
        except ChildProcessError:
            return None, killed
        if time.monotonic() > deadline:
            try:
                os.kill(pid, signal.SIGKILL)
                killed = True
            except ProcessLookupError:
                pass
            try:
                _, status = os.waitpid(pid, 0)
                return _decode_status(status), killed
            except ChildProcessError:
                return None, killed
        time.sleep(0.02)


# ----------------------------------------------------------------------- run

def run(cmd, *args, rows=24, cols=80, term="xterm-256color", seconds=2.0,
        keys=b"", key_after=0.3, env=None, cwd=None, pty=True, input=None,
        snapshots=0.0):
    """Run ``cmd`` under a fixed terminal and capture its output.

    Args:
      cmd: the program; a sequence (used as-is) or a string (word-split).
      *args: extra arguments (convenience: ``run("./exe", "-V")``).
      rows/cols: terminal size reported to the program (pty mode).
      term: TERM value (``None`` keeps the inherited environment).
      seconds: read window; the process is terminated afterwards.
      keys: bytes written to the program stdin ``key_after`` seconds in.
      env: extra environment variables merged over the inherited ones.
      cwd: working directory for the child.
      pty: True for TUI captures; False runs on pipes for byte-exact CLI
           comparisons (``input`` is then written to stdin up front).
      input: stdin bytes, pipe mode only; must fit the OS pipe buffer.
      snapshots: if > 0, take a Screen frame every N seconds.

    Returns a :class:`Capture`.
    """
    if isinstance(cmd, str):
        argv = cmd.split() + [str(a) for a in args]
    else:
        argv = [str(part) for part in cmd] + [str(a) for a in args]
    if not argv:
        raise ValueError("cmd is empty")
    if os.path.sep in argv[0]:
        argv[0] = os.path.abspath(argv[0])
        if not os.path.exists(argv[0]):
            raise FileNotFoundError(argv[0])
    else:
        argv[0] = _resolve_on_path(argv[0])
        if argv[0] is None:
            raise FileNotFoundError(f"{cmd!r}: command not found on PATH")

    child_env = dict(os.environ)
    child_env.pop("LINES", None)
    child_env.pop("COLUMNS", None)
    if env:
        child_env.update({str(k): str(v) for k, v in env.items()})
    if term is not None:
        child_env["TERM"] = term
    if pty:
        child_env["LINES"] = str(rows)
        child_env["COLUMNS"] = str(cols)

    start = time.monotonic()
    fds, pid, stdin_w = _spawn(argv, child_env, cwd, rows, cols, pty,
                               input is not None)
    parser = VtParser(rows, cols)
    raw = bytearray()
    stderr = bytearray()
    frames = []
    frame_times = []
    keys_sent = not keys
    killed = False
    pending_input = bytearray(input) if input is not None else bytearray()

    try:
        while True:
            now = time.monotonic()
            remaining = seconds - (now - start)
            if remaining <= 0:
                break
            if not keys_sent and now - start >= key_after:
                if keys and pty:
                    try:
                        os.write(fds[0], keys)
                    except OSError:
                        pass
                keys_sent = True
            if snapshots > 0 and (not frames or now - start >= frame_times[-1] + snapshots):
                frames.append(parser.snapshot())
                frame_times.append(round(now - start, 3))
            write_fds = [stdin_w] if (stdin_w is not None and pending_input) else []
            try:
                ready, wready, _ = select.select(fds, write_fds, [], min(0.05, remaining))
            except (InterruptedError, OSError, ValueError):
                break
            if wready:
                try:
                    n = os.write(stdin_w, bytes(pending_input[:65536]))
                except OSError:
                    n = 0
                del pending_input[:n]
                if not pending_input:
                    try:
                        os.close(stdin_w)
                    except OSError:
                        pass
                    stdin_w = None
            if not ready:
                try:
                    wpid, _ = os.waitpid(pid, os.WNOHANG)
                    if wpid == pid:
                        _drain(fds, raw, stderr, parser, pty, 0.05)
                        break
                except ChildProcessError:
                    break
                continue
            alive = False
            for fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    if pty:
                        raw.extend(chunk)
                        parser.feed(chunk)
                    elif fd == fds[0]:
                        raw.extend(chunk)
                    else:
                        stderr.extend(chunk)
                    alive = True
                else:
                    fds.remove(fd)
            if not fds:
                break
            if not alive:
                continue
        _drain(fds, raw, stderr, parser, pty, 0.05)
        frames.append(parser.snapshot())
        frame_times.append(round(time.monotonic() - start, 3))
    finally:
        exit_status, killed = _reap(pid)
        if stdin_w is not None:
            try:
                os.close(stdin_w)
            except OSError:
                pass
        for fd in list(fds):
            try:
                os.close(fd)
            except OSError:
                pass

    return Capture(bytes(raw), frames, frame_times, frames[-1], exit_status,
                   killed, round(time.monotonic() - start, 3),
                   stderr=bytes(stderr))


def _drain(fds, raw, stderr, parser, pty, budget):
    deadline = time.monotonic() + budget
    while fds and time.monotonic() < deadline:
        try:
            ready, _, _ = select.select(fds, [], [], 0.01)
        except (OSError, ValueError):
            return
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if chunk:
                if pty:
                    raw.extend(chunk)
                    parser.feed(chunk)
                elif fd == fds[0]:
                    raw.extend(chunk)
                else:
                    stderr.extend(chunk)
            else:
                try:
                    fds.remove(fd)
                except ValueError:
                    pass


def render(raw, rows=24, cols=80) -> Screen:
    """Render raw terminal bytes to a final Screen (no process involved)."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    p = VtParser(rows, cols)
    p.feed(raw)
    return p.snapshot()
