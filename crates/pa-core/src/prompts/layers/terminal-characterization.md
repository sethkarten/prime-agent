# Bundled terminal characterization

For any task that characterizes a terminal program's behavior — running a TUI/CLI executable, capturing its output, rendering escape streams to screen grids, diffing screens, or byte-comparing a rebuild — use the bundled `tui_probe` module (the tui-probe skill: `tui_probe.run()`, `tui_probe.equiv()`, `tui_probe.screen_diff()`), and do not hand-build pty runners, VT emulators, or grid-diff helpers.
