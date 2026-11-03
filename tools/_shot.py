"""Render the app to an SVG screenshot so the layout can be reviewed visually."""
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
        await asyncio.sleep(1.2)
        svg = app.export_screenshot()
        out = Path("welcome_shot.svg")
        out.write_text(svg, encoding="utf-8")
        print("wrote", out.resolve(), len(svg), "bytes")
        print("chips:", len(app.query(".chip")))
        b = app.query_one("#hero-banner")
        print("banner:", b.size, "| lines:", len(str(b.render()).splitlines()))


asyncio.run(main())
