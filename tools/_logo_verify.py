"""Verify LOGO_ART in branding.py matches logo.txt exactly."""
import io
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from phantombox import branding

want = [l for l in io.open("logo.txt", encoding="utf-8").read().split("\n") if l.strip()]
have = list(branding.LOGO_ART)

print("logo.txt rows:", len(want), " LOGO_ART rows:", len(have))
ok = True
for i in range(max(len(want), len(have))):
    w = want[i] if i < len(want) else "<missing>"
    h = have[i] if i < len(have) else "<missing>"
    match = "  OK" if w == h else "  DIFF"
    if w != h:
        ok = False
        print(f"row {i}{match}")
        print("  want:", repr(w))
        print("  have:", repr(h))
    else:
        print(f"row {i}{match}")
print("MATCH" if ok else "MISMATCH")
