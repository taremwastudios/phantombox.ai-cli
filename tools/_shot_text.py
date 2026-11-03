"""Capture the screen as a clean character grid (handles Textual segment strips)."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

import phantombox.config as C

C.CONFIG_FILE = Path(tempfile.mkdtemp()) / "config.json"
import phantombox.providers as P
P.CATALOG_CACHE_FILE = Path(tempfile.mkdtemp()) / "cat.json"

from phantombox.chat import PhantomChat


def grid(app):
    """Rebuild the screen text row by row from Textual's compositor strips."""
    rows = []
    comp = app.screen._compositor
    for y in range(app.size.height):
        line = ""
        try:
            strip = comp._visible_lines[y]
        except Exception:
            rows.append("")
            continue
        for seg in strip:
            text = getattr(seg, "text", "")
            line += text
        rows.append(line)
    return rows


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=(104, 44)) as pilot:
        await pilot.pause()
        await asyncio.sleep(1.0)
        for r in grid(app):
            print(r.rstrip())
        print("\n--- chip sizes:", [str(c.size) for c in app.query(".chip")][:3])


asyncio.run(main())
