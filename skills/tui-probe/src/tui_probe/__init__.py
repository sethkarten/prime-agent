"""tui_probe: capture and characterize TUI/CLI executables.

A zero-dependency toolkit for behavioral characterization of terminal
programs: run them under a controlled pty, render the escape stream to
screen grids, diff screens, and compare implementations. See SKILL.md
for the workflow.
"""

from .capture import Capture, render, run
from .compare import color_name, compare_frames, equiv, frames_report, screen_diff
from .vt import BOLD, BLINK, FAINT, ITALIC, REVERSE, STRIKE, UNDERLINE, Screen, VtParser, truecolor_rgb

__all__ = [
    "Capture", "Screen", "VtParser", "run", "render",
    "screen_diff", "equiv", "frames_report", "compare_frames",
    "color_name", "truecolor_rgb",
    "BOLD", "FAINT", "ITALIC", "UNDERLINE", "BLINK", "REVERSE", "STRIKE",
]

__version__ = "0.1.0"
