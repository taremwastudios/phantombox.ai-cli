"""Model Context Protocol (MCP) client for phantombox.

Connects external MCP *tool servers* and exposes their tools to whichever model is
active, alongside PhantomBox's own local tools.

Two transports are supported:

* **stdio**  — spawn a server process and speak newline-delimited JSON-RPC 2.0 on
  its stdin/stdout (``npx -y @modelcontextprotocol/server-filesystem <dir>``).
* **http**   — Streamable HTTP: POST JSON-RPC to a URL, read either a JSON body
  or a ``text/event-stream`` response.

Design notes
------------
* A dedicated background event loop thread owns every connection. Server processes
  are long-lived and must not be tied to whichever thread happens to call a tool,
  so all I/O is funnelled through ``_run_coro``.
* Every MCP tool is exposed under a namespaced name ``<server>__<tool>`` so two
  servers can never collide with each other or with local tools.
* Tool schemas are translated to OpenAI function-calling format because that is
  what every model API (local llama.cpp and cloud providers) expects.
* MCP tools are never auto-approved: remote servers can run arbitrary code, so
  the default permission requirement is ``needs_confirmation``.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import threading
from typing import Any, Dict, List, Optional, Tuple

import httpx

from phantombox.config import load_config, set_config_val

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "phantombox", "version": "0.1.0"}
REQUEST_TIMEOUT = 30.0          # tools/call ceiling
HANDSHAKE_TIMEOUT = 20.0        # initialize / tools/list ceiling
MAX_TOOL_RESULT_CHARS = 8000

# ---------------------------------------------------------------------------
# Dedicated event loop (one per process) for long-lived MCP connections
# ---------------------------------------------------------------------------

_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOOP_LOCK = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return the background loop that owns all MCP I/O (created on first use)."""
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="phantombox-mcp", daemon=True)
            thread.start()
            _LOOP = loop
    return _LOOP


def _run_coro(coro, timeout: Optional[float] = None):
    """Run a coroutine on the MCP loop from sync code and return its result."""
    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class McpTransportError(RuntimeError):
    pass


class _StdioTransport:
    """Newline-delimited JSON-RPC over a child process' stdin/stdout."""

    def __init__(self, command: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None):
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.stderr_tail: List[str] = []

    async def start(self) -> None:
        resolved = shutil.which(self.command) or self.command
        if not shutil.which(self.command) and not os.path.exists(self.command):
            raise McpTransportError(f"command not found on PATH: '{self.command}'")

        child_env = dict(os.environ)
        child_env.update({str(k): str(v) for k, v in self.env.items()})

        try:
            self.proc = await asyncio.create_subprocess_exec(
                resolved,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
        except Exception as exc:  # pragma: no cover - platform dependent
            raise McpTransportError(f"failed to start '{self.command}': {exc}") from exc

        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            async for raw in self.proc.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-20]
        except Exception:
            pass

    async def _alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def send(self, message: Dict[str, Any]) -> None:
        if not await self._alive() or not self.proc or not self.proc.stdin:
            detail = (" | " + self.stderr_tail[-1]) if self.stderr_tail else ""
            raise McpTransportError(f"server process is not running{detail}")
        data = (json.dumps(message) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def recv(self) -> Dict[str, Any]:
        assert self.proc and self.proc.stdout
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                detail = (" | " + self.stderr_tail[-1]) if self.stderr_tail else ""
                raise McpTransportError(f"server closed the stream{detail}")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue  # servers sometimes log to stdout; ignore non-JSON lines

    async def close(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=3.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class _HttpTransport:
    """MCP Streamable HTTP transport (JSON or SSE response bodies)."""

    def __init__(self, url: str, token: str = ""):
        self.url = url
        self.token = token
        self.session_id: str = ""
        self.protocol_version: Optional[str] = None
        self.client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def start(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=10.0))

    async def send(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST a JSON-RPC message; return the JSON-RPC response if the server replies inline."""
        assert self.client is not None
        resp = await self.client.post(self.url, headers=self._headers(), json=message)

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

        if resp.status_code >= 400:
            raise McpTransportError(f"HTTP {resp.status_code} from {self.url}: {resp.text[:200]}")

        # Notifications may legitimately return 202/204 with no body.
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return _parse_sse_for_id(resp.text, message.get("id"))
        if not resp.text.strip():
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        if isinstance(payload, list):
            for item in payload:
                if item.get("id") == message.get("id"):
                    return item
            return None
        return payload

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.aclose()
            except Exception:
                pass
            self.client = None


def _parse_sse_for_id(text: str, msg_id: Any) -> Optional[Dict[str, Any]]:
    """Extract the JSON-RPC response with `msg_id` from an SSE body."""
    last = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("id") == msg_id:
            return obj
        last = obj
    return last if isinstance(last, dict) and "result" in (last or {}) else None


# ---------------------------------------------------------------------------
# Connection — JSON-RPC session with one server
# ---------------------------------------------------------------------------

class McpConnection:
    def __init__(self, name: str, spec: Dict[str, Any]):
        self.name = name
        self.spec = spec
        self.transport: Any = None
        self.tools: List[Dict[str, Any]] = []
        self.server_info: Dict[str, Any] = {}
        self.status = "disconnected"
        self.error = ""
        self._next_id = 0
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        spec = self.spec
        if spec.get("url"):
            self.transport = _HttpTransport(spec["url"], str(spec.get("token", "")))
        elif spec.get("command"):
            self.transport = _StdioTransport(
                spec["command"],
                spec.get("args") or [],
                spec.get("env") or {},
            )
        else:
            raise McpTransportError("server spec needs a 'command' or a 'url'")

        await self.transport.start()
        self.status = "connecting"

        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
                "clientInfo": CLIENT_INFO,
            },
            timeout=HANDSHAKE_TIMEOUT,
        )
        self.server_info = (result or {}).get("serverInfo", {}) or {}
        negotiated = (result or {}).get("protocolVersion")
        if negotiated and isinstance(self.transport, _HttpTransport):
            self.transport.protocol_version = negotiated

        await self._notify("notifications/initialized", {})

        listing = await self._request("tools/list", {}, timeout=HANDSHAKE_TIMEOUT)
        self.tools = _collect_tools(listing)
        self.status = "ready"
        self.error = ""

    async def disconnect(self) -> None:
        if self.transport is not None:
            try:
                await self.transport.close()
            except Exception:
                pass
        self.transport = None
        self.status = "disconnected"

    # -- JSON-RPC ----------------------------------------------------------
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(self, method: str, params: Dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> Any:
        async with self._lock:
            msg_id = self._new_id()
            message = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}

            if isinstance(self.transport, _HttpTransport):
                response = await asyncio.wait_for(self.transport.send(message), timeout=timeout)
                if response is None:
                    raise McpTransportError(f"no response to '{method}'")
            else:
                await self.transport.send(message)
                response = await asyncio.wait_for(self._await_response(msg_id), timeout=timeout)

            if "error" in response:
                err = response["error"] or {}
                raise McpTransportError(f"{err.get('message', 'MCP error')} (code {err.get('code')})")
            return response.get("result")

    async def _await_response(self, msg_id: int) -> Dict[str, Any]:
        while True:
            message = await self.transport.recv()
            # Server-initiated requests get a polite "method not found" so the
            # server never blocks waiting on us.
            if "method" in message and "id" in message:
                try:
                    await self.transport.send({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": f"Client does not support '{message['method']}'"},
                    })
                except Exception:
                    pass
                continue
            if message.get("id") == msg_id:
                return message

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if isinstance(self.transport, _HttpTransport):
            await self.transport.send(message)
        else:
            await self.transport.send(message)

    # -- tools -------------------------------------------------------------
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        result = await self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=REQUEST_TIMEOUT,
        )
        return _format_tool_result(result)


def _collect_tools(listing: Any) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    if isinstance(listing, dict):
        for item in listing.get("tools", []) or []:
            if isinstance(item, dict) and item.get("name"):
                tools.append(item)
    return tools


def _format_tool_result(result: Any) -> str:
    """Flatten an MCP tools/call result into text for the model."""
    if result is None:
        return "(no result)"
    if not isinstance(result, dict):
        return str(result)

    parts: List[str] = []
    for block in result.get("content", []) or []:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(str(block.get("text", "")))
        elif btype in ("image", "audio"):
            parts.append(f"[{btype} content: {block.get('mimeType', 'unknown')} omitted]")
        elif btype == "resource":
            res = block.get("resource") or {}
            parts.append(str(res.get("text") or f"[resource: {res.get('uri', '')}]"))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))

    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))

    text = "\n".join(p for p in parts if p).strip() or "(empty result)"
    if result.get("isError"):
        text = f"Tool reported an error: {text}"
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n... (result truncated)"
    return text


# ---------------------------------------------------------------------------
# Manager — owns every connection, exposes schemas & dispatch
# ---------------------------------------------------------------------------

class McpManager:
    def __init__(self) -> None:
        self.connections: Dict[str, McpConnection] = {}
        # Cache of tool schemas per server, filled at connect time and reused for
        # the whole session. get_tools()/tool_definitions() NEVER re-list tools:
        # tools/list round-trips add latency to every turn for zero benefit
        # (server tool catalogs are stable once negotiated).
        self._tools_cache: Dict[str, List[Dict[str, Any]]] = {}
        # Servers whose connect attempt failed; they are NOT retried on every
        # message (that would stall each turn), only when the user explicitly
        # reconnects (/mcp connect) or calls one of their tools (lazy retry).
        self._failed: Dict[str, str] = {}

    # -- config ------------------------------------------------------------
    @staticmethod
    def servers_config() -> Dict[str, Dict[str, Any]]:
        cfg = load_config()
        raw = cfg.get("mcp_servers", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def _spec(self, name: str) -> Optional[Dict[str, Any]]:
        return self.servers_config().get(name)

    # -- lifecycle ---------------------------------------------------------
    def connect(self, name: str) -> Tuple[bool, str]:
        """Connect a configured server and report the outcome.

        Returns (ok, message) where ok means "the server is now usable". A server
        that connects but exposes a tool list the manager could not finish
        reading (some servers start slowly and answer the handshake before their
        tool catalog is ready) is still reported as SUCCESS with a status
        message, because it *is* added and will be re-probed on first use.
        Reporting it as an error was the "it errored but then shows up in the
        list" bug: persistence succeeded, only the tool probe lagged behind.
        """
        spec = self._spec(name)
        if not spec:
            return False, f"MCP server '{name}' is not configured."
        if spec.get("enabled", True) is False:
            return False, f"MCP server '{name}' is disabled."

        existing = self.connections.get(name)
        if existing and existing.status == "ready":
            if existing.tools:
                return True, self._ready_message(name, existing, cached=True)
            return True, (
                f"MCP server '{name}' is connected but exposes no tools yet."
            )

        conn = McpConnection(name, spec)
        try:
            _run_coro(conn.connect(), timeout=HANDSHAKE_TIMEOUT + 10)
        except Exception as exc:
            err = str(exc)
            # Distinguish "could not establish a session" from "session opened,
            # but the tool catalog was not ready". Only the former is an error.
            if conn.status in ("ready", "connecting") or conn.transport is not None:
                self.connections[name] = conn
                return True, (
                    f"MCP server '{name}' connected, but its tool list could not be "
                    f"read yet ({err[:80]}). It will be retried on first use."
                )
            self.connections[name] = conn
            conn.status = "error"
            conn.error = err
            self._failed[name] = err
            return False, f"MCP server '{name}' failed to connect: {err}"
        self.connections[name] = conn
        if conn.tools:
            self._tools_cache[name] = list(conn.tools)
        self._failed.pop(name, None)
        return True, self._ready_message(name, conn)

    @staticmethod
    def _ready_message(name: str, conn: McpConnection, cached: bool = False) -> str:
        label = conn.server_info.get("name") or name
        version = conn.server_info.get("version")
        ver = f" v{version}" if version else ""
        tools = ", ".join(t["name"] for t in conn.tools) or "none"
        prefix = "Already connected" if cached else "Connected"
        return f"{prefix} to MCP server '{name}' ({label}{ver}) — {len(conn.tools)} tool(s): {tools}"

    def disconnect(self, name: str) -> None:
        conn = self.connections.pop(name, None)
        if conn is not None:
            try:
                _run_coro(conn.disconnect(), timeout=10)
            except Exception:
                pass
        # Cache dies with the connection; a reconnect re-lists once.
        self._tools_cache.pop(name, None)
        self._failed.pop(name, None)
        try:
            from phantombox.tools import invalidate_tool_definitions_cache
            invalidate_tool_definitions_cache()
        except Exception:
            pass

    def connect_all(self) -> List[Tuple[str, bool, str]]:
        """Connect every configured server once (session warm-up, not per message).

        Servers already connected are left untouched; only configured-but-
        unconnected ones are spawned, so calling this repeatedly is cheap.
        """
        results = []
        for name, spec in self.servers_config().items():
            if spec.get("enabled", True) is False:
                continue
            conn = self.connections.get(name)
            if conn is not None and conn.status == "ready":
                continue  # already warm — never restart a healthy child process
            ok, msg = self.connect(name)
            results.append((name, ok, msg))
        return results

    # -- tools -------------------------------------------------------------
    def get_tools(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return tools (namespaced) for one connected server, or all of them.

        Reads from the in-memory cache populated at connect time — no tools/list
        round-trip happens here.
        """
        names = [name] if name else list(self.connections)
        out: List[Dict[str, Any]] = []
        for srv in names:
            conn = self.connections.get(srv)
            if not conn or conn.status != "ready":
                continue
            cached = self._tools_cache.get(srv)
            tool_list = cached if cached is not None else conn.tools
            for tool in tool_list:
                out.append({
                    "server": srv,
                    "name": tool["name"],
                    "qualified_name": f"{srv}__{tool['name']}",
                    "description": tool.get("description", "") or f"{tool['name']} (MCP server {srv})",
                    "input_schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                })
        return out

    def tool_definitions(self) -> List[Dict[str, Any]]:
        """OpenAI-format function schemas for every connected MCP tool."""
        defs: List[Dict[str, Any]] = []
        for tool in self.get_tools():
            schema = tool["input_schema"]
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            # Some servers omit "type"; OpenAI expects an object schema.
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            defs.append({
                "type": "function",
                "function": {
                    "name": tool["qualified_name"],
                    "description": f"[MCP:{tool['server']}] {tool['description']}",
                    "parameters": schema,
                },
            })
        return defs

    def is_mcp_tool(self, tool_name: str) -> bool:
        return _split_qualified(tool_name) is not None

    def call_tool(self, qualified_name: str, arguments: Dict[str, Any]) -> str:
        split = _split_qualified(qualified_name)
        if not split:
            return f"Error: '{qualified_name}' is not an MCP tool name (expected server__tool)."
        server, tool = split
        conn = self.connections.get(server)
        if conn is None or conn.status != "ready":
            # Lazy (re)connect: only pay the spawn + handshake cost when a tool
            # from this server is actually invoked — never during listing.
            if self._spec(server) is not None:
                ok, msg = self.connect(server)
                conn = self.connections.get(server)
                if not ok or conn is None or conn.status != "ready":
                    return f"Error: MCP server '{server}' could not be connected ({msg})"
            else:
                return f"Error: MCP server '{server}' is not connected. Run /mcp list and reconnect it."
        try:
            return _run_coro(conn.call_tool(tool, arguments), timeout=REQUEST_TIMEOUT + 10)
        except Exception as exc:
            return f"MCP tool '{qualified_name}' error: {exc}"

    def connect_failure(self, name: str) -> str:
        """Last connect error for a server ('' if none), for status display."""
        return self._failed.get(name, "")
    def snapshots(self) -> List[Dict[str, Any]]:
        """Config + live state for every configured server (for /mcp list)."""
        rows = []
        for name, spec in self.servers_config().items():
            conn = self.connections.get(name)
            rows.append({
                "name": name,
                "spec": spec,
                "status": conn.status if conn else "not connected",
                "error": conn.error if conn else "",
                "tool_count": len(conn.tools) if conn else 0,
            })
        return rows


def _split_qualified(qualified: str) -> Optional[Tuple[str, str]]:
    """Split 'server__tool' -> ('server', 'tool'), or None if not namespaced."""
    if not isinstance(qualified, str) or "__" not in qualified:
        return None
    server, _, tool = qualified.partition("__")
    if not server or not tool:
        return None
    return server, tool


# ---------------------------------------------------------------------------
# Module-level helpers used by the CLI / chat commands
# ---------------------------------------------------------------------------

_MANAGER: Optional[McpManager] = None


def get_mcp_manager() -> McpManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = McpManager()
    return _MANAGER


def list_mcp_servers() -> Dict[str, Dict[str, Any]]:
    """Configured MCP servers (name -> spec)."""
    return McpManager.servers_config()


def add_mcp_server(name: str, spec: Dict[str, Any]) -> Tuple[bool, str]:
    """Persist an MCP server spec. Does not connect it."""
    clean = (name or "").strip().lower()
    if not clean:
        return False, "Server name cannot be empty."
    if not clean.replace("_", "").replace("-", "").isalnum():
        return False, "Server name must be alphanumeric (underscores/dashes allowed)."
    if not spec.get("command") and not spec.get("url"):
        return False, "Server spec needs either a 'command' (stdio) or a 'url' (http)."

    cfg = load_config()
    servers = dict(cfg.get("mcp_servers", {}) or {})
    if clean in servers:
        existing = servers[clean]
        where = existing.get("url") or " ".join([existing.get("command", "")] + list(existing.get("args", [])))
        return False, (
            f"MCP server '{clean}' already exists ({where}). "
            f"Remove it first with /mcp remove {clean}, or pick another name."
        )
    spec = dict(spec)
    spec.setdefault("enabled", True)
    servers[clean] = spec
    set_config_val("mcp_servers", servers)
    return True, f"MCP server '{clean}' saved."


def remove_mcp_server(name: str) -> Tuple[bool, str]:
    clean = (name or "").strip().lower()
    cfg = load_config()
    servers = dict(cfg.get("mcp_servers", {}) or {})
    if clean not in servers:
        return False, f"MCP server '{clean}' is not configured."
    del servers[clean]
    set_config_val("mcp_servers", servers)
    get_mcp_manager().disconnect(clean)
    return True, f"MCP server '{clean}' removed and disconnected."


def probe_mcp_server(name: str) -> Tuple[bool, str]:
    """Connect a configured server and report its tools (used after /mcp add)."""
    return get_mcp_manager().connect(name)


def is_mcp_tool(tool_name: str) -> bool:
    return get_mcp_manager().is_mcp_tool(tool_name)


def mcp_tool_definitions() -> List[Dict[str, Any]]:
    """OpenAI schemas for MCP tools — from cache only, NEVER connecting servers.

    This runs on every chat turn, so it must not spawn processes or wait on
    handshakes. Servers are connected once at session start (chat.on_mount ->
    connect_all on a worker thread) or lazily on first tool use (call_tool).
    If nothing is connected yet, this returns [] immediately and the tools
    appear on the next turn.
    """
    mgr = get_mcp_manager()
    return mgr.tool_definitions()


def ensure_mcp_connected() -> None:
    """Warm up: connect every configured server once (blocking).

    Call exactly once per session, off the UI thread (e.g. a worker thread at
    chat startup). Idempotent: servers that are already connected are skipped.
    """
    try:
        get_mcp_manager().connect_all()
    except Exception:
        pass


def call_mcp_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    return get_mcp_manager().call_tool(tool_name, arguments)


def shutdown_mcp() -> None:
    """Disconnect every MCP server (called at interpreter exit)."""
    if _MANAGER is not None:
        for name in list(_MANAGER.connections):
            _MANAGER.disconnect(name)


atexit.register(shutdown_mcp)
