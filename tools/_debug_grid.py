import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "src")
sys.path.insert(0, "tools")

from _logo_frames_build import build_grid as _bg

g = _bg()
print("cols", len(g))
for i, col in enumerate(g):
    ink = "".join(col).replace(" ", "")
    if ink:
        print(i, len(ink), repr("".join(col)))
