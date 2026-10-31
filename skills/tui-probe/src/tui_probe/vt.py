"""VT/ANSI screen renderer for captured terminal output.

Feeds raw bytes from a pty (or pipe) through a compact VT100/xterm state
machine and renders the current screen: character grid plus per-cell
foreground, background, and attribute bits. The covered subset is the one
real TUIs (ncurses and friends) actually emit: cursor movement, erase and
insert/edit operations, scrolling and scroll regions, alt-screen and
cursor-visibility modes, DECSC/DECRC save/restore, and SGR color
(16-color, 256-color, and truecolor). OSC/DCS/APC sequences are consumed
and dropped.

The renderer deliberately does not model the physical terminal's quirks
beyond this subset; sequences it does not know are swallowed so the grid
stays usable.
"""

from __future__ import annotations

# Attribute bit positions (SGR 1..9 / 21..29).
BOLD = 0x01
FAINT = 0x02
ITALIC = 0x04
UNDERLINE = 0x08
BLINK = 0x10
REVERSE = 0x20
HIDDEN = 0x40
STRIKE = 0x80

# Default cell state: blank char, default colors (-1), no attributes.
DEFAULT_FG = -1
DEFAULT_BG = -1

# Parser states.
_GROUND, _ESC, _CSI, _OSC, _STR, _ESC_CHARSET = range(6)


def _truecolor(r: int, g: int, b: int) -> int:
    return 0x01000000 | (r << 16) | (g << 8) | b


def _is_truecolor(code: int) -> bool:
    return bool(code & 0x01000000)


def truecolor_rgb(code: int):
    """Split a truecolor cell code into (r, g, b); None for palette codes."""
    if not _is_truecolor(code):
        return None
    return ((code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF)


class Screen:
    """An immutable snapshot of the rendered screen.

    Rows are stored as compact strings; per-cell color state lives in
    parallel ``array('i')`` rows (foreground/background: -1 default,
    0-255 palette, 0x01000000|rgb truecolor) plus one ``bytes`` attr row.
    Access cells with :meth:`cell` (row, col) -> ``(char, fg, bg, attr)``.
    """

    def __init__(self, rows, cols, chars, fg, bg, attr):
        self.rows = rows
        self.cols = cols
        self.chars = chars      # list[str], one per row
        self.fg = fg            # list[array('i')], len == cols per row
        self.bg = bg
        self.attr = attr        # list[bytes]

    @property
    def text(self) -> str:
        """The screen as plain text, rows joined with newlines."""
        return "\n".join(self.chars)

    def cell(self, row: int, col: int):
        """(char, fg, bg, attr) at row/col."""
        return (self.chars[row][col], self.fg[row][col], self.bg[row][col],
                self.attr[row][col])

    def color_counts(self):
        """Counter over foreground color codes across all cells."""
        from collections import Counter
        c = Counter()
        for row in self.fg:
            c.update(row)
        return c

    def __repr__(self):  # pragma: no cover - debugging helper
        return f"Screen({self.rows}x{self.cols})"


class VtParser:
    """Incremental VT renderer: feed() bytes, snapshot() frozen screens.

    Handles partial escape sequences across feed() boundaries, so chunked
    pty reads render correctly.
    """

    def __init__(self, rows=24, cols=80):
        self.rows = rows
        self.cols = cols
        self._grid = [[(" ", DEFAULT_FG, DEFAULT_BG, 0)] * cols for _ in range(rows)]
        self._cur_fg = DEFAULT_FG
        self._cur_bg = DEFAULT_BG
        self._cur_attr = 0
        self.row = 0
        self.col = 0
        self._saved = (0, 0, DEFAULT_FG, DEFAULT_BG, 0)
        self._top = 0
        self._bottom = rows - 1
        self._wrap_pending = False
        self._state = _GROUND
        self._pending = bytearray()
        self.cursor_visible = True

    # ------------------------------------------------------------------ feed

    def feed(self, data: bytes):
        for byte in data:
            if self._state == _GROUND:
                self._ground(byte)
            elif self._state == _ESC:
                self._esc(byte)
            elif self._state == _CSI:
                self._csi(byte)
            elif self._state == _OSC:
                self._osc(byte)
            elif self._state == _STR:
                self._str(byte)
            elif self._state == _ESC_CHARSET:
                self._state = _GROUND
        # end of chunk: nothing to flush (all states hold full bytes)

    # ---------------------------------------------------------------- ground

    def _ground(self, b: int):
        if b == 0x1B:
            self._pending.clear()
            self._state = _ESC
        elif b == 0x0D:
            self.col = 0
            self._wrap_pending = False
        elif b in (0x0A, 0x0B, 0x0C):
            self._linefeed()
        elif b == 0x08:
            if self.col > 0:
                self.col -= 1
            self._wrap_pending = False
        elif b == 0x09:
            nxt = min(self.cols - 1, (self.col // 8 + 1) * 8)
            self.col = nxt
        elif b == 0x07:
            pass
        elif b == 0x00:
            pass
        elif 0x20 <= b < 0x7F:
            self._put(chr(b))
        elif b >= 0x80:
            self._pending.append(b)
            try:
                s = bytes(self._pending).decode("utf-8")
            except UnicodeDecodeError:
                if len(self._pending) >= 4:
                    self._pending.clear()
                return
            self._pending.clear()
            for ch in s:
                self._put(ch)

    def _put(self, ch: str):
        if self._wrap_pending:
            self._linefeed()
            self.col = 0
            self._wrap_pending = False
        cell = (ch, self._cur_fg, self._cur_bg, self._cur_attr)
        self._grid[self.row][self.col] = cell
        if self.col == self.cols - 1:
            self._wrap_pending = True
        else:
            self.col += 1

    def _linefeed(self):
        if self.row == self._bottom:
            self._scroll_up()
        elif self.row < self.rows - 1:
            self.row += 1

    def _scroll_up(self):
        del self._grid[self._top]
        blank = [(" ", DEFAULT_FG, DEFAULT_BG, 0)] * self.cols
        self._grid.insert(self._bottom, blank)

    def _scroll_down(self):
        del self._grid[self._bottom]
        blank = [(" ", DEFAULT_FG, DEFAULT_BG, 0)] * self.cols
        self._grid.insert(self._top, blank)

    # ------------------------------------------------------------------- esc

    def _esc(self, b: int):
        c = chr(b)
        if c == "[":
            self._state = _CSI
            self._params = bytearray()
            self._inter = bytearray()
        elif c in "]PX^_":
            self._state = _OSC if c == "]" else _STR
            self._str_term = bytearray()
        elif c in "()*+":
            # charset designator: swallow the following single char
            self._state = _ESC_CHARSET
        elif c == "7":      # DECSC
            self._saved = (self.row, self.col, self._cur_fg, self._cur_bg, self._cur_attr)
            self._state = _GROUND
        elif c == "8":      # DECRC
            r, c2, fg, bg, at = self._saved
            self.row, self.col = min(r, self.rows - 1), min(c2, self.cols - 1)
            self._cur_fg, self._cur_bg, self._cur_attr = fg, bg, at
            self._state = _GROUND
        elif c == "D":      # IND
            self._linefeed()
            self._state = _GROUND
        elif c == "E":      # NEL
            self._linefeed()
            self.col = 0
            self._state = _GROUND
        elif c == "M":      # RI
            if self.row == self._top:
                self._scroll_down()
            elif self.row > 0:
                self.row -= 1
            self._state = _GROUND
        elif c == "c":      # RIS full reset
            self.__init__(self.rows, self.cols)
        elif c in "=><":
            self._state = _GROUND
        else:
            self._state = _GROUND

    # ------------------------------------------------------------------- csi

    def _csi(self, b: int):
        c = chr(b)
        if 0x30 <= b <= 0x3F:            # parameter bytes
            self._params.append(b)
        elif 0x20 <= b <= 0x2F:          # intermediate bytes
            self._inter.append(b)
        elif 0x40 <= b <= 0x7E:          # final byte: dispatch
            self._csi_dispatch(c)
            self._state = _GROUND
        else:
            # Anything else is malformed: back to ground.
            self._state = _GROUND

    def _params_list(self, default=0, count=1):
        raw = bytes(self._params)
        parts = raw.split(b";") if raw else [b""]
        vals = []
        for p in parts:
            digits = bytes(ch for ch in p if 0x30 <= ch <= 0x39)
            vals.append(int(digits) if digits else default)
        while len(vals) < count:
            vals.append(default)
        return vals

    def _csi_dispatch(self, final: str):
        p = self._params_list()
        if final == "A":      # CUU
            n = max(1, p[0])
            self.row = max(0, self.row - n)
            self._wrap_pending = False
        elif final == "B":    # CUD
            n = max(1, p[0])
            self.row = min(self.rows - 1, self.row + n)
            self._wrap_pending = False
        elif final == "C":    # CUF
            n = max(1, p[0])
            self.col = min(self.cols - 1, self.col + n)
            self._wrap_pending = False
        elif final == "D":    # CUB
            n = max(1, p[0])
            self.col = max(0, self.col - n)
            self._wrap_pending = False
        elif final == "E":    # CNL
            n = max(1, p[0])
            self.row = min(self.rows - 1, self.row + n)
            self.col = 0
            self._wrap_pending = False
        elif final == "F":    # CPL
            n = max(1, p[0])
            self.row = max(0, self.row - n)
            self.col = 0
            self._wrap_pending = False
        elif final == "G":    # CHA
            n = max(1, p[0])
            self.col = min(self.cols - 1, n - 1)
            self._wrap_pending = False
        elif final == "d":    # VPA
            n = max(1, p[0])
            self.row = min(self.rows - 1, n - 1)
            self._wrap_pending = False
        elif final in ("H", "f"):   # CUP
            p = self._params_list(count=2)
            r = max(1, p[0])
            c = max(1, p[1])
            self.row = min(self.rows - 1, r - 1)
            self.col = min(self.cols - 1, c - 1)
            self._wrap_pending = False
        elif final == "J":    # ED
            mode = p[0] if self._params else 0
            blank = (" ", DEFAULT_FG, DEFAULT_BG, 0)
            if mode == 0:
                for c in range(self.col, self.cols):
                    self._grid[self.row][c] = blank
                for r in range(self.row + 1, self.rows):
                    self._grid[r] = [blank] * self.cols
            elif mode == 1:
                for r in range(0, self.row):
                    self._grid[r] = [blank] * self.cols
                for c in range(0, self.col + 1):
                    self._grid[self.row][c] = blank
            else:
                for r in range(self.rows):
                    self._grid[r] = [blank] * self.cols
        elif final == "K":    # EL
            mode = p[0] if self._params else 0
            blank = (" ", DEFAULT_FG, DEFAULT_BG, 0)
            if mode == 0:
                for c in range(self.col, self.cols):
                    self._grid[self.row][c] = blank
            elif mode == 1:
                for c in range(0, self.col + 1):
                    self._grid[self.row][c] = blank
            else:
                self._grid[self.row] = [blank] * self.cols
        elif final == "L":    # IL
            n = max(1, p[0])
            for _ in range(min(n, self._bottom - self.row + 1)):
                del self._grid[self._bottom]
                self._grid.insert(self.row, [self._blank()] * self.cols)
        elif final == "M":    # DL
            n = max(1, p[0])
            for _ in range(min(n, self._bottom - self.row + 1)):
                del self._grid[self.row]
                self._grid.insert(self._bottom, [self._blank()] * self.cols)
        elif final == "@":    # ICH
            n = max(1, p[0])
            row = self._grid[self.row]
            n = min(n, self.cols - self.col)
            row[:] = row[:self.col] + [self._blank()] * n + row[self.col:self.cols - n]
        elif final == "P":    # DCH
            n = max(1, p[0])
            row = self._grid[self.row]
            n = min(n, self.cols - self.col)
            del row[self.col:self.col + n]
            row.extend([self._blank()] * n)
        elif final == "X":    # ECH
            n = min(max(1, p[0]), self.cols - self.col)
            for c in range(self.col, self.col + n):
                self._grid[self.row][c] = self._blank()
        elif final == "S":    # SU
            n = max(1, p[0])
            for _ in range(n):
                self._scroll_up()
        elif final == "T":    # SD
            n = max(1, p[0])
            for _ in range(n):
                self._scroll_down()
        elif final == "r":    # DECSTBM
            if not self._params:
                self._top, self._bottom = 0, self.rows - 1
                self.row = self.col = 0
            else:
                p = self._params_list(count=2)
                top = max(1, p[0])
                bot = p[1] if p[1] else self.rows
                if top < bot <= self.rows:
                    self._top, self._bottom = top - 1, bot - 1
                    self.row = self.col = 0
        elif final == "m":    # SGR
            self._sgr()
        elif final == "h" or final == "l":
            self._mode(final == "h", p)
        # else: swallowed (n/q/c/t/g/...)

    def _blank(self):
        return (" ", DEFAULT_FG, DEFAULT_BG, 0)

    def _mode(self, set_flag: bool, p):
        raw = bytes(self._params)
        if raw.startswith(b"?"):
            if 25 in p:
                self.cursor_visible = set_flag
            # 7 (wrap), 1049/47/1047 (alt screen): wrap stays on; alt-screen
            # switching is not modeled (both screens render the same grid).

    # ------------------------------------------------------------------- sgr

    def _sgr(self):
        raw = bytes(self._params)
        if not raw:
            raw = b"0"
        parts = raw.split(b";")
        i = 0
        while i < len(parts):
            def val(j):
                if j < len(parts):
                    d = bytes(ch for ch in parts[j] if 0x30 <= ch <= 0x39)
                    return int(d) if d else 0
                return 0
            n = val(i)
            if n == 0:
                self._cur_fg, self._cur_bg, self._cur_attr = DEFAULT_FG, DEFAULT_BG, 0
            elif n == 1:
                self._cur_attr |= BOLD
            elif n == 2:
                self._cur_attr |= FAINT
            elif n == 3:
                self._cur_attr |= ITALIC
            elif n == 4:
                self._cur_attr |= UNDERLINE
            elif n == 5:
                self._cur_attr |= BLINK
            elif n == 7:
                self._cur_attr |= REVERSE
            elif n == 8:
                self._cur_attr |= HIDDEN
            elif n == 9:
                self._cur_attr |= STRIKE
            elif n in (21, 22):
                self._cur_attr &= ~(BOLD | FAINT)
            elif n == 23:
                self._cur_attr &= ~ITALIC
            elif n == 24:
                self._cur_attr &= ~UNDERLINE
            elif n == 25:
                self._cur_attr &= ~BLINK
            elif n == 27:
                self._cur_attr &= ~REVERSE
            elif n == 28:
                self._cur_attr &= ~HIDDEN
            elif n == 29:
                self._cur_attr &= ~STRIKE
            elif 30 <= n <= 37:
                self._cur_fg = n - 30
            elif n == 39:
                self._cur_fg = DEFAULT_FG
            elif 40 <= n <= 47:
                self._cur_bg = n - 40
            elif n == 49:
                self._cur_bg = DEFAULT_BG
            elif 90 <= n <= 97:
                self._cur_fg = n - 90 + 8
            elif 100 <= n <= 107:
                self._cur_bg = n - 100 + 8
            elif n in (38, 48):
                target = None
                if i + 1 < len(parts):
                    kind = val(i + 1)
                    if kind == 5 and i + 2 < len(parts):
                        target = val(i + 2) & 0xFF
                        i += 2
                    elif kind == 2 and i + 4 < len(parts):
                        target = _truecolor(val(i + 2), val(i + 3), val(i + 4))
                        i += 4
                if target is not None:
                    if n == 38:
                        self._cur_fg = target
                    else:
                        self._cur_bg = target
            i += 1

    # ------------------------------------------------------------ osc / str

    def _osc(self, b: int):
        if b == 0x07:
            self._state = _GROUND
        elif b == 0x1B:
            self._state = _STR
            self._str_term = bytearray()

    def _str(self, b: int):
        if b == 0x1B:
            self._str_term = bytearray([0x1B])
        elif self._str_term and b == 0x5C:      # ST: ESC backslash
            self._state = _GROUND
            self._str_term = bytearray()
        elif self._str_term:
            self._str_term.clear()

    # -------------------------------------------------------------- snapshot

    def snapshot(self) -> Screen:
        """Freeze the current screen into a compact immutable Screen."""
        from array import array
        chars = []
        fg = []
        bg = []
        attr = []
        for row in self._grid:
            row_chars = []
            row_fg = array("i", bytes(4 * self.cols))
            row_bg = array("i", bytes(4 * self.cols))
            row_attr = bytearray(self.cols)
            for idx, (ch, f, b_, a) in enumerate(row):
                row_chars.append(ch)
                row_fg[idx] = f
                row_bg[idx] = b_
                row_attr[idx] = a
            chars.append("".join(row_chars))
            fg.append(row_fg)
            bg.append(row_bg)
            attr.append(bytes(row_attr))
        return Screen(self.rows, self.cols, chars, fg, bg, attr)
