"""Replace the hero wordmark glyph definitions with clean 3x5 filled glyphs."""
import io

P = "src/phantombox/branding.py"
src = io.open(P, encoding="utf-8").read()
lines = src.split("\n")

start = next(i for i, l in enumerate(lines) if l.startswith('WORDMARK_WORD = "phantombox.ai"'))
end = next(i for i, l in enumerate(lines) if l.startswith("def logo_frame("))

block = '''WORDMARK_WORD = "phantombox.ai"
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


'''
lines[start:end] = block.split("\n")
io.open(P, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
print("patched")
