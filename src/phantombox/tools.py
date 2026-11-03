"""Local file and directory tools for phantombox runtime.

All AI file executions are strictly scoped to the designated workspace directory
('phantombox-dir'), located safely outside the phantombox-cli codebase.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

from phantombox.config import load_config
from phantombox.env import get_workspace_dir

# Schema definitions for llama.cpp OpenAI-compatible tool call endpoint
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List the contents of a directory (files and subdirectories) inside the phantombox-dir workspace. "
                "YOU HAVE THIS TOOL AND IT WORKS. Call it whenever you need to inspect existing files or "
                "directory structure before reading, writing, or editing anything. Never claim you lack the "
                "ability to inspect the user's files — you can, through this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to phantombox-dir (e.g. '.' for root workspace, or a subfolder name)",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a text file inside the phantombox-dir workspace, with optional line limit. "
                "YOU HAVE THIS TOOL AND IT WORKS. Use it whenever inspecting or reading existing file contents — "
                "source code, configs, notes, logs. Never say you cannot read the user's files; this tool reads them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to phantombox-dir (e.g. 'notes.txt' or 'folder/code.py')",
                    },
                    "limit_lines": {
                        "type": "integer",
                        "description": "Optional maximum number of lines to read",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a brand-new file with initial content inside the phantombox-dir workspace. "
                "YOU HAVE THIS TOOL AND IT WORKS. Whenever writing or saving code, scripts, or text to a NEW file, "
                "you MUST call this function with the full content instead of printing it in conversation. "
                "Never say you cannot write files — this is exactly how you do it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to phantombox-dir to create (e.g. 'script.py')",
                    },
                    "content": {
                        "type": "string",
                        "description": "Initial text content for the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": (
                "Create a new folder/directory inside the phantombox-dir workspace. Use it to build folder "
                "structures before creating files inside them. YOU HAVE THIS TOOL AND IT WORKS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Folder path relative to phantombox-dir to create",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write or completely overwrite a file with the given content inside the phantombox-dir workspace. "
                "YOU HAVE THIS TOOL AND IT WORKS. Whenever saving complete code or rewriting an existing file, "
                "you MUST call this function with the COMPLETE content — never output the file only as text in chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file relative to phantombox-dir",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete text content to write to the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Perform an in-place text replacement inside an existing file in the phantombox-dir workspace. "
                "YOU HAVE THIS TOOL AND IT WORKS. Provide an exact matching old_text snippet and the new_text "
                "replacement. Prefer this over rewriting whole files for small targeted edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file relative to phantombox-dir",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text snippet to find and replace",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Text to replace old_text with",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "internet_search",
            "description": (
                "Search the LIVE web (DuckDuckGo) for fresh facts, recent events, breaking news, dates, real-time "
                "sports/politics/tech info, prices, weather, or technical documentation. "
                "YOU HAVE THIS TOOL AND IT WORKS — you ARE capable of retrieving real-time information, so never "
                "claim you cannot access current information or that your knowledge cutoff prevents answering. "
                "Be proactive like Grok/Perplexity: whenever a query involves current events, specific dates/years, "
                "recent developments, people, releases, prices, weather or sports scores — or whenever your built-in "
                "knowledge might be outdated or uncertain — ALWAYS call this tool autonomously BEFORE answering, "
                "even if the user did not explicitly ask you to search. Then answer using the returned results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Specific search keywords or question to search online",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Optional number of search result pages/snippets to return (e.g. 3, 5, 8, 10). Leave unspecified or adjust as needed.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Execute a terminal/shell command inside the designated phantombox-dir workspace. "
                "YOU HAVE THIS TOOL AND IT WORKS. Use it to run scripts, build/test code, install packages, "
                "check versions, or inspect system info. Never claim you cannot execute commands or run code — "
                "this tool does exactly that (e.g. 'python script.py', 'npm test', 'pip list')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The exact shell command to execute in phantombox-dir (e.g. 'python script.py' or 'npm test')",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "Move or rename a file or directory inside the phantombox-dir workspace. "
                "YOU HAVE THIS TOOL AND IT WORKS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Source path relative to phantombox-dir to move/rename",
                    },
                    "destination_path": {
                        "type": "string",
                        "description": "Destination path relative to phantombox-dir",
                    },
                },
                "required": ["source_path", "destination_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Delete a file or folder inside the phantombox-dir workspace. "
                "YOU HAVE THIS TOOL AND IT WORKS. Confirm intent before deleting anything irreversible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to phantombox-dir of the file or folder to delete",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_subagent",
            "description": (
                "Delegate a sub-task (background operation, terminal execution, code review, or research query) "
                "to a registered subagent by its exact nickname. "
                "YOU HAVE THIS TOOL AND IT WORKS. The CURRENT VALID SUBAGENT NICKNAMES are injected into the "
                "parameter descriptions below — never invent nicknames and never claim you cannot delegate work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nickname": {
                        "type": "string",
                        "description": "Nickname of the subagent to call",
                    },
                    "task": {
                        "type": "string",
                        "description": "Detailed description or instructions for the task the subagent should perform",
                    },
                },
                "required": ["nickname", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": "Copy a file or folder (recursively) to a new path inside the phantombox-dir workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {"type": "string", "description": "Source path relative to phantombox-dir"},
                    "destination_path": {"type": "string", "description": "Destination path relative to phantombox-dir"},
                },
                "required": ["source_path", "destination_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search file CONTENTS for a text/regex pattern across the phantombox-dir workspace (like grep). Returns matching file paths, line numbers and line text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regex pattern to search for"},
                    "path": {"type": "string", "description": "Optional subdirectory to search (default: workspace root)"},
                    "glob": {"type": "string", "description": "Optional filename filter like '*.py'"},
                    "max_results": {"type": "integer", "description": "Optional cap on matches returned (default 50)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": "Get metadata about a file/folder in the phantombox-dir workspace: size, created/modified times, type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to phantombox-dir"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compress_archive",
            "description": "Create a .zip archive inside the phantombox-dir workspace from a file or folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or folder to compress, relative to phantombox-dir"},
                    "archive_path": {"type": "string", "description": "Output .zip path relative to phantombox-dir"},
                },
                "required": ["path", "archive_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_archive",
            "description": "Extract a .zip archive into a destination folder inside the phantombox-dir workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the .zip archive relative to phantombox-dir"},
                    "destination_path": {"type": "string", "description": "Destination folder relative to phantombox-dir (created if missing)"},
                },
                "required": ["path", "destination_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Run 'git status --short --branch' in the phantombox-dir workspace and return the result.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Run 'git diff' in the phantombox-dir workspace and return the unified diff of unstaged changes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show the recent commit history (oneline graph) of the git repository in the phantombox-dir workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of commits to show (default 10, max 50)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all current changes (git add -A) and create a git commit with the given message in the phantombox-dir workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch",
            "description": "List local git branches in the phantombox-dir workspace, or create a new branch with the optional 'name' argument.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Optional: create a new branch with this name"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout",
            "description": "Switch to an existing git branch in the phantombox-dir workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "description": "Branch name to check out"},
                },
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute a Python script file from the phantombox-dir workspace with the system python interpreter and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Script path relative to phantombox-dir (e.g. 'script.py')"},
                    "args": {"type": "string", "description": "Optional extra command-line arguments for the script"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_node",
            "description": "Execute a JavaScript file from the phantombox-dir workspace with Node.js and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Script path relative to phantombox-dir (e.g. 'index.js')"},
                    "args": {"type": "string", "description": "Optional extra command-line arguments"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Return basic host system info: OS, CPU, RAM, python version, current user, machine architecture.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_port",
            "description": "Check whether a TCP port is open/in use on localhost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {"type": "integer", "description": "Port number to check (1-65535)"},
                    "host": {"type": "string", "description": "Optional host (default '127.0.0.1')"},
                },
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_env",
            "description": "Read one environment variable by name, or list all environment variable NAMES when no name is given (values hidden in list mode).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Environment variable name (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a web page by URL and return its readable text content (HTML tags stripped, truncated). Use for reading documentation, articles or any public page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full http(s) URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Optional max characters of text to return (default 6000)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a location (no API key needed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City or 'city, country' (e.g. 'Berlin' or 'New York')"},
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a math expression safely (numbers and + - * / % ** parentheses, no functions/variables). Use for any arithmetic instead of computing by hand.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Math expression, e.g. '(1234 * 56) / 7'"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_units",
            "description": "Convert a value between common units (length, mass, temperature, data size), e.g. km->mi, kg->lb, c->f, gb->mb.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "number", "description": "Numeric value to convert"},
                    "from_unit": {"type": "string", "description": "Source unit (e.g. 'km', 'lb', 'c', 'gb')"},
                    "to_unit": {"type": "string", "description": "Target unit (e.g. 'mi', 'kg', 'f', 'mb')"},
                },
                "required": ["value", "from_unit", "to_unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timezone_convert",
            "description": "Convert a time between IANA timezones using the stdlib zoneinfo database, e.g. 14:30 from 'America/New_York' to 'Europe/Berlin'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time": {"type": "string", "description": "Time in HH:MM (24h). Omit to use the current UTC time."},
                    "from_tz": {"type": "string", "description": "Source IANA timezone (default 'UTC')"},
                    "to_tz": {"type": "string", "description": "Target IANA timezone"},
                },
                "required": ["to_tz"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_uuid",
            "description": "Generate one or more random UUIDs (version 4).",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "How many UUIDs (default 1, max 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hash_text",
            "description": "Hash or encode text: algorithms md5, sha1, sha256, sha512, or encodings base64 / base64-decode / hex / url-encode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to hash or encode"},
                    "algorithm": {"type": "string", "description": "One of: md5, sha1, sha256, sha512, base64, base64-decode, hex, url-encode (default sha256)"},
                },
                "required": ["text"],
            },
        },
    },
]


_TOOL_DEFS_CACHE: Optional[List[Dict[str, Any]]] = None
_TOOL_DEFS_CACHE_KEY: Optional[Tuple[Any, ...]] = None


def invalidate_tool_definitions_cache() -> None:
    """Drop the cached tool schema list (call after /subagents or /mcp changes)."""
    global _TOOL_DEFS_CACHE, _TOOL_DEFS_CACHE_KEY
    _TOOL_DEFS_CACHE = None
    _TOOL_DEFS_CACHE_KEY = None


def get_tool_definitions() -> List[Dict[str, Any]]:
    """Return tool schema definitions filtered by active configuration, updating call_subagent description with available nicknames.

    Includes tools exposed by connected MCP servers (namespaced '<server>__<tool>')
    alongside PhantomBox's built-in local tools.

    The full list is cached and rebuilt only when something that affects it
    changes (internet-search setting, registered subagents, connected MCP
    servers). This runs on every chat turn, so a rebuilt-every-time schema list
    was pure overhead; cache reads keep per-turn tool preparation at ~zero cost.
    """
    global _TOOL_DEFS_CACHE, _TOOL_DEFS_CACHE_KEY

    try:
        from phantombox.mcp import get_mcp_manager
        mgr = get_mcp_manager()
        mcp_sig = tuple(sorted(
            (srv, tuple(t["qualified_name"] for t in mgr.get_tools(srv)))
            for srv in list(mgr.connections)
        ))
    except Exception:
        mgr = None  # type: ignore[assignment]
        mcp_sig = ()

    try:
        from phantombox.subagents import list_subagents
        nick_names = tuple(sorted(list_subagents().keys()))
    except Exception:
        nick_names = ()

    try:
        allow_search = bool(load_config().get("enable_internet_search", True))
    except Exception:
        allow_search = True

    cache_key = (allow_search, nick_names, mcp_sig)
    if _TOOL_DEFS_CACHE is not None and _TOOL_DEFS_CACHE_KEY == cache_key:
        return _TOOL_DEFS_CACHE

    tools: List[Dict[str, Any]] = []
    for t in TOOL_DEFINITIONS:
        fn_name = t.get("function", {}).get("name")
        if fn_name == "internet_search" and not allow_search:
            continue

        if fn_name == "call_subagent":
            # Deep copy and enrich description with current valid nicknames
            t_copy = {
                "type": "function",
                "function": {
                    "name": "call_subagent",
                    "description": (
                        "Delegate a sub-task, background operation, terminal execution, code review, "
                        "or research query to a registered subagent by nickname. "
                        + (f"CURRENT VALID SUBAGENT NICKNAMES: {list(nick_names)}." if nick_names else "NO SUBAGENTS ARE CURRENTLY REGISTERED. Do not call this tool until a subagent is set up.")
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nickname": {
                                "type": "string",
                                "description": f"The EXACT nickname of the subagent to call. Must be one of: {list(nick_names)}" if nick_names else "Nickname of subagent",
                            },
                            "task": {
                                "type": "string",
                                "description": "Detailed description or instructions for the task the subagent should perform",
                            },
                        },
                        "required": ["nickname", "task"],
                    },
                },
            }
            tools.append(t_copy)
        else:
            tools.append(t)

    # Tools from connected MCP servers (never fatal: a dead server must not
    # remove PhantomBox's own tools from the model).
    try:
        from phantombox.mcp import mcp_tool_definitions
        tools.extend(mcp_tool_definitions())
    except Exception:
        pass

    _TOOL_DEFS_CACHE = tools
    _TOOL_DEFS_CACHE_KEY = cache_key
    return tools


def _contains_sensitive_args(arguments: Dict[str, Any], depth: int = 0) -> bool:
    """True when a tool call carries something that looks like a credential.

    Used to make the approval prompt louder (and to keep the 'sensitive' nature
    visible) instead of silently forwarding secrets to a third-party server.
    """
    if depth > 3 or not isinstance(arguments, dict):
        return False
    sensitive_keys = ("password", "passwd", "secret", "token", "api_key", "apikey", "authorization", "private_key")
    for key, value in arguments.items():
        k = str(key).lower()
        if any(s in k for s in sensitive_keys) and isinstance(value, str) and value.strip():
            return True
        if isinstance(value, dict) and _contains_sensitive_args(value, depth + 1):
            return True
    return False


def describe_tool_call(tool_name: str) -> str:
    """Short human-readable description of what a tool does, for approval prompts.

    For MCP tools the description comes from the server's own schema, prefixed
    with the owning server so the user always knows who is asking to act.
    """
    from phantombox.mcp import get_mcp_manager

    mgr = get_mcp_manager()
    if mgr.is_mcp_tool(tool_name):
        server = tool_name.split("__", 1)[0]
        for tool in mgr.get_tools(server):
            if tool["qualified_name"] == tool_name:
                return f"[MCP:{server}] {tool['description']}"
        return "(unknown tool)"

    for t in TOOL_DEFINITIONS:
        fn = t.get("function", {})
        if fn.get("name") == tool_name:
            desc = fn.get("description", "")
            # Descriptions are long and contain model-directed instructions;
            # keep the prompt readable.
            return desc.split(". ")[0].strip() + ("." if not desc.startswith(" ") else "")
    return "(unknown tool)"


def resolve_workspace_path(subpath: str) -> Path:
    """Resolve subpath safely against get_workspace_dir(), preventing directory traversal."""
    ws = get_workspace_dir().resolve()
    clean = (subpath or ".").strip()
    # Normalize forward/back slashes
    if clean in ("", ".", "/"):
        return ws
    # Prevent absolute path escaping
    target = (ws / clean).resolve()
    try:
        target.relative_to(ws)
    except ValueError:
        # Traversal attempt outside workspace
        return ws
    return target


def _is_path_allowed(target_path: Path, allowed_patterns: List[str]) -> bool:
    """Check if target_path is allowed under allowed_patterns within phantombox-dir."""
    ws = get_workspace_dir().resolve()

    # Must be inside workspace
    try:
        rel = target_path.relative_to(ws)
    except ValueError:
        return False

    if "*" in allowed_patterns:
        return True

    rel_str = str(rel).replace("\\", "/").lower()

    for pattern in allowed_patterns:
        pat = pattern.strip().lower()
        if not pat:
            continue
        if pat in ("*", ".", "/"):
            return True
        pat_clean = pat.strip("/\\")
        if rel_str == pat_clean or rel_str.startswith(pat_clean + "/"):
            return True

    return False


def check_tool_permission(tool_name: str, arguments: Dict[str, Any]) -> Tuple[bool, str, str]:
    """
    Validate tool call against workspace scope and determine confirmation requirement.
    Returns: (is_allowed_by_scope, reason_or_error, requirement)
    requirement: 'allowed_auto' | 'needs_confirmation' | 'denied'
    """
    cfg = load_config()
    mode = cfg.get("permission_mode", "restricted")
    allowed_reads = cfg.get("allowed_read_paths", ["*"])
    allowed_writes = cfg.get("allowed_write_paths", ["*"])
    auto_tools = cfg.get("custom_auto_approve_tools", ["list_dir", "read_file", "create_file", "create_folder", "internet_search"])

    # MCP tools run on a third-party server and can do anything that server can do,
    # so they are NEVER auto-approved unless the user explicitly listed them under
    # mcp_auto_approve. Full-access mode is the only blanket override.
    from phantombox.mcp import is_mcp_tool
    if is_mcp_tool(tool_name):
        if mode == "full_access":
            return True, "Full access mode: MCP tool auto-approved", "allowed_auto"
        if tool_name in (cfg.get("mcp_auto_approve", []) or []):
            return True, f"MCP tool '{tool_name}' is explicitly auto-approved", "allowed_auto"
        server = tool_name.split("__", 1)[0]
        hint = ""
        if _contains_sensitive_args(arguments):
            hint = " — arguments reference credentials/secrets"
        return True, f"MCP tool from server '{server}' requires confirmation{hint}", "needs_confirmation"

    # Online search check
    if tool_name == "internet_search":
        if not cfg.get("enable_internet_search", True):
            return False, "Internet search is disabled in settings.", "denied"
        if mode in ("full_access", "restricted"):
            # Internet search is safe read-only; auto-approve by default so small models can search without friction
            return True, "Internet search auto-approved", "allowed_auto"
        if mode == "custom":
            if tool_name in auto_tools:
                return True, "Custom mode: 'internet_search' is auto-approved", "allowed_auto"
            return True, "Custom mode: 'internet_search' requires confirmation", "needs_confirmation"
        return True, "Internet search allowed", "allowed_auto"

    # Terminal command check
    if tool_name in ("run_command", "execute_command"):
        if mode == "full_access":
            return True, "Full access mode: terminal command auto-approved", "allowed_auto"
        if mode == "custom":
            if tool_name in auto_tools or "run_command" in auto_tools:
                return True, f"Custom mode: '{tool_name}' is auto-approved", "allowed_auto"
            return True, f"Custom mode: '{tool_name}' requires confirmation", "needs_confirmation"
        # Restricted mode requires user confirmation
        return True, "Restricted mode: running terminal commands requires confirmation", "needs_confirmation"

    # Move file check
    if tool_name == "move_file":
        ws = get_workspace_dir().resolve()
        src_str = arguments.get("source_path", "")
        dst_str = arguments.get("destination_path", "")
        res_src = resolve_workspace_path(src_str)
        res_dst = resolve_workspace_path(dst_str)
        try:
            res_src.relative_to(ws)
            res_dst.relative_to(ws)
        except ValueError:
            return False, f"Access denied: paths must stay inside phantombox-dir ({ws}).", "denied"
        if not _is_path_allowed(res_src, allowed_writes) or not _is_path_allowed(res_dst, allowed_writes):
            return False, f"Path outside allowed write scope: {allowed_writes}", "denied"
        if mode == "full_access":
            return True, "Full access mode: auto-approved", "allowed_auto"
        if mode == "custom":
            if tool_name in auto_tools:
                return True, "Custom mode: move_file auto-approved", "allowed_auto"
            return True, "Custom mode: move_file requires confirmation", "needs_confirmation"
        return True, "Restricted mode: move_file requires confirmation", "needs_confirmation"

    # Call subagent check
    if tool_name == "call_subagent":
        return True, "Subagent delegation allowed", "allowed_auto"

    # Safe read-only / informational tools: auto-approve in all modes
    if tool_name in (
        "get_file_info", "search_files", "git_status", "git_diff", "git_log", "git_branch",
        "get_system_info", "check_port", "get_env", "calculate", "convert_units",
        "timezone_convert", "generate_uuid", "hash_text", "web_fetch", "get_weather",
    ):
        return True, f"'{tool_name}' is read-only/informational: auto-approved", "allowed_auto"

    # Harmless workspace utilities that create new content but touch nothing existing
    if tool_name == "compress_archive":
        if mode == "restricted":
            return True, "Restricted mode: compress_archive requires confirmation", "needs_confirmation"
        return True, "Utility tool: auto-approved", "allowed_auto"

    target_path_str = arguments.get("path", ".")
    resolved_path = resolve_workspace_path(target_path_str)

    # Verify path hasn't escaped the workspace
    ws = get_workspace_dir().resolve()
    try:
        resolved_path.relative_to(ws)
    except ValueError:
        return False, f"Access denied: path '{target_path_str}' is outside phantombox-dir ({ws}).", "denied"

    # 1. Path scope check
    if tool_name in ("list_dir", "read_file"):
        if not _is_path_allowed(resolved_path, allowed_reads):
            return False, f"Path '{target_path_str}' is outside allowed read scope: {allowed_reads}", "denied"
    elif tool_name in ("write_file", "edit_file", "create_file", "create_folder", "delete_file"):
        if not _is_path_allowed(resolved_path, allowed_writes):
            return False, f"Path '{target_path_str}' is outside allowed write scope: {allowed_writes}", "denied"

    # 2. Permission level check
    if mode == "full_access":
        return True, "Full access mode: auto-approved", "allowed_auto"

    if mode == "restricted":
        return True, "Restricted mode: requires user confirmation", "needs_confirmation"

    if mode == "custom":
        if tool_name in auto_tools:
            return True, f"Custom mode: '{tool_name}' is auto-approved", "allowed_auto"
        return True, f"Custom mode: '{tool_name}' requires confirmation", "needs_confirmation"

    # Default fallback
    return True, "Requires confirmation", "needs_confirmation"


def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute the validated tool inside phantombox-dir and return output string."""
    try:
        path_str = arguments.get("path", ".")
        target_path = resolve_workspace_path(path_str)
        ws = get_workspace_dir().resolve()

        if tool_name == "list_dir":
            if not target_path.exists():
                return f"Error: Directory '{path_str}' does not exist in phantombox-dir."
            if not target_path.is_dir():
                return f"Error: Path '{path_str}' is not a directory."

            items = []
            for entry in sorted(target_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                prefix = "[DIR] " if entry.is_dir() else "      "
                size_str = ""
                if entry.is_file():
                    try:
                        size_str = f" ({entry.stat().st_size} bytes)"
                    except Exception:
                        pass
                items.append(f"{prefix} {entry.name}{size_str}")

            rel_display = target_path.relative_to(ws)
            header = f"Directory listing of phantombox-dir/{rel_display}:\n" if str(rel_display) != "." else "Directory listing of phantombox-dir:\n"
            return header + ("\n".join(items) if items else "(empty directory)")

        elif tool_name == "read_file":
            if not target_path.exists():
                return f"Error: File '{path_str}' does not exist in phantombox-dir."
            if not target_path.is_file():
                return f"Error: Path '{path_str}' is not a regular file."

            limit = arguments.get("limit_lines")
            content = target_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            if limit and limit > 0 and len(lines) > limit:
                truncated = "\n".join(lines[:limit])
                return f"{truncated}\n... ({len(lines) - limit} more lines truncated)"
            return content

        elif tool_name == "create_file":
            content = arguments.get("content", "")
            if target_path.exists():
                return f"Error: File '{path_str}' already exists in phantombox-dir. Use write_file or edit_file instead."
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            return f"Successfully created file '{target_path.relative_to(ws)}' in phantombox-dir ({len(content)} chars)."

        elif tool_name == "create_folder":
            if target_path.exists():
                if target_path.is_dir():
                    return f"Folder '{path_str}' already exists in phantombox-dir."
                return f"Error: Path '{path_str}' already exists and is a file."
            target_path.mkdir(parents=True, exist_ok=True)
            return f"Successfully created folder '{target_path.relative_to(ws)}' in phantombox-dir."

        elif tool_name == "write_file":
            content = arguments.get("content", "")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} characters to '{target_path.relative_to(ws)}'."

        elif tool_name == "edit_file":
            if not target_path.exists():
                return f"Error: File '{path_str}' does not exist in phantombox-dir."
            old_text = arguments.get("old_text", "")
            new_text = arguments.get("new_text", "")
            if not old_text:
                return "Error: 'old_text' cannot be empty."

            content = target_path.read_text(encoding="utf-8")
            if old_text not in content:
                return f"Error: 'old_text' was not found in '{target_path.relative_to(ws)}'."

            updated = content.replace(old_text, new_text, 1)
            target_path.write_text(updated, encoding="utf-8")
            return f"Successfully replaced text in '{target_path.relative_to(ws)}'."

        elif tool_name == "internet_search":
            query = arguments.get("query", "").strip()
            if not query:
                return "Error: 'query' parameter is required for internet_search."
            raw_max = arguments.get("max_results")
            if raw_max is not None:
                try:
                    max_results = max(1, min(int(raw_max), 20))
                except Exception:
                    max_results = None
            else:
                max_results = None

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post("https://lite.duckduckgo.com/lite/", data={"q": query}, headers=headers)
                    if resp.status_code != 200:
                        return f"Search failed with status {resp.status_code}. No internet results found."

                    html = resp.text
                    links = re.findall(r'<a[^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>', html)
                    snippets = re.findall(r'<td[^>]+class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>', html, re.DOTALL)

                    valid_links = []
                    for href, title_html in links:
                        if href.startswith(("http://", "https://", "/l/?")):
                            if "/l/?" in href:
                                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                                actual_url = parsed.get("uddg", [href])[0]
                            else:
                                actual_url = href
                            title = re.sub(r"<[^>]+>", "", unescape(title_html)).strip()
                            if actual_url and title and not actual_url.startswith("https://duckduckgo.com"):
                                valid_links.append((actual_url, title))

                    # If model didn't specify max_results, return all available matches (typically 4-10 on DDG Lite)
                    total_available = len(valid_links)
                    if max_results is not None:
                        found_count = min(total_available, max_results)
                    else:
                        found_count = total_available

                    if found_count == 0:
                        return f"No internet search results found for query '{query}'."

                    out = [f"Web search results for: '{query}' ({found_count} pages found)\n"]
                    for idx in range(found_count):
                        url, title = valid_links[idx]
                        snippet = ""
                        if idx < len(snippets):
                            snippet = re.sub(r"<[^>]+>", "", unescape(snippets[idx])).strip()
                            snippet = re.sub(r"\s+", " ", snippet)
                        out.append(f"[{idx+1}] {title}\nURL: {url}\nSummary: {snippet}\n")

                    return "\n".join(out)
            except Exception as search_err:
                return f"Internet search error: {search_err}"

        elif tool_name in ("run_command", "execute_command"):
            cmd = arguments.get("command", "").strip()
            if not cmd:
                return "Error: 'command' parameter is required for run_command."

            import subprocess
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(ws),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                output = stdout + ("\n" + stderr if stderr else "")
                output = output.strip()

                if len(output) > 4000:
                    output = output[:4000] + "\n... (output truncated at 4000 chars)"

                exit_str = f"Exit code: {result.returncode}"
                if output:
                    return f"{output}\n\n[{exit_str}]"
                return f"[Command executed with {exit_str} and no output]"
            except subprocess.TimeoutExpired:
                return f"Error: Command '{cmd}' timed out after 60 seconds."
            except Exception as cmd_err:
                return f"Terminal execution error: {cmd_err}"

        elif tool_name == "move_file":
            src_str = arguments.get("source_path", "").strip()
            dst_str = arguments.get("destination_path", "").strip()
            if not src_str or not dst_str:
                return "Error: 'source_path' and 'destination_path' are required for move_file."
            src_path = resolve_workspace_path(src_str)
            dst_path = resolve_workspace_path(dst_str)
            if not src_path.exists():
                return f"Error: Source '{src_str}' does not exist in phantombox-dir."
            import shutil
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            return f"Successfully moved '{src_path.name}' to '{dst_path.relative_to(ws)}'."

        elif tool_name == "delete_file":
            if not target_path.exists():
                return f"Error: Path '{path_str}' does not exist in phantombox-dir."
            import shutil
            if target_path.is_dir():
                shutil.rmtree(str(target_path))
                return f"Successfully deleted directory '{target_path.relative_to(ws)}' from phantombox-dir."
            else:
                target_path.unlink()
                return f"Successfully deleted file '{target_path.relative_to(ws)}' from phantombox-dir."

        elif tool_name == "call_subagent":
            nickname = arguments.get("nickname", "").strip().lower()
            task = arguments.get("task", "").strip()
            if not nickname or not task:
                return "Error: 'nickname' and 'task' are required for call_subagent."

            from phantombox.subagents import list_subagents, get_subagent, run_subagent_task
            active_agents = list_subagents()
            if not active_agents:
                return "Error: No subagents are currently registered. The user must first set one up using /subagents setup <nickname> <model>."

            if nickname not in active_agents:
                valid_nicks = list(active_agents.keys())
                return f"Error: Subagent '{nickname}' does not exist. You MUST use one of the available registered subagents: {valid_nicks}."

            import asyncio

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            if loop.is_running():
                # Running inside active loop
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, run_subagent_task(nickname, task))
                    res = future.result()
            else:
                res = loop.run_until_complete(run_subagent_task(nickname, task))

            return f"[Subagent '{nickname}' Result]:\n{res}"

        elif tool_name == "copy_file":
            src_str = arguments.get("source_path", "").strip()
            dst_str = arguments.get("destination_path", "").strip()
            if not src_str or not dst_str:
                return "Error: 'source_path' and 'destination_path' are required for copy_file."
            import shutil
            src_path = resolve_workspace_path(src_str)
            dst_path = resolve_workspace_path(dst_str)
            if not src_path.exists():
                return f"Error: Source '{src_str}' does not exist in phantombox-dir."
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if src_path.is_dir():
                shutil.copytree(str(src_path), str(dst_path), dirs_exist_ok=True)
            else:
                shutil.copy2(str(src_path), str(dst_path))
            return f"Successfully copied '{src_path.name}' to '{dst_path.relative_to(ws)}'."

        elif tool_name == "search_files":
            pattern = arguments.get("pattern", "")
            if not pattern:
                return "Error: 'pattern' is required for search_files."
            search_root = resolve_workspace_path(arguments.get("path", "."))
            if not search_root.exists() or not search_root.is_dir():
                return f"Error: Search path '{arguments.get('path', '.')}' is not a directory in phantombox-dir."
            glob_filter = (arguments.get("glob") or "*").strip() or "*"
            try:
                max_results = max(1, min(int(arguments.get("max_results", 50)), 200))
            except Exception:
                max_results = 50
            try:
                rx = re.compile(pattern)
            except re.error as rx_err:
                return f"Error: invalid regex pattern: {rx_err}"
            matches = []
            for fpath in sorted(search_root.rglob(glob_filter)):
                if not fpath.is_file():
                    continue
                try:
                    if fpath.stat().st_size > 2_000_000:
                        continue
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        try:
                            rel = fpath.relative_to(ws)
                        except ValueError:
                            rel = fpath
                        matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
            if not matches:
                return f"No matches for pattern '{pattern}' in phantombox-dir."
            return f"Found {len(matches)} match(es) for '{pattern}':\n" + "\n".join(matches)

        elif tool_name == "get_file_info":
            if not target_path.exists():
                return f"Error: Path '{path_str}' does not exist in phantombox-dir."
            import datetime
            st = target_path.stat()
            kind = "directory" if target_path.is_dir() else "file"
            created = datetime.datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
            modified = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            return (
                f"File info for '{target_path.relative_to(ws)}':\n"
                f"  type: {kind}\n"
                f"  size: {st.st_size} bytes\n"
                f"  created: {created}\n"
                f"  modified: {modified}"
            )

        elif tool_name == "compress_archive":
            src_str = arguments.get("path", "").strip()
            arc_str = arguments.get("archive_path", "").strip()
            if not src_str or not arc_str:
                return "Error: 'path' and 'archive_path' are required for compress_archive."
            import zipfile
            src_path = resolve_workspace_path(src_str)
            arc_path = resolve_workspace_path(arc_str)
            if not src_path.exists():
                return f"Error: Source '{src_str}' does not exist in phantombox-dir."
            if arc_path.suffix.lower() != ".zip":
                return "Error: 'archive_path' must end with .zip"
            arc_path.parent.mkdir(parents=True, exist_ok=True)
            file_count = 0
            with zipfile.ZipFile(arc_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if src_path.is_file():
                    zf.write(src_path, src_path.name)
                    file_count = 1
                else:
                    for fpath in src_path.rglob("*"):
                        if fpath.is_file():
                            zf.write(fpath, fpath.relative_to(src_path.parent))
                            file_count += 1
            return f"Created archive '{arc_path.relative_to(ws)}' with {file_count} file(s)."

        elif tool_name == "extract_archive":
            dst_str = arguments.get("destination_path", "").strip()
            if not dst_str:
                return "Error: 'destination_path' is required for extract_archive."
            import zipfile
            if not target_path.exists() or not target_path.is_file():
                return f"Error: Archive '{path_str}' does not exist in phantombox-dir."
            dst_path = resolve_workspace_path(dst_str)
            dst_path.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(target_path) as zf:
                    zf.extractall(dst_path)
                return f"Extracted '{target_path.name}' to '{dst_path.relative_to(ws)}'."
            except zipfile.BadZipFile:
                return "Error: Not a valid .zip archive."

        elif tool_name in ("git_status", "git_diff", "git_log", "git_commit", "git_branch", "git_checkout"):
            import subprocess
            git_args: List[str] = []
            if tool_name == "git_status":
                git_args = ["status", "--short", "--branch"]
            elif tool_name == "git_diff":
                git_args = ["diff"]
            elif tool_name == "git_log":
                raw_count = arguments.get("count", 10)
                try:
                    count = max(1, min(int(raw_count), 50))
                except Exception:
                    count = 10
                git_args = ["log", "--oneline", "--graph", "--decorate", f"-n{count}"]
            elif tool_name == "git_commit":
                message = (arguments.get("message") or "").strip()
                if not message:
                    return "Error: 'message' is required for git_commit."
                git_args = ["commit", "-m", message]
            elif tool_name == "git_branch":
                branch_name = (arguments.get("name") or "").strip()
                git_args = ["branch", branch_name] if branch_name else ["branch"]
            elif tool_name == "git_checkout":
                branch = (arguments.get("branch") or "").strip()
                if not branch:
                    return "Error: 'branch' is required for git_checkout."
                git_args = ["checkout", branch]
            try:
                if tool_name == "git_commit":
                    subprocess.run(["git", "add", "-A"], cwd=str(ws), capture_output=True, text=True, timeout=30)
                result = subprocess.run(["git"] + git_args, cwd=str(ws), capture_output=True, text=True, timeout=30)
                out = (result.stdout or "").strip()
                err = (result.stderr or "").strip()
                if result.returncode != 0:
                    return f"Git error: {err or 'unknown failure'}"
                return out if out else f"[{tool_name}: no output]"
            except FileNotFoundError:
                return "Error: git is not installed or not on PATH."
            except subprocess.TimeoutExpired:
                return "Error: git command timed out."
            except Exception as git_err:
                return f"Git execution error: {git_err}"

        elif tool_name == "run_python":
            script_str = arguments.get("path", "").strip()
            if not script_str:
                return "Error: 'path' is required for run_python."
            import subprocess
            script_path = resolve_workspace_path(script_str)
            if not script_path.exists() or not script_path.is_file():
                return f"Error: Script '{script_str}' does not exist in phantombox-dir."
            extra = (arguments.get("args") or "").strip()
            cmd = "python " + str(script_path) + ((" " + extra) if extra else "")
            try:
                result = subprocess.run(cmd, shell=True, cwd=str(ws), capture_output=True, text=True, timeout=120)
                out = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
                if len(out) > 4000:
                    out = out[:4000] + "\n... (output truncated at 4000 chars)"
                return f"{out}\n\n[Exit code: {result.returncode}]" if out else f"[Exit code: {result.returncode}, no output]"
            except subprocess.TimeoutExpired:
                return "Error: Python script timed out after 120 seconds."
            except Exception as py_err:
                return f"Python execution error: {py_err}"

        elif tool_name == "run_node":
            script_str = arguments.get("path", "").strip()
            if not script_str:
                return "Error: 'path' is required for run_node."
            import subprocess
            script_path = resolve_workspace_path(script_str)
            if not script_path.exists() or not script_path.is_file():
                return f"Error: Script '{script_str}' does not exist in phantombox-dir."
            extra = (arguments.get("args") or "").strip()
            cmd = "node " + str(script_path) + ((" " + extra) if extra else "")
            try:
                result = subprocess.run(cmd, shell=True, cwd=str(ws), capture_output=True, text=True, timeout=120)
                out = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
                if len(out) > 4000:
                    out = out[:4000] + "\n... (output truncated at 4000 chars)"
                return f"{out}\n\n[Exit code: {result.returncode}]" if out else f"[Exit code: {result.returncode}, no output]"
            except FileNotFoundError:
                return "Error: Node.js (node) is not installed or not on PATH."
            except subprocess.TimeoutExpired:
                return "Error: Node script timed out after 120 seconds."
            except Exception as node_err:
                return f"Node execution error: {node_err}"

        elif tool_name == "get_system_info":
            import platform
            import getpass
            import datetime
            return (
                "System info:\n"
                f"  os: {platform.system()} {platform.release()} ({platform.version()})\n"
                f"  machine: {platform.machine()}\n"
                f"  cpu: {platform.processor() or 'unknown'}\n"
                f"  python: {platform.python_version()}\n"
                f"  user: {getpass.getuser()}\n"
                f"  hostname: {platform.node()}\n"
                f"  local time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

        elif tool_name == "check_port":
            import socket
            raw_port = arguments.get("port")
            try:
                port = int(raw_port)
            except Exception:
                return "Error: 'port' must be an integer (1-65535)."
            if not (1 <= port <= 65535):
                return "Error: 'port' must be between 1 and 65535."
            host = (arguments.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.5)
            try:
                if sock.connect_ex((host, port)) == 0:
                    return f"Port {port} on {host} is OPEN (something is listening)."
                return f"Port {port} on {host} is closed/free."
            finally:
                sock.close()

        elif tool_name == "get_env":
            name = (arguments.get("name") or "").strip()
            if name:
                value = os.environ.get(name)
                if value is None:
                    return f"Environment variable '{name}' is not set."
                return f"{name}={value}"
            names = sorted(os.environ.keys())
            return f"{len(names)} environment variables set (use get_env with 'name' to read one):\n" + ", ".join(names)

        elif tool_name == "web_fetch":
            url = (arguments.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return "Error: 'url' must start with http:// or https://"
            try:
                max_chars = max(500, min(int(arguments.get("max_chars", 6000)), 20000))
            except Exception:
                max_chars = 6000
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            try:
                with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                    resp = client.get(url, headers=headers)
                    if resp.status_code != 200:
                        return f"Fetch failed with HTTP status {resp.status_code} for {url}"
                    ctype = resp.headers.get("content-type", "")
                    if "html" in ctype or "xml" in ctype or not ctype:
                        html = resp.text
                        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
                        text = re.sub(r"<[^>]+>", " ", html)
                        text = unescape(text)
                        text = re.sub(r"[ \t]+", " ", text)
                        text = re.sub(r"\n\s*\n+", "\n\n", text)
                    else:
                        text = resp.text
                    text = text.strip()
                    if not text:
                        return f"Page fetched but no readable text returned ({ctype})."
                    if len(text) > max_chars:
                        text = text[:max_chars] + f"\n... (truncated at {max_chars} chars)"
                    return f"Content of {url}:\n\n{text}"
            except Exception as fetch_err:
                return f"Web fetch error: {fetch_err}"

        elif tool_name == "get_weather":
            location = (arguments.get("location") or "").strip()
            if not location:
                return "Error: 'location' is required for get_weather."
            try:
                with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                    geo = client.get(
                        "https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": location, "count": 1},
                    )
                    results = geo.json().get("results") or []
                    if not results:
                        return f"Could not find location '{location}'."
                    place = results[0]
                    lat, lon = place["latitude"], place["longitude"]
                    label = ", ".join(str(place.get(k)) for k in ("name", "country") if place.get(k))
                    wx = client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        params={"latitude": lat, "longitude": lon, "current_weather": True},
                    )
                    cur = (wx.json().get("current_weather") or {})
                    if not cur:
                        return f"Weather data unavailable for '{label}'."
                    wmo = cur.get("weathercode")
                    codes = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "heavy showers", 95: "thunderstorm"}
                    desc = codes.get(wmo, f"weather code {wmo}") if wmo is not None else "unknown"
                    return (
                        f"Current weather in {label}:\n"
                        f"  temperature: {cur.get('temperature')} °C\n"
                        f"  condition: {desc}\n"
                        f"  wind speed: {cur.get('windspeed')} km/h"
                    )
            except Exception as wx_err:
                return f"Weather lookup error: {wx_err}"

        elif tool_name == "calculate":
            expression = (arguments.get("expression") or "").strip()
            if not expression:
                return "Error: 'expression' is required for calculate."
            cleaned = expression.replace("^", "**")
            if not re.fullmatch(r"[0-9eE+\-*/%(). ]*", cleaned):
                return "Error: only numbers and + - * / % ** ( ) are allowed."
            try:
                value = eval(cleaned, {"__builtins__": {}}, {})  # noqa: S307 - input strictly validated above
                return f"{expression} = {value}"
            except ZeroDivisionError:
                return "Error: division by zero."
            except Exception as calc_err:
                return f"Error evaluating expression: {calc_err}"

        elif tool_name == "convert_units":
            try:
                value = float(arguments.get("value"))
            except Exception:
                return "Error: 'value' must be a number."
            from_unit = str(arguments.get("from_unit", "")).strip().lower()
            to_unit = str(arguments.get("to_unit", "")).strip().lower()
            factors = {
                "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344},
                "mass": {"mg": 0.000001, "g": 0.001, "kg": 1.0, "t": 1000.0, "oz": 0.0283495, "lb": 0.453592},
                "data": {"b": 1.0, "kb": 1024.0, "mb": 1024.0 ** 2, "gb": 1024.0 ** 3, "tb": 1024.0 ** 4},
            }
            temps = ("c", "f", "k")
            result_val = None
            if from_unit in temps and to_unit in temps:
                celsius = value
                if from_unit == "f":
                    celsius = (value - 32) * 5 / 9
                elif from_unit == "k":
                    celsius = value - 273.15
                if to_unit == "c":
                    result_val = celsius
                elif to_unit == "f":
                    result_val = celsius * 9 / 5 + 32
                else:
                    result_val = celsius + 273.15
            else:
                for table in factors.values():
                    if from_unit in table and to_unit in table:
                        result_val = value * table[from_unit] / table[to_unit]
                        break
            if result_val is None:
                supported = "length (mm/cm/m/km/in/ft/yd/mi), mass (mg/g/kg/t/oz/lb), temperature (c/f/k), data (b/kb/mb/gb/tb)"
                return f"Error: unsupported conversion '{from_unit}' -> '{to_unit}'. Supported: {supported}"
            return f"{value} {from_unit} = {round(result_val, 6)} {to_unit}"

        elif tool_name == "timezone_convert":
            from datetime import datetime
            try:
                from zoneinfo import ZoneInfo
            except Exception:
                return "Error: zoneinfo is not available on this Python build."
            to_tz = (arguments.get("to_tz") or "").strip()
            from_tz = (arguments.get("from_tz") or "UTC").strip() or "UTC"
            time_str = (arguments.get("time") or "").strip()
            try:
                src_zone = ZoneInfo(from_tz)
                dst_zone = ZoneInfo(to_tz)
            except Exception as tz_err:
                return f"Error: unknown timezone ({tz_err}). Use IANA names like 'Europe/Berlin'."
            if time_str:
                try:
                    hh, mm = time_str.split(":")
                    naive = datetime.now(src_zone).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                except Exception:
                    return "Error: 'time' must be in HH:MM (24h) format."
            else:
                naive = datetime.now(src_zone)
            converted = naive.astimezone(dst_zone)
            return f"{naive.strftime('%H:%M')} {from_tz} = {converted.strftime('%H:%M')} {to_tz} ({converted.strftime('%Y-%m-%d')})"

        elif tool_name == "generate_uuid":
            import uuid as uuid_mod
            try:
                count = max(1, min(int(arguments.get("count", 1)), 20))
            except Exception:
                count = 1
            ids = [str(uuid_mod.uuid4()) for _ in range(count)]
            return "\n".join(ids)

        elif tool_name == "hash_text":
            import hashlib
            import base64 as b64
            text = arguments.get("text", "")
            algorithm = (arguments.get("algorithm") or "sha256").strip().lower()
            data = text.encode("utf-8")
            if algorithm in ("md5", "sha1", "sha256", "sha512"):
                return f"{algorithm}: {hashlib.new(algorithm, data).hexdigest()}"
            if algorithm == "base64":
                return f"base64: {b64.b64encode(data).decode('ascii')}"
            if algorithm == "base64-decode":
                try:
                    return f"base64-decode: {b64.b64decode(text).decode('utf-8', errors='replace')}"
                except Exception as b64_err:
                    return f"Error: invalid base64 input ({b64_err})"
            if algorithm == "hex":
                return f"hex: {data.hex()}"
            if algorithm == "url-encode":
                return f"url-encode: {urllib.parse.quote(text, safe='')}"
            return "Error: algorithm must be one of md5, sha1, sha256, sha512, base64, base64-decode, hex, url-encode."

        else:
            # External MCP server tool ('server__tool')
            from phantombox.mcp import is_mcp_tool, call_mcp_tool
            if is_mcp_tool(tool_name):
                return call_mcp_tool(tool_name, arguments)
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        return f"Tool execution error: {e}"
