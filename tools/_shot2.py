"""Render the welcome screen (or chat) to PNG so the layout/colours can be reviewed."""
import asyncio
import subprocess
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
CHROME = r"C:\Users\HomePC\AppData\Local\ms-playwright\chromium-1243\chrome-win64\chrome.exe"


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.8)
        if MODE == "chat":
            app._switch_to_chat()
        await pilot.pause()
        await asyncio.sleep(0.3)

        Path(f"shot_{MODE}.svg").write_text(app.export_screenshot(), encoding="utf-8")
        print("hero:", app.query_one("#hero-banner").region)
        print("cards: welcome", app.query_one("#welcome-prompt-card").size,
              "dock", app.query_one("#dock-prompt-card").size)
        print("prompts: welcome", app.query_one("#welcome-prompt").size,
              "dock", app.query_one("#dock-prompt").size)
        chips = [c.size.width for c in app.query(".chip")]
        print("chip widths:", chips)


asyncio.run(main())

subprocess.run([
    CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
    f"--window-size={SIZE[0] * 9},{(SIZE[1] + 1) * 18}",
    "--screenshot=" + str(Path(f"shot_{MODE}.png").resolve()),
    Path(f"shot_{MODE}.svg").resolve().as_uri(),
], capture_output=True)
print("wrote", f"shot_{MODE}.png")
