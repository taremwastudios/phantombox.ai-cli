import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from rich.console import Console
from textual.widgets import Static

console = Console(record=True, width=80, file=open("nul", "w"))

blocks = "".join(chr(c) for c in range(0x2591, 0x2594))
for label, text in [
    ("double-width blocks", "▓▓▓▓  ▓    ▓   ▓▓▓"),
    ("single-width box", "────│ ─ ╭╮╰╯"),
    ("mixed", blocks + "  ████"),
]:
    print(f"{label:20} width={Static(text)._content.cell_length if hasattr(Static(text), '_content') else '?'}")
    st = Static(text, markup=False)
    from rich.text import Text
    t = Text(text)
    print(f"{'':20} rich len={len(text)}")
    print(f"{'':20} strip cell_length={Static(text).render().cell_length if hasattr(Static(text).render(), 'cell_length') else '?'}")

# measure via rich's cell_len
from rich.cells import cell_len

print("cell_len blocks:", cell_len("▓▓▓▓"))
print("cell_len box:", cell_len("────"))
print("cell_len full block:", cell_len("████"))

# check the console's unicode width cache
from rich._cell_widths import cell_width

for ch in "▓░█─╭╰":
    print(ch, "->", cell_width(ch))

import unicodedata

for ch in "▓░█─╭╰":
    print("east_asian", ch, unicodedata.east_asian_width(ch))
