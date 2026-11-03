"""A minimal, real MCP server over stdio, used to test phantombox's MCP client.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout exactly like official
MCP servers (initialize, notifications/initialized, tools/list, tools/call).
"""
import json
import sys

TOOLS = [
    {
        "name": "add",
        "description": "Add two integers together.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "first number"},
                "b": {"type": "integer", "description": "second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "echo",
        "description": "Echo text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail_tool",
        "description": "Always reports an error (tests isError handling).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # Notifications have no id and get no reply.
        if msg_id is None:
            continue

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "test-mcp", "version": "1.2.3"},
                },
            })
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            if name == "add":
                total = int(args.get("a", 0)) + int(args.get("b", 0))
                send({"jsonrpc": "2.0", "id": msg_id,
                      "result": {"content": [{"type": "text", "text": str(total)}], "isError": False}})
            elif name == "echo":
                send({"jsonrpc": "2.0", "id": msg_id,
                      "result": {"content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}]}})
            elif name == "fail_tool":
                send({"jsonrpc": "2.0", "id": msg_id,
                      "result": {"content": [{"type": "text", "text": "boom"}], "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": msg_id,
                      "error": {"code": -32602, "message": f"unknown tool '{name}'"}})
        else:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
