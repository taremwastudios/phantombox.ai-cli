"""Welcome-screen branding: ASCII logo and icon glyph metrics.

The logo is modelled on ``image3.png``: a filled isometric "phantom box" with a
ghost outline escaping from the top-right corner and three signal arcs radiating
from it, followed by a lowercase, filled wordmark with a cast shadow.

Rendering rules
---------------
* Every cell is either *filled* (``█``, ``▓``, ``▒``) or *empty* (space). The
  shadow pass uses the darker ramps so the wordmark reads as raised.
* Lines are centred because block glyphs are double-width in most terminals;
  ragged line widths look broken.
* An ASCII-only fallback is used when the terminal encoding cannot represent
  block characters or the terminal is too narrow.

Icon metrics
------------
``LEFT_ICONS`` / ``RIGHT_ICONS`` are fixed-width ASCII glyph stacks that flank
the central wordmark. Each icon occupies ``ICON_SIZE`` rows so the two columns
stay vertically aligned, and all glyphs stay in the plain ASCII range so they
render on any terminal.
"""

from __future__ import annotations

import unicodedata

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Hero logo
# ---------------------------------------------------------------------------
#
# Minimal, lowercase "phantombox.ai": small filled glyphs on a shared
# baseline with a one-row drop shadow. Kept deliberately compact.

WORDMARK_WORD = "phantombox.ai"
WORDMARK_GAP = 1
_WORDMARK_H = 5

# Each glyph: a list of rows, one string per row, all rows the same width.
# Widths may differ per glyph ('m' is wider); the pitch is glyph width + GAP.
_WORDMARK_GLYPHS = {
    "p": ["███", "█ █", "███", "█  ", "█  "],
    "h": ["█ █", "█ █", "███", "█ █", "█ █"],
    "a": ["   ", "███", "█ █", "███", "█ █"],
    "n": ["█ █", "███", "█ █", "█ █", "█ █"],
    "t": [" █ ", " █ ", "███", " █ ", " █ "],
    "o": ["   ", "███", "█ █", "█ █", "███"],
    "m": ["█ █ █", "█████", "█ █ █", "█ █ █"],
    "b": ["█  ", "███", "█ █", "█ █", "███"],
    "x": ["█ █", "█ █", " █ ", "█ █", "█ █"],
    ".": [" ", " ", " ", " ", "█"],
    "i": ["█", " ", "█", "█", "█"],
}


def _wordmark_rows() -> list[str]:
    """Compact filled wordmark + one drop-shadow row beneath."""
    rows = [""] * _WORDMARK_H
    for i, ch in enumerate(WORDMARK_WORD):
        glyph = _WORDMARK_GLYPHS.get(ch, [" "] * _WORDMARK_H)
        w = max(len(r) for r in glyph)
        norm = [r.ljust(w) for r in glyph] + [" " * w] * (_WORDMARK_H - len(glyph))
        for r in range(_WORDMARK_H):
            rows[r] += norm[r]
            if i != len(WORDMARK_WORD) - 1:
                rows[r] += " " * WORDMARK_GAP
    width = max(len(r) for r in rows)
    rows = [r.ljust(width) for r in rows]
    silhouette = "".join(
        "▒" if any(r[c] != " " for r in rows) else " " for c in range(width)
    )
    return rows + [(" " + silhouette).rstrip()]



def logo_frame(lit: int = 0, trail: int = 0) -> list[str]:
    """One static frame (API kept for HeroLogo)."""
    return _wordmark_rows()


def logo_frames() -> list[list[str]]:
    return [logo_frame()]


def logo_plain_rows() -> list[str]:
    return [WORDMARK_WORD]



# Narrowest terminal width at which the wordmark fits without wrapping.
LOGO_MIN_WIDTH = 20


def supports_blocks_lenient() -> bool:
    """True unless every candidate encoding is pure ASCII.

    Unlike supports_blocks() this does not demand that the codepage can *encode*
    the block glyphs: Windows consoles (cp1252/cp437) draw █ ▓ ▒ fine through the
    Unicode API even when the legacy codepage cannot encode them, so rejecting
    them hid the filled logo behind the plain-text fallback.
    """
    import locale
    import sys

    candidates = []
    for stream in (getattr(sys, "stdout", None), getattr(sys, "__stdout__", None)):
        enc = getattr(stream, "encoding", None)
        if enc:
            candidates.append(enc)
    try:
        preferred = locale.getpreferredencoding(False)
        if preferred:
            candidates.append(preferred)
    except Exception:
        pass
    if not candidates:
        return True
    for enc in candidates:
        norm = enc.lower().replace("-", "").replace("_", "")
        if norm not in ("ascii", "usascii"):
            return True
    return False


ICON_SIZE = 3
ICON_GUTTER = 13
ICON_HERO_WIDTH = 46

LEFT_ICONS = [
    ("brain",   ["   .-""-.   ", "  ( o  o )  ", "   `-..-'   "]),
    ("cloud",   ["    .---.   ", "  .(     ). ", " (___.--'_)  "]),
    ("bolt",    ["    __/     ", "   /__      ", "  / /       "]),
    ("chip",    ["  .-------. ", "  | [###] | ", "  '-------' "]),
    ("link",    ["   o---o    ", "  /     \\   ", " o       o  "]),
]

RIGHT_ICONS = [
    ("signal",  ["  \\  |  /   ", "   \\ | /    ", "  -- + --   "]),
    ("globe",   ["    .-.     ", "  (  *  )   ", "    '-'     "]),
    ("shield",  ["   .----.   ", "  | [**] |  ", "   '----'   "]),
    ("wave",    ["   /\\/\\     ", "  /    \\    ", " /      \\   "]),
    ("spark",   ["    /\\      ", "   /  \\     ", "   \\  /     "]),
]


def icon_column(icons, rows: int = ICON_SIZE, gutter: int = ICON_GUTTER):
    """Lay icons into vertically stacked fixed-width lines."""
    out = []
    for _name, glyphs in icons:
        for r in range(rows):
            line = glyphs[r] if r < len(glyphs) else ""
            out.append(line[:gutter].ljust(gutter))
    return out

# ---------------------------------------------------------------------------
# Mark: filled isometric box + escaping phantom + signal arcs
# ---------------------------------------------------------------------------
#
# The box is drawn as three faces:
#   top   -> ▀ ▄ and horizontal runs (lit)
#   side  -> █ (solid)  with ▓ shading toward the bottom
#   base  -> ▒ (darkest)
# The phantom floats above/right of the box so the two shapes never collide,
# and three concentric arcs radiate from its head.

_MARK = [
    "                  ======================    \\  |  / ",
    "               ///                    ///    \\ | /  ",
    "            ///                        ///     --+-- ",
    "         ///                            ///    .----. ",
    "      /////////////////////////////////////    |____| ",
    "      ||                                  ||          ",
    "      ||                                  ||          ",
    "      ||                                  ||          ",
    "      ||                                  ||          ",
    "      ||                                   ||         ",
    "       ||                                   ||        ",
    "        ||                                   ||       ",
    "         ======================================       ",
]

# Filled variant: the reference image is a *solid* mark, so the faces are filled
# rather than outlined. Built as three explicit isometric faces:
#
#        __________                 top face    (lit, whitespace interior)
#      /|         /|               ─────────────────────────────────────
#     / |        / |     right   left face   █ solid, lit
#    /__|_______/  |     face    right face  ▓ shaded (turned away from light)
#    |  |       |  |              base       ▒ darkest
#    |  |_______|__|
#
# The phantom floats in free space at the top-right so it never overlaps the
# cube, and the three signal arcs radiate from its head.
_MARK_PLAIN = [
    "          ___________________                   ",
    "         /                  /\\           \\  |  /",
    "        /                  /  \\           \\ | / ",
    "       /                  /____\\           --+--",
    "      /                  /     /                ",
    "     /__________________/     /                 ",
    "     |                  |    /                  ",
    "     |                  |   /                   ",
    "     |                  |  /                    ",
    "     |__________________|/                      ",
]

_MARK_FILLED = [
    "            ████████████████████████          ╲ │ ╱  ",
    "          ████████████████████████████        ─ + ─  ",
    "        ████████████████████████████████      ╱ │ ╲  ",
    "      ████████████████████████████████████          ",
    "      ████████████████████████████████████     ▄▄▄▄  ",
    "      ████████████████████████████████▒▒▒▒▒    █   █ ",
    "      ████████████████████████████████▒▒▒▒▒    █    █",
    "      ████████████████████████████████▒▒▒▒▒    █▄▄▄▄█",
    "      ████████████████████████████████▒▒▒▒▒          ",
    "      ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▒▒▒▒▒          ",
]


# Lowercase wordmark on a shared baseline, built from FULL blocks only.
#
# Half-block glyphs (▄ ▀ ▒ ▓) render as detached fragments in many terminal
# fonts, which made an earlier version unreadable. Every glyph below is 5 rows
# tall using █ for strokes and spaces for counters, so it stays solid.
_WORD = "phantombox"

# Glyph design rules (keep these invariants or the wordmark breaks):
#   * every glyph is exactly GLYPH_W cells wide on every row, followed by a
#     1-cell gap added by _wordmark() -> a uniform 6-cell pitch;
#   * only █ and spaces are used (no ▄▀▒▓, which render as stripes/fragments);
#   * 5 rows tall, all letters sharing one baseline.
GLYPH_W = 5
GLYPH_GAP = 2   # blank cells between letters so adjacent strokes never merge

# Each glyph is 5 rows tall and GLYPH_W cells wide. Shapes are deliberately
# distinct (p/b by descender, n/m by stem count) and use internal spaces so the
# letters stay legible even where █ renders as a solid cell.
_GLYPHS = {
    "p": ["███  ", "█  █ ", "███  ", "█    ", "█    "],
    "h": ["█    ", "███  ", "█  █ ", "█  █ ", "█  █ "],
    "a": [" ██  ", "   █ ", " ███ ", "█  █ ", " ███ "],
    "n": ["█  █ ", "█  █ ", "█  █ ", "█  █ ", "█  █ "],
    "t": ["  █  ", "████ ", "  █  ", "  █  ", "  █  "],
    "o": [" ██  ", "█  █ ", "█  █ ", "█  █ ", " ██  "],
    "m": ["█ █ █", "█ █ █", "█ █ █", "█ █ █", "█ █ █"],
    "b": ["█    ", "███  ", "█  █ ", "█  █ ", "███  "],
    "x": ["█   █", " █ █ ", "  █  ", " █ █ ", "█   █"],
}


def _wordmark() -> list[str]:
    rows = [""] * 5
    for ch in _WORD:
        glyph = _GLYPHS.get(ch, [""] * 5)
        # Pad/trim every row to GLYPH_W so vertical strokes stay aligned.
        normalized = [
            (glyph[i] if i < len(glyph) else "")[:GLYPH_W].ljust(GLYPH_W)
            for i in range(5)
        ]
        for i in range(5):
            rows[i] += normalized[i] + (" " * GLYPH_GAP)
    width = max(len(r) for r in rows)
    # Do NOT rstrip: trailing spaces are what keep the columns aligned.
    rows = [r.ljust(width) for r in rows]

    # Drop shadow: the wordmark's whole silhouette one row down, one cell right.
    silhouette = "".join(
        "▒" if any(r[i] != " " for r in rows) else " "
        for i in range(width)
    )
    return rows + [(" " + silhouette).rstrip()]


def _center(lines: list[str], width: int | None = None) -> list[str]:
    w = width or max((len(l) for l in lines), default=0)
    return [l.center(w) for l in lines]


def banner_lines(filled: bool = True) -> list[str]:
    """Full logo (mark + wordmark) as centred lines."""
    body = list(_MARK_FILLED if filled else _MARK)
    canv = body + [""] + _wordmark()
    return _center(canv)


ASCII_ONLY_BANNER = [
    "      .---------.       ",
    "     /  ^   ^  /|       ",
    "    /    ___  / |   \\|/ ",
    "   |           | |  -+- ",
    "   |  PHANTOM  | /   /|\\",
    "   |    BOX    |/        ",
    "   '---------'          ",
    "    p h a n t o m b o x  ",
]

WORDMARK_ONLY = [
    "▄▄▄  █   ▄▀▄ █▀▄ █▄ █ ▀█▀ ▄▀▄ █▄▀▄ █▀▄ █ █",
    "█  █ █   █▀█ █  █ █ ▀█  █  █ █ █ █ █ █  █ ▄ ",
    "▀▀▀  ▀   ▀ ▀ ▀  ▀ ▀  ▀  ▀  ▀▀▀ ▀ ▀ ▀ ▀▀▀ █ █",
]


def supports_blocks() -> bool:
    """True when the active terminal encoding can render block glyphs.

    Checks, in order: the real stdout stream, then the locale preferred encoding,
    then ``sys.stdout``. Some environments (pytest capture, Textual's headless
    driver) replace ``sys.stdout`` with an object that has no ``encoding``
    attribute at all — treating that as "ascii" made the rich logo silently fall
    back to the plain ASCII one.
    """
    import locale
    import sys

    candidates = []
    for stream in (getattr(sys, "stdout", None), getattr(sys, "__stdout__", None)):
        enc = getattr(stream, "encoding", None)
        if enc:
            candidates.append(enc)
    try:
        preferred = locale.getpreferredencoding(False)
        if preferred:
            candidates.append(preferred)
    except Exception:
        pass

    if not candidates:
        return True  # unknown encoding: assume a modern terminal

    for enc in candidates:
        norm = enc.lower().replace("-", "").replace("_", "")
        if norm in ("ascii", "usascii"):
            continue
        try:
            "█▓▒▀▄".encode(enc)
        except Exception:
            continue
        return True
    return False


def pick_banner(width: int | None = None) -> tuple[str, list[str]]:
    """Choose the richest banner that fits: full | wordmark | ascii | plain."""
    import shutil

    cols = width or shutil.get_terminal_size((80, 24)).columns
    if supports_blocks():
        if cols >= 62:
            return "full", banner_lines()
        if cols >= 44:
            return "wordmark", [l.center(max(len(x) for x in WORDMARK_ONLY)) for l in WORDMARK_ONLY]
        return "plain", ["phantombox"]
    if cols >= 34:
        return "ascii", ASCII_ONLY_BANNER
    return "plain", ["phantombox"]


def text_width(text: str) -> int:
    """Display width (double-width East Asian glyphs count as 2)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)
