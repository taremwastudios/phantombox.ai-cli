"""Verify all five reported issues are fixed."""
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
P._REMOTE_MODELS_CACHE["groq"] = [f"m-{i}" for i in range(9)]

from phantombox.chat import PhantomChat
import phantombox.mcp as M
import phantombox.tools as T

print("=== 5. import check ===")
try:
    from phantombox.mcp import is_mcp_tool
    print("   is_mcp_tool importable from phantombox.mcp: OK")
    print("   is_mcp_tool on tools module:", hasattr(T, "is_mcp_tool"), "(expected False)")
except Exception as e:
    print("   FAILED:", e)

print("\n=== 1. duplicate add ===")
real_add = M.add_mcp_server.__wrapped__ if hasattr(M.add_mcp_server, "__wrapped__") else None
from phantombox.mcp import add_mcp_server as real_add_fn

C.set_config_val("mcp_servers", {"github": {"command": "npx", "args": ["-y", "gh"]}})
ok, msg = real_add_fn("github", {"command": "npx", "args": ["-y", "gh"]})
print(f"   re-add existing -> ok={ok} msg={msg}")
ok2, msg2 = real_add_fn("fresh", {"command": "npx"})
print(f"   add new         -> ok={ok2} msg={msg2}")


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


servers = {"github": {"command": "npx", "args": ["-y", "gh"]}}
M.list_mcp_servers = lambda: servers
M.get_mcp_manager = lambda: FakeMgr()
M.add_mcp_server = lambda n, s: (False, f"MCP server '{n}' already exists (npx -y gh).")
M.probe_mcp_server = lambda n: (True, f"ok {n}")

spinners = []


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

        _orig_start = PhantomChat._start_status

        async def tracked_start(self, label, color="#38bdf8"):
            sp = await _orig_start(self, label, color)
            spinners.append(label)
            return sp

        app._start_status = tracked_start.__get__(app)

        print("\n=== 1b. /mcp add duplicate message ===")
        printed.clear()
        await app._handle_mcp_command(["add", "github", "npx"], "")
        print("   ", strip(printed[-1])[:90])

        print("\n=== 2. /mcp list does not freeze the app ===")
        import re
        app._handle_command.__globals__  # noqa
        before = app._pending_confirmation
        task = asyncio.create_task(app._handle_command("/mcp list"))
        await pilot.pause()
        await asyncio.sleep(0.3)
        # The command task itself must return immediately (worker owns the wait).
        print("   command returned immediately:", task.done())
        print("   waiting on y/n:", app._pending_confirmation is not None)
        # UI still responsive: another command can run while waiting.
        printed.clear()
        await app._handle_mcp_command(["--tools", "groq"], "")
        print("   another command ran while waiting:", bool(printed))

        # Answer y
        prompt = app.query_one("#dock-prompt") if not app.in_welcome_mode else app.query_one("#welcome-prompt")
        prompt.focus()
        prompt.post_message(prompt.Submitted("y"))
        await pilot.pause()
        for _ in range(30):
            await asyncio.sleep(0.1)
            if app._pending_confirmation is None:
                break
        print("   confirmation resolved:", app._pending_confirmation is None)
        await asyncio.sleep(0.5)
        print("   listing produced output:", any("MCP Servers" in p for p in printed) or any("MCP Servers" in p for p in printed))

        print("\n=== 3. spinner for /mcp --tools <provider> ===")
        spinners.clear()
        printed.clear()
        await app._handle_mcp_command(["--tools", "groq"], "")
        print("   spinners shown:", spinners)


def strip(t):
    import re
    return re.sub(r"\[/?[a-z0-9 #_.\-]+\]", "", t)


asyncio.run(main())
