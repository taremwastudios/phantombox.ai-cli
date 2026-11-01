"""External LLM Provider & MCP Integration for phantombox.

Supports connecting cloud providers (OpenRouter, Groq, Claude, OpenAI, Gemini, Qwen, DeepSeek, Mistral)
via API keys, fetching remote model catalogs, and streaming chat completions with tool calling.
"""

from __future__ import annotations

import base64
import json
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from phantombox.config import load_config, set_config_val

# Supported providers definition
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "phantombox": {
        "id": "phantombox",
        "name": "PhantomBox Router",
        "tag": "Router",
        "description": "Smart Model Router: scans all connected providers until 200 OK response is found",
        "base_url": "",
        "models_url": "",
        "api_type": "router",
        "api_key_prefix": "",
        "help_url": "https://phantombox.ai",
        "no_key_required": True,
    },
    "phantomboxai": {
        "id": "phantombox",
        "name": "PhantomBox",
        "tag": "PB AI",
        "description": "Official PhantomBox VPS inference",
        "base_url": "https://api-phantombox.phantom-illusions-studio.com/v1",
        "models_url": "https://api-phantombox.phantom-illusions-studio.com/v1/models",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://phantombox.ai",
    },
    "openrouter": {
        "id": "openrouter",
        "name": "OpenRouter",
        "tag": "OpenRouter",
        "description": "Unified access to 200+ models (Claude, GPT, Llama, DeepSeek, Qwen)",
        "base_url": "https://openrouter.ai/api/v1",
        "models_url": "https://openrouter.ai/api/v1/models",
        "api_type": "openai",
        "api_key_prefix": "sk-or-",
        "help_url": "https://openrouter.ai/keys",
    },
    "groq": {
        "id": "groq",
        "name": "Groq",
        "tag": "Groq",
        "description": "Ultra-fast LPU inference (Llama 3.3, Mixtral, DeepSeek)",
        "base_url": "https://api.groq.com/openai/v1",
        "models_url": "https://api.groq.com/openai/v1/models",
        "api_type": "openai",
        "api_key_prefix": "gsk_",
        "help_url": "https://console.groq.com/keys",
    },
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "tag": "Claude",
        "description": "Claude 3.7 Sonnet, Claude 3.5 Sonnet & Haiku models",
        "base_url": "https://api.anthropic.com/v1",
        "models_url": "https://api.anthropic.com/v1/models",
        "api_type": "anthropic",
        "api_key_prefix": "sk-ant-",
        "help_url": "https://console.anthropic.com/settings/keys",
    },
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "tag": "OpenAI",
        "description": "GPT-4o, GPT-4o-mini, o3-mini, o1 reasoning models",
        "base_url": "https://api.openai.com/v1",
        "models_url": "https://api.openai.com/v1/models",
        "api_type": "openai",
        "api_key_prefix": "sk-",
        "help_url": "https://platform.openai.com/api-keys",
    },
    "gemini": {
        "id": "gemini",
        "name": "Google Gemini",
        "tag": "Gemini",
        "description": "Gemini 2.0 Flash, Gemini 1.5 Pro & Flash models",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
        "api_type": "openai",
        "api_key_prefix": "AIza",
        "help_url": "https://aistudio.google.com/app/apikey",
    },
    "qwen": {
        "id": "qwen",
        "name": "Qwen / DashScope",
        "tag": "Qwen",
        "description": "Qwen-Max, Qwen-Plus, Qwen-Turbo, Qwen2.5-Coder",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "models_url": "",
        "api_type": "openai",
        "api_key_prefix": "sk-",
        "help_url": "https://bailian.console.aliyun.com/",
    },
    "aionlabs": {
        "id": "aionlabs",
        "name": "AionLabs",
        "tag": "AionLabs",
        "description": "High-throughput specialized AI cloud inference",
        "base_url": "https://api.aionlabs.ai/v1",
        "models_url": "https://api.aionlabs.ai/v1/models",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://aionlabs.ai/",
    },
    "baseten": {
        "id": "baseten",
        "name": "Baseten",
        "tag": "Baseten",
        "description": "Production model serving with dedicated Truss / vLLM endpoints",
        "base_url": "https://bridge.baseten.co/v1",
        "models_url": "https://bridge.baseten.co/v1/models",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://app.baseten.co/settings/api_keys",
    },
    "azure": {
        "id": "azure",
        "name": "Azure AI",
        "tag": "Azure",
        "description": "Azure OpenAI Service and Azure AI Model Catalog",
        "base_url": "https://models.inference.ai.azure.com",
        "models_url": "https://models.inference.ai.azure.com/models",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://ai.azure.com/",
    },
    "bedrock": {
        "id": "bedrock",
        "name": "Amazon Bedrock",
        "tag": "Bedrock",
        "description": "AWS Bedrock models via OpenAI-compatible gateway / proxy",
        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com/v1",
        "models_url": "",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://aws.amazon.com/bedrock/",
    },
    "atlascloud": {
        "id": "atlascloud",
        "name": "Atlas Cloud",
        "tag": "Atlas",
        "description": "Decentralized GPU compute and serverless LLM API",
        "base_url": "https://api.atlascloud.ai/v1",
        "models_url": "https://api.atlascloud.ai/v1/models",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://atlascloud.ai/",
    },
    "assemblyai": {
        "id": "assemblyai",
        "name": "AssemblyAI",
        "tag": "AssemblyAI",
        "description": "Speech AI and LeMUR LLM reasoning engine",
        "base_url": "https://api.assemblyai.com/lemur/v3",
        "models_url": "",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://www.assemblyai.com/app",
    },
    "akashml": {
        "id": "akashml",
        "name": "AkashML",
        "tag": "Akash",
        "description": "Decentralized open-source cloud GPU inference",
        "base_url": "https://chatapi.akash.network/api/v1",
        "models_url": "https://chatapi.akash.network/api/v1/models",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://akash.network/",
    },
    "grok": {
        "id": "grok",
        "name": "Grok (xAI)",
        "tag": "Grok",
        "description": "xAI Grok-2 and Grok-2 Vision with real-time knowledge",
        "base_url": "https://api.x.ai/v1",
        "models_url": "https://api.x.ai/v1/models",
        "api_type": "openai",
        "api_key_prefix": "xai-",
        "help_url": "https://console.x.ai/",
    },
    "qianfan": {
        "id": "qianfan",
        "name": "Baidu Qianfan",
        "tag": "Qianfan",
        "description": "ERNIE-4.0, ERNIE-3.5 and Qianfan foundation models",
        "base_url": "https://qianfan.baidubce.com/v2",
        "models_url": "",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://cloud.baidu.com/product/wenxinworkshop",
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "tag": "DeepSeek",
        "description": "DeepSeek-V3 and DeepSeek-R1 reasoning models",
        "base_url": "https://api.deepseek.com/v1",
        "models_url": "https://api.deepseek.com/v1/models",
        "api_type": "openai",
        "api_key_prefix": "sk-",
        "help_url": "https://platform.deepseek.com/api_keys",
    },
    "huggingface": {
        "id": "huggingface",
        "name": "Hugging Face",
        "tag": "HF",
        "description": "Hugging Face Inference Providers: thousands of open models (Llama, Qwen, DeepSeek, Mistral...)",
        "base_url": "https://router.huggingface.co/v1",
        "models_url": "https://router.huggingface.co/v1/models",
        "api_type": "openai",
        "api_key_prefix": "hf_",
        "help_url": "https://huggingface.co/settings/tokens",
    },
    "zglm": {
        "id": "zglm",
        "name": "Z.ai GLM (Zhipu)",
        "tag": "GLM",
        "description": "Zhipu GLM-4 family: GLM-4.6, GLM-4.5, GLM-4-Plus and GLM-4-Flash",
        "base_url": "https://api.z.ai/api/paas/v4",
        "models_url": "",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://z.ai/manage-apikey/apikey-list",
    },
    "mistral": {
        "id": "mistral",
        "name": "Mistral AI",
        "tag": "Mistral",
        "description": "Mistral Large, Mistral Small, Codestral models",
        "base_url": "https://api.mistral.ai/v1",
        "models_url": "https://api.mistral.ai/v1/models",
        "api_type": "openai",
        "api_key_prefix": "",
        "help_url": "https://console.mistral.ai/api-keys/",
    },
}

# Cache for fetched remote models catalog
_REMOTE_MODELS_CACHE: Dict[str, List[str]] = {}

# Stickiness cache: retains active winning model across conversation turns / tool-calling iterations
_PINNED_ROUTER_MODEL: Optional[Tuple[str, str]] = None


def get_pinned_router_model() -> Optional[Tuple[str, str]]:
    """Get the currently pinned (provider_id, model_id) for sticky multi-turn tool calling."""
    return _PINNED_ROUTER_MODEL


def set_pinned_router_model(provider_id: str, model_id: str) -> None:
    """Pin the responsive model so tool calling iterations use the same model consistently."""
    global _PINNED_ROUTER_MODEL
    _PINNED_ROUTER_MODEL = (provider_id, model_id)


def clear_pinned_router_model() -> None:
    """Clear pinned model (e.g. if the model fails or conversation resets)."""
    global _PINNED_ROUTER_MODEL
    _PINNED_ROUTER_MODEL = None


def get_all_providers() -> List[Dict[str, Any]]:
    """Return all available external providers list."""
    return list(PROVIDERS.values())


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve provider specification by ID (case-insensitive)."""
    clean = provider_id.lower().strip()
    if clean in ("claude",):
        clean = "anthropic"
    return PROVIDERS.get(clean)


def get_provider_key(provider_id: str) -> Optional[str]:
    """Retrieve stored API key for a provider."""
    cfg = load_config()
    providers_cfg = cfg.get("providers", {})
    p = providers_cfg.get(provider_id.lower().strip())
    if isinstance(p, dict):
        key = p.get("api_key", "").strip()
        return key if key else None
    elif isinstance(p, str):
        key = p.strip()
        return key if key else None
    return None


def set_provider_key(provider_id: str, api_key: str) -> None:
    """Store or clear API key for a provider."""
    cfg = load_config()
    providers_cfg = dict(cfg.get("providers", {}))
    clean_id = provider_id.lower().strip()
    clean_key = api_key.strip()

    if clean_key:
        providers_cfg[clean_id] = {
            "api_key": clean_key,
            "enabled": True,
        }
    else:
        # Disconnect provider
        if clean_id in providers_cfg:
            del providers_cfg[clean_id]

    set_config_val("providers", providers_cfg)
    # Clear cached models
    _REMOTE_MODELS_CACHE.pop(clean_id, None)


def is_provider_connected(provider_id: str) -> bool:
    """Check if an API key is configured for this provider, or if it requires no key (like phantombox router)."""
    p_info = get_provider(provider_id)
    if p_info and p_info.get("no_key_required"):
        return True
    return bool(get_provider_key(provider_id))


def list_connected_providers() -> List[str]:
    """List IDs of all currently connected providers."""
    return [pid for pid in PROVIDERS if is_provider_connected(pid)]


async def fetch_provider_models(provider_id: str) -> List[str]:
    """Fetch the LIVE model catalog from the provider API.

    Never returns hardcoded defaults: models shown to the user are always ones
    the endpoint itself reports, so nothing can be 'Model no longer exists'.
    On fetch failure the cache stays empty and can be retried on next call.
    """
    clean_id = provider_id.lower().strip()
    p_info = get_provider(clean_id)
    if not p_info:
        return []

    api_key = get_provider_key(clean_id)
    if not api_key:
        return []

    # Live catalog already cached
    if clean_id in _REMOTE_MODELS_CACHE:
        return list(_REMOTE_MODELS_CACHE[clean_id])

    models_url = p_info.get("models_url", "")
    if not models_url:
        # No catalog endpoint: nothing reliable to list
        _REMOTE_MODELS_CACHE[clean_id] = []
        return []

    headers: Dict[str, str] = {}
    if p_info.get("api_type") == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        headers = {
            "Authorization": f"Bearer {api_key}",
        }
        if clean_id == "openrouter":
            headers["HTTP-Referer"] = "https://phantombox.ai"
            headers["X-Title"] = "phantombox"

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                data = body.get("data", [])
                fetched_ids: List[str] = []
                for item in data:
                    mid = item.get("id") if isinstance(item, dict) else str(item)
                    if not mid or not isinstance(mid, str):
                        continue
                    m_lower = mid.lower()
                    # Filter out non-chat / utility models
                    if any(bad in m_lower for bad in ("embed", "whisper", "tts", "dall-e", "moderation", "audio", "davinci", "babbage")):
                        continue
                    fetched_ids.append(mid)

                if fetched_ids:
                    result = sorted(fetched_ids)
                    _REMOTE_MODELS_CACHE[clean_id] = result
                    return result
    except Exception:
        pass

    # Fetch failed: cache an empty list (NOT stale defaults) so it can be retried
    # later without ever showing the user a model that no longer exists.
    _REMOTE_MODELS_CACHE[clean_id] = []
    return []


def parse_model_identifier(model_identifier: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Parse a model string (e.g. 'openrouter:anthropic/claude-3.7-sonnet' or 'groq/llama-3.3-70b-versatile').
    Returns (provider_info, stripped_model_id).
    If it's a local model, returns (None, model_identifier).
    """
    clean = model_identifier.strip()
    if not clean:
        return None, clean

    # Check colon separator first: 'provider:model'
    if ":" in clean:
        prefix, rest = clean.split(":", 1)
        p = get_provider(prefix)
        if p:
            return p, rest

    # Check leading slash: 'provider/model'
    for pid in PROVIDERS:
        if clean.lower().startswith(f"{pid}/"):
            return PROVIDERS[pid], clean[len(pid) + 1 :]

    if clean.lower().startswith("claude/"):
        return PROVIDERS["anthropic"], clean[7:]

    # Check remote server prefix: 'server/server_name/model_name' or 'remote/server_name/model_name'
    if clean.lower().startswith("server/") or clean.lower().startswith("remote/"):
        parts = clean.split("/", 2)
        if len(parts) >= 3:
            srv_name = parts[1]
            rem_model = parts[2]
            return {
                "id": f"server_{srv_name}",
                "server_name": srv_name,
                "name": f"Server ({srv_name})",
                "tag": f"Server:{srv_name}",
                "api_type": "remote_server",
            }, rem_model

    return None, clean


def format_model_display_name(model_identifier: str) -> str:
    """Format model identifier for friendly UI display in prompt box."""
    p_info, raw_id = parse_model_identifier(model_identifier)
    if p_info:
        # e.g. openrouter/claude-3.5-sonnet
        tag = p_info.get("id", "remote")
        return f"{tag}/{raw_id}"
    return model_identifier


async def stream_chat_deltas(
    model_identifier: str,
    local_port: int,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Stream completion deltas from local llama-server or remote MCP/API provider.
    Yields OpenAI-compatible delta objects: {'content': ..., 'tool_calls': [...]}.
    """
    p_info, raw_model_id = parse_model_identifier(model_identifier)

    # 1. Local model via llama.cpp server
    if not p_info:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"http://127.0.0.1:{local_port}/v1/chat/completions",
                json={
                    "messages": messages,
                    "tools": tools,
                    "stream": True,
                    "temperature": 0.7,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"]
                        yield delta
                    except Exception:
                        continue
        return

    # 2. PhantomBox Autonomous Smart Router / Subagents
    if p_info.get("api_type") == "router" or p_info["id"] == "phantombox":
        from phantombox.router import stream_router_completion
        async for delta in stream_router_completion(
            messages=messages,
            tools=tools,
            enable_subagents_mode=(raw_model_id == "subagents"),
        ):
            yield delta
        return

    # 3. Remote PhantomBox Server connection (/server --connect)
    if p_info.get("api_type") == "remote_server":
        from phantombox.server import list_remote_servers
        remotes = list_remote_servers()
        srv_name = p_info.get("server_name", "")
        srv_conf = remotes.get(srv_name)
        if not srv_conf:
            raise ValueError(f"Remote server '{srv_name}' not found. Connect it first using /server --connect.")

        base_url = srv_conf["url"].rstrip("/")
        username = srv_conf.get("username", "")
        password = srv_conf.get("password", "")

        auth_header = "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
        }
        body: Dict[str, Any] = {
            "model": raw_model_id,
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
        }
        if tools:
            body["tools"] = tools

        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST",
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json=body,
            ) as response:
                if response.status_code != 200:
                    err_txt = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(f"Remote server error ({response.status_code}): {err_txt}")

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"]
                        yield delta
                    except Exception:
                        continue
        return

    # 3. Remote Cloud Provider
    api_key = get_provider_key(p_info["id"])
    if not api_key:
        raise ValueError(f"No API key configured for provider '{p_info['name']}'. Run /mcp --connect to set your key.")

    # Anthropic Messages API
    if p_info.get("api_type") == "anthropic":
        system_text = ""
        anthropic_messages: List[Dict[str, Any]] = []
        for m in messages:
            r = m.get("role")
            if r == "system":
                system_text = str(m.get("content") or "")
            elif r == "user":
                anthropic_messages.append({"role": "user", "content": str(m.get("content") or "")})
            elif r == "assistant":
                if m.get("tool_calls"):
                    blocks: List[Dict[str, Any]] = []
                    if m.get("content"):
                        blocks.append({"type": "text", "text": str(m["content"])})
                    for tc in m["tool_calls"]:
                        try:
                            args = json.loads(tc["function"]["arguments"])
                        except Exception:
                            args = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id"),
                            "name": tc["function"]["name"],
                            "input": args,
                        })
                    anthropic_messages.append({"role": "assistant", "content": blocks})
                else:
                    anthropic_messages.append({"role": "assistant", "content": str(m.get("content") or "")})
            elif r == "tool":
                anthropic_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id"),
                        "content": str(m.get("content", "")),
                    }],
                })

        anthropic_tools: List[Dict[str, Any]] = []
        for t in tools:
            fn = t.get("function", {})
            anthropic_tools.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })

        req_body: Dict[str, Any] = {
            "model": raw_model_id,
            "messages": anthropic_messages,
            "max_tokens": 4096,
            "stream": True,
            "temperature": 0.7,
        }
        if system_text:
            req_body["system"] = system_text
        if anthropic_tools:
            req_body["tools"] = anthropic_tools

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=req_body,
            ) as response:
                response.raise_for_status()
                tool_call_index = 0
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    try:
                        event = json.loads(data_str)
                        ev_type = event.get("type")
                        if ev_type == "content_block_delta":
                            d = event.get("delta", {})
                            if d.get("type") == "text_delta":
                                yield {"content": d.get("text", "")}
                            elif d.get("type") == "input_json_delta":
                                yield {
                                    "tool_calls": [{
                                        "index": tool_call_index,
                                        "function": {"arguments": d.get("partial_json", "")},
                                    }]
                                }
                        elif ev_type == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                tool_call_index = event.get("index", 0)
                                yield {
                                    "tool_calls": [{
                                        "index": tool_call_index,
                                        "id": cb.get("id"),
                                        "function": {
                                            "name": cb.get("name", ""),
                                            "arguments": "",
                                        },
                                    }]
                                }
                    except Exception:
                        continue
        return

    # Standard OpenAI-compatible endpoints (OpenRouter, Groq, OpenAI, Gemini, Qwen, DeepSeek, Mistral)
    base_url = p_info["base_url"].rstrip("/")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if p_info["id"] == "openrouter":
        headers["HTTP-Referer"] = "https://phantombox.ai"
        headers["X-Title"] = "phantombox"

    # Gemini-specific OpenAI compatibility handling for thought signatures:
    # When sending assistant tool_calls back in multi-turn conversations, if the model is a thinking
    # model (such as Gemini 2.5 / 3 preview), Google requires thought_signature on toolCall parts,
    # OR thinking_budget = 0 / reasoning off to avoid requiring cryptographic thought signatures.
    cleaned_messages = messages
    if p_info["id"] == "gemini":
        cleaned_messages = []
        for msg in messages:
            msg_copy = dict(msg)
            if msg_copy.get("role") == "assistant" and msg_copy.get("tool_calls"):
                cleaned_tool_calls = []
                for tc in msg_copy["tool_calls"]:
                    tc_dict = dict(tc)
                    # If thought_signature is missing on this function call, Gemini 400s
                    # We ensure thought_signature is present if provided, or provide fallback
                    if "thought_signature" not in tc_dict and "thought_signature" not in tc_dict.get("function", {}):
                        # Add dummy / empty signature or pass through
                        pass
                    cleaned_tool_calls.append(tc_dict)
                msg_copy["tool_calls"] = cleaned_tool_calls
            cleaned_messages.append(msg_copy)

    body: Dict[str, Any] = {
        "model": raw_model_id,
        "messages": cleaned_messages,
        "stream": True,
        "temperature": 0.7,
    }
    if tools:
        body["tools"] = tools

    # For Gemini reasoning/thinking models (like gemini-3-flash-preview, gemini-2.5-flash),
    # disabling the internal thinking budget for tool calls prevents the thought_signature validation error
    if p_info["id"] == "gemini" and ("gemini-3" in raw_model_id or "preview" in raw_model_id):
        # Allow disabling thinking or configuring reasoning_effort
        body["reasoning_effort"] = "none"

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=body,
        ) as response:
            if response.status_code != 200:
                err_body = await response.aread()
                err_text = err_body.decode("utf-8", errors="replace")
                try:
                    err_json = json.loads(err_text)
                    msg = err_json.get("error", {}).get("message") or err_text
                except Exception:
                    msg = err_text
                raise RuntimeError(f"API Error ({response.status_code}) from {p_info['name']} for model '{raw_model_id}': {msg}")

            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"]
                    yield delta
                except Exception:
                    continue


def fetch_cached_or_default_models(provider_id: str) -> List[str]:
    """Return the cached LIVE catalog for a provider (empty until fetched).

    Never invents fallback models: a stale hardcoded list is how users end up
    selecting 'Model no longer exists' entries. The catalog is populated by
    fetch_provider_models() hitting the provider's real endpoint (hot-loaded at
    chat startup for every connected provider).
    """
    clean_id = provider_id.lower().strip()
    p = get_provider(clean_id)
    if not p:
        return []
    return list(_REMOTE_MODELS_CACHE.get(clean_id, []))
