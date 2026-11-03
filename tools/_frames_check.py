import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from phantombox import branding

fs = branding.logo_frames()
print("frames", len(fs), "width", branding._logo_width(), "minwidth", branding.LOGO_MIN_WIDTH)
print("frame sizes:", [len(f) for f in fs[:5]], len(fs[30]))
print("supports_blocks:", branding.supports_blocks())
for f in (fs[0], fs[38]):
    print("----")
    for r in f:
        print(repr(r[:30]))
