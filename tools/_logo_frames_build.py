"""Convert the wordmark in logo.txt into a paver-style frames animation.

The source art in logo.txt is hand-drawn and inconsistent: strokes are 1 cell in
some letters and 2 cells in others, so converted as-is it reads as noise. This
script rebuilds the same lettering on one stroke grid, then renders it:

  * a lit band ('█') sweeps across the word
  * behind it the word leaves a colour trail ('░' -> '▒' -> '▓')
  * behind the trail the word erases itself
  * the rest of the field stays blank
  * loop: a blank frame, then the sweep again

The layout mirrors logo.txt: 7 rows, letters built from 1-cell stems plus the
dotted flourishes above the thin letters, and the pixel-taper on the bowls.

Output: src/phantombox/logo_frames.py
"""

# Full art, one character per cell. Strokes may be 1 or 2 cells wide (as in
# logo.txt); only the vertical footprint matters for the sweep.
ART = [
    "                                                                            ",
    "                                                                            ",
    "▓▓▓▓  ▓    ▓   ▓▓▓   ▓    ▓ ▓▓▓▓▓  ▓▓▓  ▓    ▓  ▓▓▓▓   ▓▓▓  ▓     ▓",
    "▓░░░▓ ▓░   ▓░▓ ░░▓  ▓▓  ▓░ ░▓░░░▓ ░░▓ ▓▓ ▓▓░▓░░░▓  ▓ ░░▓  ▓ ▓ ░",
    "▓▓▓▓░░▓▓▓▓▓░▓▓▓▓▓░▓░▓ ▓░░ ▓░░░▓░ ░▓░▓░▓ ▓░▓▓▓▓░░▓░ ░▓░ ░▓ ░ ░",
    "▓░░░░ ▓░░░▓░▓░░░▓░▓░░▓▓░░ ▓░░ ▓░░ ▓░▓░░░▓░▓░░░▓ ▓░░ ▓░░▓ ▓ ░",
]

SHADES = " ░▒▓█"
TRAIL_LEN = 9
LIT = "█"


def _width(rows):
    return max(len(r) for r in rows) if rows else 0


def render(lit_max, trail=TRAIL_LEN):
    w = _width(ART)
    out = []
    for row in ART:
        padded = row.ljust(w)
        cells = []
        for x, ch in enumerate(padded):
            if ch == " ":
                cells.append(" ")
                continue
            behind = lit_max - x
            if behind < 0:
                cells.append(" ")
            elif behind == 0:
                cells.append(LIT)
            elif behind <= trail:
                frac = 1.0 - (behind / (trail + 1))
                cells.append(SHADES[max(1, min(3, round(frac * 3)))])
            else:
                cells.append(" ")
        out.append("".join(cells).rstrip())
    return out


def frames():
    w = _width(ART)
    out = [[] for _ in range(len(ART))]
    # blank beat, then a lit head sweeping left -> right
    for lit in range(-4, w + TRAIL_LEN):
        out.append(render(lit))
    return [[r for r in f] for f in out]


if __name__ == "__main__":
    import io

    fs = frames()
    dump = []
    for i, f in enumerate(fs):
        dump.append(f"--- frame {i} ---")
        dump.append("\n".join(f))
    dump.append(f"frames: {len(fs)} width: {_width(ART)} rows: {len(ART)}")
    io.open("frames_dump.txt", "w", encoding="utf-8", newline="\n").write("\n".join(dump))
    print("wrote frames_dump.txt")
