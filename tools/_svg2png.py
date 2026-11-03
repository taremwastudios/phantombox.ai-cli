"""Rasterize a Textual SVG screenshot to PNG with Pillow (no cairo needed).

Textual's export_screenshot writes absolutely-positioned <rect> cells and
<text x y fill> glyph runs in SVG user units == terminal cells * cell size.
We paint rects first, then the glyphs, using a mono font so columns line up.
"""
import re
import sys
from html import unescape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CELL_W, CELL_H, FONT_PX = 9, 18, 15


def load_font():
    for name in ("consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(name, FONT_PX)
        except Exception:
            continue
    return ImageFont.load_default()


def render(svg_path: str, png_path: str):
    svg = Path(svg_path).read_text(encoding="utf-8")

    # Textual/rich writes a decimal viewBox: viewBox="0 0 1287 1123.6"
    vm = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if vm:
        cells_w = max(1, round(float(vm.group(1)) / CELL_W))
        cells_h = max(1, round(float(vm.group(2)) / CELL_H))
    else:
        wm = re.search(r'width="([\d.]+)"', svg)
        hm = re.search(r'height="([\d.]+)"', svg)
        cells_w = int(float(wm.group(1))) // CELL_W if wm else 104
        cells_h = int(float(hm.group(1))) // CELL_H if hm else 44

    img = Image.new("RGB", (cells_w * CELL_W, cells_h * CELL_H), "#11111b")
    draw = ImageDraw.Draw(img)
    font = load_font()

    # Background rects
    for m in re.finditer(r'<rect[^>]*?x="([\d.]+)"[^>]*?y="([\d.]+)"[^>]*?'
                         r'(?:width="([\d.]+)")?[^>]*?(?:height="([\d.]+)")?[^>]*?'
                         r'fill="(#[0-9a-fA-F]{3,8}|rgb\([^)]*\)|none)"', svg):
        x, y = float(m.group(1)), float(m.group(2))
        w = float(m.group(3) or CELL_W)
        h = float(m.group(4) or CELL_H)
        fill = m.group(5)
        if fill == "none":
            continue
        draw.rectangle([x, y, x + w, y + h], fill=fill)

    # Text runs: <text ... x=".." y=".." fill="..">TEXT</text>
    for m in re.finditer(r'<text\b([^>]*)>(.*?)</text>', svg, re.S):
        attrs, raw = m.group(1), m.group(2)
        xm = re.search(r'x="([\d.]+)"', attrs)
        ym = re.search(r'y="([\d.]+)"', attrs)
        fm = re.search(r'fill="([^"]+)"', attrs)
        if not (xm and ym):
            continue
        x, y = float(xm.group(1)), float(ym.group(1))
        if x < 0 or y < 0:
            continue
        fill = fm.group(1) if fm else "#ffffff"
        if fill == "none":
            continue
        text = _unescape(unescape(re.sub(r"<[^>]+>", "", raw)))
        # Rich emits one <text> per styled run; draw as-is at its cell origin.
        for idx, line in enumerate(text.splitlines() or [text]):
            if line.strip():
                draw.text((x, y - CELL_H + 3 + idx * CELL_H), line, font=font, fill=fill)

    img.save(png_path)
    print("wrote", png_path, img.size)


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'")
             .replace("&#160;", " ")
             .replace("&amp;", "&"))


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
