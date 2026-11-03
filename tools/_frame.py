"""Dump what the app actually draws, cell by cell, for the welcome screen."""
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


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=(104, 44)) as pilot:
        await pilot.pause()
        await asyncio.sleep(1.0)

        # Build a plain text frame from the compositor's strips.
        rows = {0: {}, 1: {}}
        for strip in app.screen._compositor.render_strips():
            for seg in strip._segments:
                rows[strip._y].setdefault(strip._x, seg.text)

        out = []
        for y in sorted(rows):
            cells = rows[y]
            line = []
            x = 0
            for x in range(0, 104):
                line.append(cells.get(x, " ")[0] if cells.get(x) else " ")
            out.append("".join(line).rstrip())

        Path("welcome_frame.txt").write_text("\n".join(out), encoding="utf-8")
        print("\n".join(out[20:44]))


asyncio.run(main())
