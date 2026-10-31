"""Local file and directory tools for phantombox runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from phantombox.config import load_config

# Schema definitions for llama.cpp OpenAI-compatible tool call endpoint
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory (files and subdirectories).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list (e.g. '.' for current directory, or a folder path)",
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
            "description": "Read the contents of a text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read",
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
            "name": "write_file",
            "description": "Write or overwrite a file with given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to write",
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
            "description": "Perform in-place text replacement in an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to edit",
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
]


def _is_path_allowed(target_path: Path, allowed_patterns: List[str]) -> bool:
    """Check if target_path is allowed under allowed_patterns (e.g. ['*'] or specific dirs/files)."""
    if "*" in allowed_patterns:
        return True

    try:
        resolved_target = target_path.resolve()
    except Exception:
        resolved_target = target_path

    for pattern in allowed_patterns:
        pat = pattern.strip()
        if not pat:
            continue
        try:
            pat_path = Path(pat).resolve()
            # If pattern is a directory, check if target is inside it
            if resolved_target == pat_path:
                return True
            try:
                resolved_target.relative_to(pat_path)
                return True
            except ValueError:
                pass
        except Exception:
            pass

    return False


def check_tool_permission(tool_name: str, arguments: Dict[str, Any]) -> Tuple[bool, str, str]:
    """
    Validate tool call against configured scope and determine confirmation requirement.
    Returns: (is_allowed_by_scope, reason_or_error, requirement)
    requirement: 'allowed_auto' | 'needs_confirmation' | 'denied'
    """
    cfg = load_config()
    mode = cfg.get("permission_mode", "restricted")
    allowed_reads = cfg.get("allowed_read_paths", ["*"])
    allowed_writes = cfg.get("allowed_write_paths", ["*"])
    auto_tools = cfg.get("custom_auto_approve_tools", ["list_dir", "read_file"])

    target_path_str = arguments.get("path", ".")
    target_path = Path(target_path_str)

    # 1. Path scope check
    if tool_name in ("list_dir", "read_file"):
        if not _is_path_allowed(target_path, allowed_reads):
            return False, f"Path '{target_path_str}' is outside allowed read paths: {allowed_reads}", "denied"
    elif tool_name in ("write_file", "edit_file"):
        if not _is_path_allowed(target_path, allowed_writes):
            return False, f"Path '{target_path_str}' is outside allowed write paths: {allowed_writes}", "denied"

    # 2. Permission level check
    if mode == "full_access":
        return True, "Full access mode: auto-approved", "allowed_auto"

    if mode == "restricted":
        return True, "Restricted mode: requires confirmation", "needs_confirmation"

    if mode == "custom":
        if tool_name in auto_tools:
            return True, f"Custom mode: '{tool_name}' is auto-approved", "allowed_auto"
        return True, f"Custom mode: '{tool_name}' requires confirmation", "needs_confirmation"

    # Default fallback
    return True, "Requires confirmation", "needs_confirmation"


def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute the validated tool and return output string."""
    try:
        path_str = arguments.get("path", ".")
        target_path = Path(path_str).resolve()

        if tool_name == "list_dir":
            if not target_path.exists():
                return f"Error: Directory '{path_str}' does not exist."
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

            return f"Directory listing of {target_path}:\n" + ("\n".join(items) if items else "(empty directory)")

        elif tool_name == "read_file":
            if not target_path.exists():
                return f"Error: File '{path_str}' does not exist."
            if not target_path.is_file():
                return f"Error: Path '{path_str}' is not a regular file."

            limit = arguments.get("limit_lines")
            content = target_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            if limit and limit > 0 and len(lines) > limit:
                truncated = "\n".join(lines[:limit])
                return f"{truncated}\n... ({len(lines) - limit} more lines truncated)"
            return content

        elif tool_name == "write_file":
            content = arguments.get("content", "")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} characters to '{target_path}'."

        elif tool_name == "edit_file":
            if not target_path.exists():
                return f"Error: File '{path_str}' does not exist."
            old_text = arguments.get("old_text", "")
            new_text = arguments.get("new_text", "")
            if not old_text:
                return "Error: 'old_text' cannot be empty."

            content = target_path.read_text(encoding="utf-8")
            if old_text not in content:
                return f"Error: 'old_text' was not found in '{target_path}'."

            updated = content.replace(old_text, new_text, 1)
            target_path.write_text(updated, encoding="utf-8")
            return f"Successfully replaced text in '{target_path}'."

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        return f"Tool execution error: {e}"
