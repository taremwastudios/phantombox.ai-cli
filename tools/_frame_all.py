"""Dump the actual rendered frame (cell by cell) for the welcome screen or chat."""
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

MODE = sys.argv[1] if len(sys.argv) > 1 else "welcome"
SIZE = (104, 44)


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.9)
        if MODE == "welcome":
            # Land on a frame where the sweep is mid-word, not a blank beat.
            hero = app.query_one("#hero-banner")
            hero._frame_idx = len(hero._frames) // 2
            hero._next_frame()
            await pilot.pause()
        if MODE == "chat":
            app._switch_to_chat()
            await pilot.pause()
            await asyncio.sleep(0.3)

        buf = []
        for strip in app.screen._compositor.render_strips():
            buf.append(strip.text)
        Path(f"frame_{MODE}.txt").write_text(
            "\n".join(f"{i:02d} |{text}|" for i, text in enumerate(buf)),
            encoding="utf-8",
        )
        print("wrote frame_%s.txt (%d strips)" % (MODE, len(buf)))


asyncio.run(main())
