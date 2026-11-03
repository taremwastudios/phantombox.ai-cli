"""Per-model capability metadata (does this model support function/tool calling?).

Tool support is *not* uniform across providers, and it is what decides whether an
agentic turn can actually work:

* The ``tools`` array is silently ignored by models without function-calling
  support. The model then answers in prose while the chat loop waits for a tool
  call that never comes — which reads as "the assistant refuses to use tools".
* For routing, sending 30+ tool schemas to a model that cannot use them burns
  tokens and latency for nothing.

The rules here are declarative: ``supports_tools(provider_id, model_id)`` answers
three-valued (True / False / None-unknown) and callers decide how to act on
"unknown".
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from phantombox.providers import get_provider

# ---------------------------------------------------------------------------
# Provider-level defaults: (default, deny_patterns, allow_patterns)
#   default = None  -> provider does not advertise tool support; assume no
#   default = True  -> most models on this endpoint support tools
# deny wins over allow; unknown model ids on a True provider stay True.
# ---------------------------------------------------------------------------
PROVIDER_TOOL_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "openrouter": {
        # OpenRouter fronts many vendors; most modern chat models support tools.
        "default": True,
        "deny": (),
    },
    "groq": {
        "default": True,
        # Groq's whisper/tts additions plus older non-tool llama-2 models.
        # Without the audio entries, whisper-large-v3 was reported as tool-capable
        # and received file/MCP schemas it can never use.
        "deny": (r"llama-?2", r"llama2", r"whisper", r"-tts\b", r"distil-whisper", r"playai"),
    },
    "anthropic": {
        "default": True,
        # Claude 2.x predates tool use
        "deny": (r"claude-2", r"claude-instant"),
    },
    "openai": {
        "default": True,
        "deny": (
            r"^o1-(mini-)?preview$",   # o1-preview historically rejected tools
            r"^gpt-3\.5-turbo-instruct$",
            r"^davinci",
            r"^babbage",
            # Non-chat endpoints: audio/image/embedding models cannot call tools.
            r"whisper",
            r"\btts",
            r"dall-e",
            r"^text-embedding",
            r"^omni-moderation",
        ),
    },
    "gemini": {
        "default": True,
        "deny": (r"gemini-1\.0-pro-vision", r"embedding", r"imagen", r"-tts"),
    },
    "mistral": {
        "default": True,
        "deny": (r"embed", r"moderation", r"ocr", r"voxtral"),
    },
    "deepseek": {"default": True, "deny": (r"coder-beta",)},
    "nvidia": {"default": True, "deny": (r"embed", r"rerank", r"clip")},
    "qwen": {"default": True, "deny": (r"embed", r"tts", r"asr", r"-audio", r"vl-ocr")},
    "grok": {"default": True, "deny": ()},
    "huggingface": {"default": None, "deny": ()},
    "phantomboxai": {"default": None, "deny": ()},
    "zglm": {"default": None, "deny": ()},
    "qianfan": {"default": None, "deny": ()},
    "aionlabs": {"default": None, "deny": ()},
    "baseten": {"default": None, "deny": ()},
    "azure": {"default": True, "deny": ()},
    "bedrock": {"default": True, "deny": ()},
    "atlascloud": {"default": None, "deny": ()},
    "assemblyai": {"default": False, "deny": ()},
    "akashml": {"default": None, "deny": ()},
}

# Local llama.cpp servers: whether tools work depends entirely on the template /
# chat format embedded in the GGUF. These names are known-bad for tool calling.
LOCAL_NO_TOOL_PATTERNS = (
    r"embed",
    # Tiny models rarely emit valid JSON tool calls
    r"^(?:qwen[\d.]*-)?(?:0\.5b|1\.5b)\b",
    r"0\.5b|1\.5b-instruct",
    r"phi-?2",
    r"tinyllama",
    r"smollm",
    r"stablelm",
)


def _matches_any(model_id: str, patterns) -> bool:
    mid = (model_id or "").lower()
    return any(re.search(p, mid) for p in (patterns or ()))


def supports_tools(provider_id: str, model_id: str) -> Optional[bool]:
    """Return True/False if known for this (provider, model), else None."""
    pid = (provider_id or "").strip().lower()
    mid = (model_id or "").strip()

    # Local model (no provider prefix): heuristic only.
    if not pid:
        if _matches_any(mid, LOCAL_NO_TOOL_PATTERNS):
            return False
        return None

    info = get_provider(pid) or {}
    api_type = info.get("api_type")

    # Router/subagents always forward tools to the selected route.
    if api_type == "router" or pid == "phantombox":
        return True

    rules = PROVIDER_TOOL_DEFAULTS.get(pid)
    if rules is None:
        return None

    if _matches_any(mid, rules.get("deny")):
        return False
    return rules.get("default")


def tool_capability_label(provider_id: str, model_id: str) -> str:
    """Human-readable capability label for /models listings."""
    cap = supports_tools(provider_id, model_id)
    if cap is True:
        return "tools: yes"
    if cap is False:
        return "tools: no"
    return "tools: unknown"


def tools_supported_or_unknown(provider_id: str, model_id: str) -> bool:
    """True unless the model is *known* not to support tool calling."""
    return supports_tools(provider_id, model_id) is not False
