"""Dump specific animation frames as text to verify content."""
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from phantombox import branding

fs = branding.logo_frames()
print("total frames:", len(fs), "trail:", branding.LOGO_TRAIL)
for idx in (0, 10, 40, 60, len(fs) - branding.LOGO_TRAIL - 5, len(fs) - 1):
    f = fs[idx]
    print(f"\n=== frame {idx} (of {len(fs)-1}) ===")
    for r in f:
        print("|" + r.replace("\u00a0", " ") + "|")
