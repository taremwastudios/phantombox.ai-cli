"""Replace the animation-driven hero art in branding.py with a clean,
small-lettered, filled, shadowed "phantombox.ai" wordmark.
"""
import io

P = "src/phantombox/branding.py"
src = io.open(P, encoding="utf-8").read()
lines = src.split("\n")

start = next(i for i, l in enumerate(lines) if l.startswith("# Animated hero logo"))
end = next(i for i, l in enumerate(lines) if l.startswith("LOGO_ART = _load_logo_art()")) + 1

block = '''# Animated hero logo
# ---------------------------------------------------------------------------
#
# The hero slot renders a small, lowercase "phantombox.ai" wordmark: solid
# blocks for the lettering (filled), with a one-row drop shadow underneath so
# it reads as raised. Simple and always legible -- no animation.

WORDMARK_WORD = "phantombox.ai"

# Glyph rules: every glyph is GLYPH_W cells wide, 5 rows tall, sharing one
# baseline. '.' and 'i' reuse the same slot (dot over the stem).
WORDMARK_GLYPH_W = 6
WORDMARK_GAP = 2

_WORDMARK_GLYPHS = {
    "p": ["      ", "████  ", "█  █  ", "████  ", "█     ", "█     "],
    "h": ["█   █ ", "█   █ ", "█████ ", "█   █ ", "█   █ "],
    "a": ["      ", " ███  ", "█   █ ", "█████ ", "█   █ "],
    "n": ["█   █ ", "██  █ ", "█ █ █ ", "█  ██ ", "█   █ ", "█   █ "],
    "t": ["  █   ", "  █   ", "█████ ", "  █   ", "  █   "],
    "o": ["      ", " ███  ", "█   █ ", "█   █ ", " ███  "],
    "m": ["█  █  ", "█████ ", "█ █ █ ", "█ █ █ "],
    "b": ["█     ", "████  ", "█   █ ", "█   █ ", "████  "],
    "x": ["█   █ ", " █ █  ", "  █   ", "  █   ", " █ █  ", "█   █ "],
    ".": ["      ", "      ", "      ", "      ", "      ", "  ██  "],
    "i": ["  ██  ", "      ", "  ██  ", "  ██  "],
}

_WORDMARK_H = 6


def _wordmark_rows() -> list[str]:
    """Solid 'phantombox.ai' wordmark, 6 rows of lettering."""
    rows = [""] * _WORDMARK_H
    for i, ch in enumerate(WORDMARK_WORD):
        glyph = _WORDMARK_GLYPHS.get(ch, [" "] * _WORDMARK_H)
        for r in range(_WORDMARK_H):
            rows[r] += glyph[r].ljust(WORDMARK_GLYPH_W)[:WORDMARK_GLYPH_W]
            if i != len(WORDMARK_WORD) - 1:
                rows[r] += " " * WORDMARK_GAP
    width = max(len(r) for r in rows)
    return [r.ljust(width) for r in rows]


def logo_frame(lit: int = 0, trail: int = 0) -> list[str]:
    """Compatibility shim: one static frame (solid wordmark + shadow row)."""
    rows = _wordmark_rows()
    width = max(len(r) for r in rows)
    silhouette = "".join("▒" if any(r[i] != " " for r in rows) else " " for i in range(width))
    rows = rows + [(" " + silhouette).rstrip()]
    return [r for r in rows]


def logo_frames() -> list[list[str]]:
    """One static frame (kept for HeroLogo's API)."""
    return [logo_frame()]


def logo_plain_rows() -> list[str]:
    """Fallback for terminals without block glyphs."""
    return [WORDMARK_WORD]
'''

lines[start:end] = block.split("\n")
io.open(P, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
print("patched branding.py")
