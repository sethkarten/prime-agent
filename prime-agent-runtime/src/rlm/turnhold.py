"""Cell-scoped turn-hold guards: a model cell cannot park the session.

The discipline contract the prompt teaches: shell work goes through
``bash()`` and waiting is the turn boundary's job — end the turn and let the
[bash-done] notification wake the session. A cell that instead shells out
through ``subprocess``/``os.system`` or sleep-polls holds the kernel for
minutes, invisible to the harness. ``install()`` wraps those entry points so
a call that originates from model-cell code raises :class:`TurnHoldError`
immediately, with the prescribed alternative in the message — the failure
lands in the cell's result instead of silently holding the turn.

The guards are origin-scoped: they fire only on the session's event-loop
(main) thread, and only when the stack from the wrapped call reaches a
``<cell-*>`` frame with no runtime frame in between. So the runtime's own
subprocess spawns (``bash()`` itself spawns on the calling thread), harness
lock retries, MCP servers, watcher threads, and cell-spawned threads (which
park only themselves, not the session) are unaffected.
"""

from __future__ import annotations

import os
import sys
import threading

#: A cell may pace itself, but a longer sleep is a wait: end the turn instead.
SLEEP_LIMIT_S = 1.0

_RLM_DIR = os.path.dirname(os.path.abspath(__file__))


class TurnHoldError(RuntimeError):
    """A model cell tried to hold the session; the message prescribes the alternative."""


_SHELL_MSG = (
    "Shell commands must go through bash(), not subprocess/os.system: subprocess calls "
    "block the kernel, show the user nothing while they run, and spawn processes the "
    "harness cannot see or stop. Start the command with h = bash(cmd); it returns a live "
    "handle immediately. Collect a quick result with r = await h (sub-second commands "
    "only); for anything longer, end the turn and collect the result on the [bash-done] "
    "notification."
)

_SLEEP_MSG = (
    "Do not sleep to wait for background work: sleeping parks the session. End the turn "
    "instead — the [bash-done] notification wakes you when a command finishes (a child "
    "reply or heartbeat wake works the same way); for a scheduled wake, create a "
    "heartbeat with rlm_heartbeat.create(). Sub-second pacing sleeps are allowed."
)


def _from_model_cell() -> bool:
    """True when the wrapped call originates from model-cell code.

    Walks the stack outward from the wrapped call: reaching a `<cell-*>`
    frame means cell code is responsible for the call, a runtime frame first
    means the chain passes through the runtime (bash() spawning, a lock
    retry) and no cell asked for it directly. Off the main thread the session
    is not being held, so the answer is no. Called only from a guard wrapper,
    two frames below the caller.
    """
    if threading.current_thread() is not threading.main_thread():
        return False  # a cell-spawned thread parks only itself, not the session
    frame = sys._getframe(2)
    while frame is not None:
        # The runtime dir check must survive unnormalized import paths
        # (PYTHONPATH entries may carry `..`), so normalize per frame.
        filename = os.path.normpath(frame.f_code.co_filename)
        if filename.startswith("<cell-"):
            return True
        if filename.startswith(_RLM_DIR):
            return False
        frame = frame.f_back
    return False


def _guard_shell(name: str, fn):
    def guarded(*args, **kwargs):
        if _from_model_cell():
            raise TurnHoldError(_SHELL_MSG)
        return fn(*args, **kwargs)

    guarded.__name__ = name
    return guarded


def _guard_sleep(fn):
    def guarded(delay, *args, **kwargs):
        if isinstance(delay, (int, float)) and delay > SLEEP_LIMIT_S and _from_model_cell():
            raise TurnHoldError(_SLEEP_MSG)
        return fn(delay, *args, **kwargs)

    guarded.__name__ = getattr(fn, "__name__", "sleep")
    return guarded


def install() -> None:
    """Install the guards in the kernel process (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True

    import subprocess

    for name in ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        setattr(subprocess, name, _guard_shell(name, getattr(subprocess, name)))
    # The class stays intact (subclassing, isinstance); the guard fires in __init__.
    subprocess.Popen.__init__ = _guard_shell("Popen.__init__", subprocess.Popen.__init__)

    os.system = _guard_shell("os.system", os.system)
    os.popen = _guard_shell("os.popen", os.popen)
    try:
        import pty

        pty.spawn = _guard_shell("pty.spawn", pty.spawn)
    except ImportError:
        pass

    import time

    time.sleep = _guard_sleep(time.sleep)

    import asyncio

    asyncio.sleep = _guard_sleep(asyncio.sleep)


_installed = False
