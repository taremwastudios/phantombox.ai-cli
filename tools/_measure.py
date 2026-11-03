"""Measure the real geometry of the welcome screen widgets."""
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

        for sel in ("#welcome-container", "#hero-banner", "#welcome-prompt-card",
                    "#welcome-prompt", "#welcome-chips"):
            try:
                w = app.query_one(sel)
            except Exception as exc:
                print(f"{sel:26} MISSING ({exc})")
                continue
            region = w.region
            print(f"{sel:26} size={w.size}  region=y{region.y} h{region.height} w{region.width}")

        print()
        print("app size:", app.size)
        print("chips:", len(app.query(".chip")))
        for chip in app.query(".chip"):
            print(f"   chip {chip.size} region={chip.region}")

        prompt = app.query_one("#welcome-prompt")
        print("\nprompt styles: height=", prompt.styles.height,
              "min=", prompt.styles.min_height)
        # simulate typing two lines then 8 lines to check flex
        prompt.text = "line one\nline two"
        await pilot.pause()
        print("after 2 lines:", prompt.size, prompt.styles.height)
        prompt.text = "\n".join(f"line {i}" for i in range(9))
        await pilot.pause()
        print("after 9 lines:", prompt.size, prompt.styles.height)


asyncio.run(main())
