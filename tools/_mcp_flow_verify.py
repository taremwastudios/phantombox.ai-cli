"""Verify /mcp add spinner + clear outcomes, and the /mcp list y/n gate."""
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
P._REMOTE_MODELS_CACHE["groq"] = [f"model-{i}" for i in range(37)]

from phantombox.chat import PhantomChat
import phantombox.mcp as M

log = []

# Spinner visibility probe: record mount/remove of SpinnerThinking.
import phantombox.chat as CH

_orig_start = PhantomChat._start_status
_orig_stop = PhantomChat._stop_status


async def start_status(self, label, color="#38bdf8"):
    sp = await _orig_start(self, label, color)
    if sp is not None:
        log.append(("spinner+", label))
    return sp


async def stop_status(spinner):
    log.append(("spinner-", "removed"))
    if spinner is None:
        return
    try:
        await spinner.remove()
    except Exception:
        pass


PhantomChat._start_status = start_status
PhantomChat._stop_status = staticmethod(stop_status)
PhantomChat._stop_status = staticmethod(stop_status)

# Deterministic add results.
def fake_add(name, spec):
    return True, f"MCP server '{name}' saved."


def fake_probe_ok(name):
    return True, f"Connected to MCP server '{name}' (demo) — 2 tool(s): a, b"


def fake_probe_partial(name):
    return True, f"MCP server '{name}' connected, but its tool list could not be read yet (timeout)."


servers = {}


def fake_list():
    return servers


def fake_remove(name):
    return True, f"removed {name}"


M.add_mcp_server = fake_add
M.list_mcp_servers = fake_list
M.probe_mcp_server = fake_probe_ok

captured = []


async def main():
    app = PhantomChat("phantombox/router", 8080)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.6)

        orig = app._add_system_message

        async def spy(text):
            captured.append(text)
            await orig(text)

        app._add_system_message = spy

        # --- add: success ---
        captured.clear()
        log.clear()
        await app._handle_mcp_command(["add", "demo", "npx", "-y", "server"], "")
        print("=== /mcp add (success) ===")
        print("  spinner:", log)
        print("  message:", strip(captured[-1]))

        # --- add: partial (connected, tools unavailable) ---
        M.probe_mcp_server = fake_probe_partial
        captured.clear()
        log.clear()
        await app._handle_mcp_command(["add", "slow", "npx", "-y", "slow"], "")
        print("\n=== /mcp add (partial) ===")
        print("  spinner:", log)
        print("  message:", strip(captured[-1]))

        # --- add: hard failure ---
        M.probe_mcp_server = lambda n: (False, f"MCP server '{n}' failed to connect: FileNotFoundError")
        captured.clear()
        log.clear()
        await app._handle_mcp_command(["add", "broken", "nope"], "")
        print("\n=== /mcp add (failure) ===")
        print("  spinner:", log)
        print("  message:", strip(captured[-1]))

        # --- list: decline ---
        servers.update({"github": {"command": "npx", "args": ["-y", "gh"]}})
        M.get_mcp_manager = lambda: FakeMgr()
        captured.clear()
        log.clear()

        async def decline(question):
            captured.append(question)
            return False

        app._confirm_yes_no = decline
        await app._handle_mcp_command(["list"], "")
        print("\n=== /mcp list (declined) ===")
        print("  prompt:", strip(captured[0]).replace("\n", " | ")[:200])
        print("  spinner:", log)
        print("  after:", [strip(c).replace("\n", " ") for c in captured[1:]])

        # --- list: accept ---
        captured.clear()
        log.clear()

        async def accept(question):
            captured.append(question)
            return True

        app._confirm_yes_no = accept
        await app._handle_mcp_command(["list"], "")
        print("\n=== /mcp list (accepted) ===")
        print("  spinner:", log)
        body = strip(captured[-1])
        print("  output head:", body.replace("\n", " | ")[:220])


def strip(t):
    import re
    return re.sub(r"\[/?[a-z0-9 #_.\-]+\]", "", t)


class FakeConn:
    status = "ready"
    error = ""
    tools = [
        {"name": "create_issue", "description": "Create an issue", "inputSchema": {"type": "object"}},
    ]


class FakeMgr:
    connections = {"github": FakeConn()}

    def get_tools(self, name=None):
        return [{
            "server": "github", "name": "create_issue",
            "qualified_name": "github__create_issue",
            "description": "Create an issue", "input_schema": {"type": "object"},
        }]

    def connect_all(self):
        return []


asyncio.run(main())
