"""Measure where the masked colour layer vs. the border actually renders.

Checks the real geometry of the prompt card: does the inner input layer leave a
1-cell ring on all four sides, and are the border pixels drawn over the colour?
"""
import asyncio
import io
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, "src")

import phantombox.config as C

C.CONFIG_FILE = Path(tempfile.mkdtemp()) / "config.json"
import phantombox.providers as P
P.CATALOG_CACHE_FILE = Path(tempfile.mkdtemp()) / "cat.json"

from phantombox.chat import PhantomChat

CELL_W, CELL_H = 9.0, 18.0


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=(104, 44)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.8)
        svg = app.export_screenshot()
        Path("shot_welcome.svg").write_text(svg, encoding="utf-8")

        root = ET.fromstring(svg)
        ns = {"s": "http://www.w3.org/2000/svg"}
        rects = []
        for r in root.iter("{http://www.w3.org/2000/svg}rect"):
            rects.append((
                float(r.get("x", 0)), float(r.get("y", 0)),
                float(r.get("width", CELL_W)), float(r.get("height", CELL_H)),
                r.get("fill"),
            ))
        print("rects:", len(rects))
        fills = {}
        for r in rects:
            fills[r[4]] = fills.get(r[4], 0) + 1
        print("fills:", fills)

        # Sample the row straight through the middle of the prompt card.
        card = app.query_one("#welcome-prompt-card")
        y0 = card.region.y
        prompt = app.query_one("#welcome-prompt")
        print("card region:", card.region, "prompt region:", prompt.region)

        row_y = (card.region.y + 2) * CELL_H
        band = [r for r in rects if abs(r[1] - row_y) < 1]
        band.sort()
        print(f"\nsample row y={row_y}: {len(band)} rects")
        for r in band[:6] + band[-6:]:
            print(f"  x={r[0]:7.1f} w={r[2]:5.1f} fill={r[4]}")


asyncio.run(main())
