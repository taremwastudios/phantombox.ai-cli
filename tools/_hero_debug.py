import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import phantombox.config as C

C.CONFIG_FILE = Path(tempfile.mkdtemp()) / "config.json"
import phantombox.providers as P
P.CATALOG_CACHE_FILE = Path(tempfile.mkdtemp()) / "cat.json"

from phantombox.chat import PhantomChat, HeroLogo


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=(104, 44)) as pilot:
        await pilot.pause()
        hero = app.query_one("#hero-banner", HeroLogo)
        print("hero class:", type(hero).__name__)
        print("hero size:", hero.size, "region:", hero.region)
        print("timer:", hero._timer is not None, "frame:", hero._frame_idx)
        text = str(hero.renderable)
        print("renderable rows:", len(text.split("\n")), "len:", len(text))
        print("first rows:")
        for r in text.split("\n"):
            print("  ", repr(r[:20]))
        print("styles height:", hero.styles.height, "width:", hero.styles.width)
        print("content lines:",
              [ln for ln in str(hero._content.plain).split("\n")][:8] if hasattr(hero, "_content") else "n/a")


asyncio.run(main())
