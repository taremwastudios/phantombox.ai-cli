"""Context window and token usage utilities for phantombox."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Known default context windows for common models (in tokens)
MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    # Local defaults
    "qwen2.5-0.5b": 32_768,
    "qwen2.5-1.5b": 32_768,
    "llama3.2-1b": 131_072,
    "phi3.5-mini": 131_072,
    # Groq models
    "llama-3.3-70b-versatile": 131_072,
    "llama-3.1-8b-instant": 131_072,
    "deepseek-r1-distill-llama-70b": 131_072,
    "mixtral-8x7b-32768": 32_768,
    "gemma2-9b-it": 8_192,
    "qwen-2.5-32b": 131_072,
    "qwen-2.5-coder-32b": 131_072,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3-mini": 200_000,
    "gpt-4-turbo": 128_000,
    # Anthropic
    "claude-3-7-sonnet-20250219": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    # Google Gemini
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.0-flash-lite": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    # Qwen DashScope
    "qwen-max": 32_768,
    "qwen-plus": 131_072,
    "qwen-turbo": 131_072,
    "qwen2.5-72b-instruct": 131_072,
    "qwen2.5-coder-32b-instruct": 131_072,
    # DeepSeek
    "deepseek-chat": 65_536,
    "deepseek-reasoner": 65_536,
    # Grok
    "grok-2-1212": 131_072,
    "grok-2-vision-1212": 32_768,
    "grok-beta": 131_072,
    # Mistral
    "mistral-large-latest": 128_000,
    "mistral-small-latest": 128_000,
    "codestral-latest": 256_000,
}

DEFAULT_FALLBACK_CONTEXT = 32_768


def get_model_context_limit(model_identifier: str) -> int:
    """Return the context limit in tokens for the given model identifier."""
    if not model_identifier or model_identifier == "(no model selected)":
        return DEFAULT_FALLBACK_CONTEXT

    clean = model_identifier.lower().strip()
    # Strip provider prefix if present: 'provider:model' or 'provider/model'
    if ":" in clean:
        clean = clean.split(":", 1)[1]
    elif "/" in clean:
        parts = clean.split("/")
        clean = parts[-1]

    # Exact match
    if clean in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[clean]

    # Prefix/substring match
    for k, v in MODEL_CONTEXT_WINDOWS.items():
        if k in clean or clean in k:
            return v

    # Fallback heuristic based on common naming
    if "1m" in clean or "gemini" in clean:
        return 1_000_000
    if "128k" in clean:
        return 128_000
    if "64k" in clean:
        return 65_536
    if "32k" in clean or "qwen" in clean:
        return 32_768

    return DEFAULT_FALLBACK_CONTEXT


def estimate_tokens_from_messages(messages: List[Dict[str, Any]]) -> int:
    """
    Estimate total tokens consumed by conversation messages using character heuristic
    (~3.5 - 4 chars per token plus per-message framing overhead).
    """
    total_chars = 0
    message_count = len(messages)

    for m in messages:
        # Role name overhead
        total_chars += len(str(m.get("role", ""))) + 4
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(str(block.get("text", "")))
                    total_chars += len(str(block.get("content", "")))
        # Tool calls overhead
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                total_chars += len(str(fn.get("name", "")))
                total_chars += len(str(fn.get("arguments", "")))

    # ~3.8 characters per token on average for natural language & code
    token_est = int(total_chars / 3.8) + (message_count * 3)
    return max(0, token_est)


def format_token_count(tokens: int) -> str:
    """Format token count into readable string like 1k, 128k, 1M."""
    if tokens >= 1_000_000:
        val = round(tokens / 1_000_000, 1)
        return f"{int(val)}M" if val.is_integer() else f"{val:.1f}M"
    elif tokens >= 1_000:
        val = round(tokens / 1_000)
        return f"{int(val)}k"
    else:
        return f"{tokens}"


def get_context_marker_info(messages: List[Dict[str, Any]], model_identifier: str) -> Dict[str, Any]:
    """
    Compute context stats for prompt box.
    Returns:
      {
         'used_tokens': int,
         'limit_tokens': int,
         'percentage': float,
         'text': str, # e.g. '1.2k/128k (1%)'
         'color': str, # 'green' (<10%), 'yellow' (<=50%), 'orange' (<90%), 'red' (>=90%)
      }
    """
    limit = get_model_context_limit(model_identifier)
    used = estimate_tokens_from_messages(messages)
    pct = (used / limit) * 100.0 if limit > 0 else 0.0

    used_str = format_token_count(used)
    limit_str = format_token_count(limit)
    pct_int = int(round(pct))

    text = f"{used_str}/{limit_str} ({pct_int}%)"

    if pct < 10.0:
        color = "#10b981"  # green
    elif pct <= 50.0:
        color = "#eab308"  # yellow
    elif pct < 90.0:
        color = "#f97316"  # orange
    else:
        color = "#ef4444"  # red

    return {
        "used_tokens": used,
        "limit_tokens": limit,
        "percentage": pct,
        "text": text,
        "color": color,
    }
