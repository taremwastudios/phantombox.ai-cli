"""Exercise the new /mcp subcommands end-to-end in a headless app."""
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

C.set_config_val("providers", {"groq": {"api_key": "test-key"}})
P._REMOTE_MODELS_CACHE["groq"] = ["llama-3.3-70b-versatile", "llama-2-70b-chat", "whisper-large-v3"]
print("connected:", P.list_connected_providers())

from phantombox.chat import PhantomChat
import phantombox.mcp as M


class FakeConn:
    status = "ready"
    error = ""

    def __init__(self, tools):
        self.tools = tools


class FakeMgr:
    def __init__(self):
        self.connections = {
            "github": FakeConn([
                {"name": "create_issue", "description": "Create a GitHub issue", "inputSchema": {"type": "object"}},
                {"name": "list_prs", "description": "List pull requests", "inputSchema": {"type": "object"}},
            ]),
            "filesystem": FakeConn([
                {"name": "read", "description": "Read a file", "inputSchema": {"type": "object"}},
            ]),
        }

    def get_tools(self, name=None):
        out = []
        for srv, conn in ([(name, self.connections[name])] if name else self.connections.items()):
            for t in conn.tools:
                out.append({
                    "server": srv, "name": t["name"],
                    "qualified_name": f"{srv}__{t['name']}",
                    "description": t["description"], "input_schema": t["inputSchema"],
                })
        return out


M.get_mcp_manager = lambda: FakeMgr()
M.list_mcp_servers = lambda: {"github": {"command": "npx", "args": ["-y", "gh"]}, "filesystem": {"command": "npx", "args": ["fs"]}}

captured = []


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.7)

        orig = app._add_system_message

        async def spy(text):
            captured.append(text)
            await orig(text)

        app._add_system_message = spy

        for cmd in (
            ["list"],
            ["--tools", "groq"],
            ["--tools", "provider", "--list"],
            ["--tools", "--list"],
            ["--models", "groq"],
            ["--tools", "notreal"],
        ):
            captured.clear()
            await app._handle_mcp_command(cmd, " ".join(cmd))
            print(f"\n{'=' * 78}\n/mcp {' '.join(cmd)}\n{'=' * 78}")
            for c in captured:
                print(strip(c))


def strip(t):
    import re
    return re.sub(r"\[/?[a-z0-9 #_\-]+\]", "", t)


asyncio.run(main())
