"""Reproduce: /mcp list prints the y/n prompt and then the UI stops responding."""
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
C.set_config_val("providers", {"groq": {"api_key": "gsk_test"}})
P._REMOTE_MODELS_CACHE["groq"] = [f"model-{i}" for i in range(12)]

from phantombox.chat import PhantomChat
import phantombox.mcp as M


class FakeConn:
    status = "ready"
    error = ""
    tools = [{"name": "x", "description": "d", "inputSchema": {"type": "object"}}]


class FakeMgr:
    connections = {"github": FakeConn()}

    def get_tools(self, name=None):
        return [{"server": "github", "name": "x", "qualified_name": "github__x",
                 "description": "d", "input_schema": {"type": "object"}}]

    def connect_all(self):
        return []


M.list_mcp_servers = lambda: {"github": {"command": "npx", "args": ["-y", "gh"]}}
M.get_mcp_manager = lambda: FakeMgr()


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.6)

        printed = []
        orig = app._add_system_message

        async def spy(t):
            printed.append(t)
            await orig(t)

        app._add_system_message = spy

        prompt = app.query_one("#welcome-prompt")

        # Type /mcp list and submit it like a user would.
        task = asyncio.create_task(app._handle_command("/mcp list"))
        await pilot.pause()
        await asyncio.sleep(0.3)

        print("pending confirmation:", app._pending_confirmation is not None)
        print("prompt printed:", any("Are you sure" in p for p in printed))
        print("task done (should be False, waiting for y/n):", task.done())
        print("pending future:", app._pending_confirmation)

        # Now answer "y" into the prompt, as the user would.
        if app.in_welcome_mode:
            prompt = app.query_one("#welcome-prompt")
        else:
            prompt = app.query_one("#dock-prompt")
        prompt.focus()
        prompt.text = "y"
        prompt.post_message(prompt.Submitted("y"))
        await pilot.pause()
        await asyncio.sleep(0.4)

        print("\nafter submitting 'y':")
        print("  task done:", task.done())
        print("  pending:", app._pending_confirmation)
        if task.done():
            print("  output tail:", [p.replace(chr(10), ' | ')[:80] for p in printed[-2:]])
        else:
            print("  *** STILL BLOCKED ***")

        task.cancel()


asyncio.run(main())
