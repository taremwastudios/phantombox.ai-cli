from PIL import Image, ImageDraw, ImageFont

FONT = ImageFont.truetype("consola.ttf", 15)
CHARS = "\u2588\u2593\u2592\u2591\u2500\u2502\u2550\u2551\u2554\u2557\u256d\u256e\u2580\u2584\u2594\u258c\u2590"

img = Image.new("RGB", (900, 40), "#11111b")
d = ImageDraw.Draw(img)
for ch in CHARS:
    # measure: if the font lacks the glyph it draws the .notdef box
    img2 = Image.new("L", (20, 24), 0)
    d2 = ImageDraw.Draw(img2)
    d2.text((0, 0), ch, font=FONT, fill=255)
    px = img2.load()
    solid = sum(1 for yy in range(24) for xx in range(20) if px[xx, yy] > 200)
    print(f"U+{ord(ch):04X} {ch}  ink={solid:3d}")

d.text((10, 12), "".join(CHARS), font=FONT, fill="#ffffff")
img.save("glyph_probe.png")
