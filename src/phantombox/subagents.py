"""Subagents Architecture for PhantomBox.

Allows users to configure dedicated AI subagents (local or cloud models)
with specialized nicknames, tasks, and scripts to assist the primary model:
- Subagent registration with custom nicknames and models
- Task automation scripts (e.g. next message suggestion, autonomous web search assistance, code reviewer)
- Model-to-subagent invocation via `call_subagent` tool
- Global and session auto-assignment of subagents
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from phantombox.config import load_config, set_config_val, save_config
from phantombox.providers import stream_chat_deltas

# Built-in automation script templates
AVAILABLE_SCRIPTS: Dict[str, Dict[str, Any]] = {
    "suggestions": {
        "id": "suggestions",
        "name": "Next Message Suggestions",
        "description": "Analyzes the conversation and generates 3 quick, smart follow-up suggestions for the user.",
        "system_prompt": (
            "You are a helpful conversation assistant. Analyze the conversation history and "
            "provide 3 concise, highly relevant next question or response suggestions that the user could ask. "
            "Format as clean bullet points."
        ),
    },
    "web_assistant": {
        "id": "web_assistant",
        "name": "Autonomous Internet Search Helper",
        "description": "Proactively identifies queries requiring live web facts, performs searches, and provides distilled background knowledge.",
        "system_prompt": (
            "You are a web search helper subagent. Your job is to check the user's message, "
            "extract key topics needing up-to-date facts, and provide a clear, factual summary of background context to help the main model answer."
        ),
    },
    "code_reviewer": {
        "id": "code_reviewer",
        "name": "Code Reviewer & Quality Auditor",
        "description": "Reviews code written or discussed for bugs, security vulnerabilities, and optimizations.",
        "system_prompt": (
            "You are a specialized code review subagent. Review any code snippets in the chat for bugs, edge cases, "
            "security vulnerabilities, and performance improvements. Be direct, concise, and constructive."
        ),
    },
    "terminal_operator": {
        "id": "terminal_operator",
        "name": "Background Terminal & File Operator",
        "description": "Specialized in running terminal processes, moving files, compiling, and testing scripts.",
        "system_prompt": (
            "You are a background terminal and file operations subagent. You assist the main model by executing terminal commands, "
            "organizing files, moving files, and verifying code executions inside the phantombox-dir workspace."
        ),
    },
}


def get_subagents_config() -> Dict[str, Any]:
    """Retrieve subagents configuration."""
    cfg = load_config()
    sub_cfg = cfg.get("subagents")
    if not isinstance(sub_cfg, dict):
        sub_cfg = {
            "agents": {},
            "global_auto": False,
            "global_auto_nickname": "",
        }
    return sub_cfg


def save_subagents_config(sub_cfg: Dict[str, Any]) -> None:
    """Save subagents configuration."""
    set_config_val("subagents", sub_cfg)


def register_subagent(
    nickname: str,
    model: str,
    role: str = "Assistant Helper",
    scripts: Optional[List[str]] = None,
    custom_system_prompt: str = "",
) -> Tuple[bool, str]:
    """Register or update a subagent with a unique nickname and designated model."""
    clean_nick = nickname.strip().lower()
    if not clean_nick:
        return False, "Nickname cannot be empty."

    clean_model = model.strip().lower()
    # Guard: prevent subagents-in-subagents recursion via phantombox/subagents.
    # /subagents is already tailored for provisioning subagents; phantombox/router
    # is the entry point tailored to search available model responses.
    if "subagents" in clean_model.split("/")[-1] or clean_model in ("subagents", "phantombox/subagents", "phantombox:subagents"):
        return (
            False,
            "A subagent cannot use 'phantombox/subagents' as its model (subagents cannot spawn subagents). "
            "Use a concrete model instead, or switch your main model to 'phantombox/router' to auto-discover model responses.",
        )

    sub_cfg = get_subagents_config()
    agents = sub_cfg.setdefault("agents", {})

    agent_scripts = [s for s in (scripts or []) if s in AVAILABLE_SCRIPTS]

    agents[clean_nick] = {
        "nickname": clean_nick,
        "model": model.strip(),
        "role": role.strip(),
        "scripts": agent_scripts,
        "custom_system_prompt": custom_system_prompt.strip(),
    }
    save_subagents_config(sub_cfg)
    return True, f"Subagent '{clean_nick}' registered using model '{model}'."


def remove_subagent(nickname: str) -> Tuple[bool, str]:
    """Delete a subagent by nickname."""
    clean_nick = nickname.strip().lower()
    sub_cfg = get_subagents_config()
    agents = sub_cfg.get("agents", {})
    if clean_nick in agents:
        del agents[clean_nick]
        # Clean up auto reference if needed
        if sub_cfg.get("global_auto_nickname") == clean_nick:
            sub_cfg["global_auto"] = False
            sub_cfg["global_auto_nickname"] = ""
        save_subagents_config(sub_cfg)
        return True, f"Subagent '{clean_nick}' removed."
    return False, f"Subagent '{clean_nick}' not found."


def list_subagents() -> Dict[str, Dict[str, Any]]:
    """List all registered subagents."""
    sub_cfg = get_subagents_config()
    return sub_cfg.get("agents", {})


def get_subagent(nickname: str) -> Optional[Dict[str, Any]]:
    """Get a registered subagent by nickname."""
    clean_nick = nickname.strip().lower()
    agents = list_subagents()
    return agents.get(clean_nick)


def set_global_auto(enabled: bool, nickname: str = "") -> Tuple[bool, str]:
    """Set global auto subagent assignment across chat sessions."""
    sub_cfg = get_subagents_config()
    if enabled:
        clean_nick = nickname.strip().lower()
        if clean_nick:
            agents = sub_cfg.get("agents", {})
            if clean_nick not in agents:
                return False, f"Subagent '{clean_nick}' does not exist. Set it up first using /subagents setup."
            sub_cfg["global_auto_nickname"] = clean_nick
        elif not sub_cfg.get("global_auto_nickname"):
            # Pick first available if none specified
            agents = sub_cfg.get("agents", {})
            if not agents:
                return False, "No subagents registered yet. Use /subagents setup first."
            sub_cfg["global_auto_nickname"] = next(iter(agents.keys()))
        sub_cfg["global_auto"] = True
        save_subagents_config(sub_cfg)
        return True, f"Global auto-subagent enabled: '{sub_cfg['global_auto_nickname']}'."
    else:
        sub_cfg["global_auto"] = False
        save_subagents_config(sub_cfg)
        return True, "Global auto-subagent disabled."


def get_global_auto() -> Tuple[bool, str]:
    """Check if global auto subagent is enabled and get its nickname."""
    sub_cfg = get_subagents_config()
    enabled = sub_cfg.get("global_auto", False)
    nick = sub_cfg.get("global_auto_nickname", "")
    return enabled, nick


def clean_message_for_subagent(content: Optional[str]) -> str:
    """Clean a message's content before passing it to subagents or automated helpers.

    Strips router attribution imprints, fallback provider notices, and router alerts
    so that subagents only see what the user asked and clean model responses.
    """
    if not content:
        return ""
    text = str(content)

    # 1. Strip router imprint footer (e.g. --- \n *Response returned by: ... via phantombox ...*)
    imprint_markers = [
        "\n\n---\n*Response returned by: ",
        "\n---\n*Response returned by: ",
        "\n*Response returned by: ",
        "--- \n*Response returned by: ",
        "---\n*Response returned by: ",
    ]
    for marker in imprint_markers:
        if marker in text:
            text = text.split(marker, 1)[0]

    # 2. Strip any standalone trailing lines matching Response returned by
    lines = text.splitlines()
    clean_lines = []
    for line in lines:
        sline = line.strip()
        low = sline.lower()
        if low.startswith("*response returned by:") or low.startswith("response returned by:") or low.startswith("response returned by;"):
            continue
        if low.startswith("> *notice: all cloud model resources exhausted") or low.startswith("**phantombox router alert:**"):
            continue
        clean_lines.append(line)
    text = "\n".join(clean_lines).rstrip()

    # 3. Clean trailing divider if left over from stripping imprint
    if text.endswith("\n---") or text.endswith("\n\n---"):
        text = text[:text.rfind("---")].rstrip()

    return text


def sanitize_suggestion_line(line: str) -> Optional[str]:
    """Validate and clean an individual suggestion candidate line.

    Rejects any option that mimics or contains provider attribution metadata,
    error messages, or router status alerts.
    """
    if not line:
        return None
    cleaned = line.strip().lstrip("•-*0123456789. \t").strip()
    if not cleaned or len(cleaned) < 2:
        return None

    low = cleaned.lower()
    if low.startswith("here are") or low.startswith("suggestions:"):
        return None
    if "response returned by" in low or "returned by:" in low or "returned by;" in low:
        return None
    if "via phantombox" in low or "via **phantombox" in low:
        return None
    if "notice: all cloud model" in low or "router alert" in low:
        return None
    return cleaned


async def run_subagent_task(
    nickname: str,
    task_prompt: str,
    context_messages: Optional[List[Dict[str, Any]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    local_port: int = 8080,
) -> str:
    """Execute a prompt through a designated subagent's model."""
    agent = get_subagent(nickname)
    if not agent:
        return f"Error: Subagent '{nickname}' not found."

    model_name = agent.get("model", "")
    if not model_name:
        return f"Error: No model configured for subagent '{nickname}'."

    sys_prompt = agent.get("custom_system_prompt") or f"You are subagent '{nickname}', a specialized assistant ({agent.get('role', 'Helper')})."
    
    msgs = [{"role": "system", "content": sys_prompt}]
    if context_messages:
        # Include recent turns of conversation context with clean user/assistant messages
        for m in context_messages[-6:]:
            if m.get("role") in ("user", "assistant"):
                cleaned_content = clean_message_for_subagent(m.get("content", ""))
                if cleaned_content:
                    msgs.append({"role": m["role"], "content": cleaned_content})

    msgs.append({"role": "user", "content": task_prompt})

    from phantombox.runtime import list_running
    running = list_running()
    port = local_port
    if model_name in running:
        port = running[model_name].get("port", local_port)

    # Use tools if provided
    agent_tools = tools or []

    result_text = ""
    try:
        async for delta in stream_chat_deltas(
            model_identifier=model_name,
            local_port=port,
            messages=msgs,
            tools=agent_tools,
        ):
            content = delta.get("content")
            if content:
                result_text += content
    except Exception as e:
        return f"Subagent '{nickname}' execution error: {e}"

    return result_text.strip()
