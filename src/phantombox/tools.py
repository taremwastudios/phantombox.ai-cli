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
            "description": "List contents of a directory (files and subdirectories) inside phantombox-dir. Call this tool whenever inspecting existing files or directories.",
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
            "description": "Read the contents of a text file inside phantombox-dir. Call this tool whenever inspecting or reading existing file contents.",
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
                "Create a brand new file inside phantombox-dir with initial content. "
                "CRITICAL: Whenever writing or saving code, scripts, or text to a file, you MUST call this function "
                "with the full code/content instead of just printing it in conversation."
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
            "description": "Create a new folder or directory inside phantombox-dir. Use this to create folder structures before creating files.",
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
                "Write or completely overwrite a file with given content inside phantombox-dir. "
                "CRITICAL: Whenever saving complete code or rewriting a file, you MUST execute this function with the complete content."
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
                "Perform in-place text replacement in an existing file inside phantombox-dir. "
                "Provide an exact matching old_text snippet and the new_text replacement."
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
                "Search the live web for fresh facts, recent events, breaking news, dates, real-time sports/politics/tech information, "
                "or technical documentation using DuckDuckGo search. "
                "CRITICAL: Be proactive like Grok/Perplexity. Whenever answering queries involving current events, specific dates/years, recent developments, "
                "people, releases, prices, weather, sports scores, or when your knowledge might be outdated or uncertain, "
                "ALWAYS call this tool autonomously BEFORE formulating your answer, even if the user did not explicitly ask you to search."
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
                "Execute a terminal/shell process inside the designated 'phantombox-dir' workspace. "
                "Use this tool when the user requires running terminal commands, executing build/test commands, "
                "running scripts, installing packages, or inspecting system info."
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
            "description": "Move or rename a file or directory inside phantombox-dir.",
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
            "description": "Delete a file or folder inside phantombox-dir.",
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
                "Delegate a sub-task, background operation, terminal execution, code review, "
                "or research query to a registered subagent by nickname."
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
]


def get_tool_definitions() -> List[Dict[str, Any]]:
    """Return tool schema definitions filtered by active configuration, updating call_subagent description with available nicknames."""
    cfg = load_config()
    allow_search = cfg.get("enable_internet_search", True)

    from phantombox.subagents import list_subagents
    active_agents = list_subagents()
    nick_names = list(active_agents.keys())

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
                        + (f"CURRENT VALID SUBAGENT NICKNAMES: {nick_names}." if nick_names else "NO SUBAGENTS ARE CURRENTLY REGISTERED. Do not call this tool until a subagent is set up.")
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "nickname": {
                                "type": "string",
                                "description": f"The EXACT nickname of the subagent to call. Must be one of: {nick_names}" if nick_names else "Nickname of subagent",
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

    return tools


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

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        return f"Tool execution error: {e}"
