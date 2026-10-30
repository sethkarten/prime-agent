from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
import sys
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from rlm import bash, turnhold  # noqa: E402

_CELL_SEQ = 0


def run_cell(code: str, ns: dict):
    """Execute code the way the REPL does: compiled under a `<cell-N>`
    filename, top-level await included."""
    global _CELL_SEQ
    _CELL_SEQ += 1
    filename = f"<cell-{_CELL_SEQ}>"
    flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    code_obj = compile(ast.parse(code, filename), filename, "exec", flags=flags)
    value = eval(code_obj, ns)
    if code_obj.co_flags & inspect.CO_COROUTINE:
        value = asyncio.run(value)
    return value


class TurnHoldGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        turnhold.install()

    def test_shell_from_cell_raises_with_prescription(self):
        with self.assertRaises(turnhold.TurnHoldError) as ctx:
            run_cell("import subprocess\nsubprocess.run(['true'])", {})
        self.assertIn("bash()", str(ctx.exception))

    def test_os_system_from_cell_raises(self):
        with self.assertRaises(turnhold.TurnHoldError):
            run_cell("import os\nos.system('true')", {})

    def test_bash_from_cell_still_spawns(self):
        # bash() itself spawns through subprocess.Popen on the calling
        # thread: the runtime frame in the chain must exempt it.
        run_cell(
            "import asyncio\n"
            "r = await bash('printf ok')\n"
            "assert r.exit_code == 0 and r.output == 'ok', r\n",
            {"bash": bash},
        )

    def test_shell_outside_cells_passes_through(self):
        self.assertEqual(subprocess.run(["true"]).returncode, 0)
        self.assertEqual(os.system("true"), 0)

    def test_long_sleep_from_cell_raises(self):
        with self.assertRaises(turnhold.TurnHoldError) as ctx:
            run_cell("import time\ntime.sleep(30)", {})
        self.assertIn("End the turn", str(ctx.exception))

    def test_long_asyncio_sleep_from_cell_raises(self):
        with self.assertRaises(turnhold.TurnHoldError):
            run_cell("import asyncio\nawait asyncio.sleep(30)", {})

    def test_short_pacing_sleeps_pass(self):
        run_cell("import time\ntime.sleep(0.05)", {})
        run_cell("import asyncio\nawait asyncio.sleep(0)", {})

    def test_sleep_outside_cells_passes_through(self):
        time.sleep(1.1)  # a plain non-cell call: no `<cell-*>` frame in the chain

    def test_thread_spawned_by_cell_may_sleep(self):
        # A cell-created thread parks only itself; the turn is not held.
        run_cell(
            "import threading, time\n"
            "done = threading.Event()\n"
            "def waiter():\n"
            "    time.sleep(1.2)\n"
            "    done.set()\n"
            "threading.Thread(target=waiter, daemon=True).start()\n"
            "assert not done.is_set()\n",
            {},
        )

    def test_install_is_idempotent(self):
        turnhold.install()
        turnhold.install()
        self.assertEqual(subprocess.run(["true"]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
