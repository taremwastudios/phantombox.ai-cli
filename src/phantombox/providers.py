"""External LLM Provider & MCP Integration for phantombox.

Supports connecting cloud providers (OpenRouter, Groq, Claude, OpenAI, Gemini, Qwen, DeepSeek, Mistral)
via API keys, fetching remote model catalogs, and streaming chat completions with tool calling.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from phantombox.config import load_config, set_config_val
from phantombox.env import get_data_dir

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
        "id": "phantomboxai",
        "name": "PhantomBox",
        "tag": "PhantomBox",
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
    "nvidia": {
        "id": "nvidia",
        "name": "NVIDIA NIM",
        "tag": "NVIDIA",
        "description": "NVIDIA NIM / build.nvidia.com hosted open models (Llama, Nemotron, DeepSeek, Qwen)",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "api_type": "openai",
        "api_key_prefix": "nvapi-",
        "help_url": "https://build.nvidia.com/",
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

# Cache for fetched remote models catalog (in-memory, per process)
_REMOTE_MODELS_CACHE: Dict[str, List[str]] = {}

# Persistent catalog cache survives restarts so /models is instant on every launch.
# Entries are keyed by provider + a fingerprint of the API key, so rotating a key
# never serves a catalog fetched with the old one.
CATALOG_CACHE_FILE = get_data_dir() / "model_catalog_cache.json"
CATALOG_TTL_SECONDS = 24 * 60 * 60  # 24h
_CATALOG_LOCK = threading.Lock()

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


# Alternate spellings a user (or /providers --connect) may have used when storing a key
_PROVIDER_KEY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "anthropic": ("claude",),
    "phantomboxai": ("phantombox-ai", "phantombox_ai"),
    "grok": ("xai",),
    "qwen": ("dashscope",),
    "qianfan": ("baidu",),
}


def get_provider_key(provider_id: str) -> Optional[str]:
    """Retrieve stored API key for a provider.

    The stored config key may differ from the canonical provider id (e.g. the
    key saved under 'claude' for the 'anthropic' provider), so every known
    alias spelling is checked before giving up.
    """
    cfg = load_config()
    providers_cfg = cfg.get("providers", {}) or {}
    clean = provider_id.lower().strip()
    candidates = [clean, *(_PROVIDER_KEY_ALIASES.get(clean, ()))]

    def _read(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            key = str(value.get("api_key", "") or "").strip()
            return key or None
        if isinstance(value, str):
            key = value.strip()
            return key or None
        return None

    for cand in candidates:
        found = _read(providers_cfg.get(cand))
        if found:
            return found

    # Last resort: a stored key whose canonical provider is this provider.
    if isinstance(providers_cfg, dict):
        for stored, value in providers_cfg.items():
            canonical = get_provider(str(stored))
            if canonical and canonical.get("id") == clean:
                found = _read(value)
                if found:
                    return found
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
    # Clear cached models (including alias spellings of the same provider); a new
    # key may unlock a different catalog, so drop the persisted copy too.
    for key in (clean_id, *_PROVIDER_KEY_ALIASES.get(clean_id, ())):
        _REMOTE_MODELS_CACHE.pop(key, None)
        invalidate_catalog(key)


def is_provider_connected(provider_id: str) -> bool:
    """Check if an API key is configured for this provider, or if it requires no key (like phantombox router)."""
    p_info = get_provider(provider_id)
    if p_info and p_info.get("no_key_required"):
        return True
    return bool(get_provider_key(provider_id))


def list_connected_providers() -> List[str]:
    """List IDs of all currently connected providers."""
    return [pid for pid in PROVIDERS if is_provider_connected(pid)]


def _normalize_catalog_model_id(mid: str) -> str:
    """Normalize a model id reported by a provider /models endpoint.

    Google's OpenAI-compatible endpoint reports ids such as 'models/gemini-2.5-flash',
    but its chat completions endpoint expects the bare 'gemini-2.5-flash'. Returning
    the prefixed form makes every probe 404, so the provider never shows routes.
    """
    mid = (mid or "").strip()
    if mid.lower().startswith("models/"):
        mid = mid[len("models/"):]
    return mid


def _key_fingerprint(api_key: str) -> str:
    """Stable, non-reversible fingerprint of an API key (never store the key itself)."""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def _load_catalog_cache() -> Dict[str, Dict[str, Any]]:
    """Read the on-disk catalog cache; tolerate corruption / missing file."""
    try:
        if not CATALOG_CACHE_FILE.exists():
            return {}
        data = json.loads(CATALOG_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_catalog_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    """Persist the catalog cache atomically (tmp + replace) so it never truncates."""
    try:
        with _CATALOG_LOCK:
            tmp = CATALOG_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
            tmp.replace(CATALOG_CACHE_FILE)
    except Exception:
        pass


def _cached_catalog(clean_id: str, api_key: str, allow_stale: bool = False) -> Optional[List[str]]:
    """Return a persisted catalog for (provider, key) if present and fresh enough.

    A key change invalidates the entry: models fetched with a different key may
    not be accessible (or may include a different plan's catalog), so we re-fetch.
    """
    entry = _load_catalog_cache().get(clean_id)
    if not isinstance(entry, dict):
        return None
    if entry.get("key_fp") != _key_fingerprint(api_key):
        return None
    models = entry.get("models")
    if not isinstance(models, list) or not models:
        return None
    age = time.time() - float(entry.get("fetched_at", 0) or 0)
    if not allow_stale and age > CATALOG_TTL_SECONDS:
        return None
    return [str(m) for m in models]


def _store_catalog(clean_id: str, api_key: str, models: List[str]) -> None:
    """Write a successful catalog fetch to the persistent cache."""
    cache = _load_catalog_cache()
    cache[clean_id] = {
        "key_fp": _key_fingerprint(api_key),
        "models": models,
        "fetched_at": time.time(),
    }
    _save_catalog_cache(cache)


def catalog_cache_age(provider_id: str) -> Optional[float]:
    """Seconds since this provider's catalog was last fetched (None if never)."""
    entry = _load_catalog_cache().get(provider_id.lower().strip())
    if not isinstance(entry, dict) or not entry.get("fetched_at"):
        return None
    return max(0.0, time.time() - float(entry["fetched_at"]))


def invalidate_catalog(provider_id: Optional[str] = None) -> None:
    """Drop cached catalogs (one provider, or all) from memory and disk."""
    if provider_id is None:
        _REMOTE_MODELS_CACHE.clear()
        _save_catalog_cache({})
        return
    clean_id = provider_id.lower().strip()
    _REMOTE_MODELS_CACHE.pop(clean_id, None)
    cache = _load_catalog_cache()
    if cache.pop(clean_id, None) is not None:
        _save_catalog_cache(cache)


async def fetch_provider_models(provider_id: str, force: bool = False) -> List[str]:
    """Fetch the LIVE model catalog from the provider API.

    Never returns hardcoded defaults: models shown to the user are always ones
    the endpoint itself reports, so nothing can be 'Model no longer exists'.
    Failures are never memoized, so a transient error (timeout, rate limit) can
    be retried on the next call; pass force=True to bypass a successful cache.
    """
    clean_id = provider_id.lower().strip()
    p_info = get_provider(clean_id)
    if not p_info:
        return []

    api_key = get_provider_key(clean_id)
    if not api_key:
        return []

    # 1. In-process cache (fastest path)
    if not force and clean_id in _REMOTE_MODELS_CACHE:
        return list(_REMOTE_MODELS_CACHE[clean_id])

    # 2. Persistent cache from a previous session (survives restarts, TTL-bounded).
    #    A forced refresh skips this; a network failure below may still fall back
    #    to a stale catalog so an offline launch still shows models.
    if not force:
        persisted = _cached_catalog(clean_id, api_key)
        if persisted:
            _REMOTE_MODELS_CACHE[clean_id] = persisted
            return list(persisted)

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
                data = body.get("data", []) if isinstance(body, dict) else []
                if not isinstance(data, list):
                    data = []
                fetched_ids: List[str] = []
                for item in data:
                    mid = item.get("id") if isinstance(item, dict) else str(item)
                    if not mid or not isinstance(mid, str):
                        continue
                    mid = _normalize_catalog_model_id(mid)
                    if not mid:
                        continue
                    m_lower = mid.lower()
                    # Filter out non-chat / utility models
                    if any(bad in m_lower for bad in ("embed", "whisper", "tts", "dall-e", "moderation", "audio", "davinci", "babbage")):
                        continue
                    fetched_ids.append(mid)

                if fetched_ids:
                    result = sorted(set(fetched_ids))
                    _REMOTE_MODELS_CACHE[clean_id] = result
                    _store_catalog(clean_id, api_key, result)
                    return result
    except Exception:
        pass

    # Fetch failed: do NOT memoize the failure, so /models and /phantombox --routes
    # rescan can retry instead of showing an empty catalog for the whole session.
    # However, a stale on-disk catalog is better than nothing when the network or
    # provider is temporarily down (offline launch / rate limit).
    _REMOTE_MODELS_CACHE.pop(clean_id, None)
    stale = _cached_catalog(clean_id, api_key, allow_stale=True)
    if stale:
        return stale
    return []


def split_router_mode_suffix(model_identifier: str) -> Tuple[str, str]:
    """Split a router identifier from its routing-mode suffix.

    Returns (base, suffix) where suffix is one of ``shuffle``, ``overdrive`` or
    ``<provider>`` (as printed in the prompt tag, e.g. ``phantombox/router/shuffle``),
    or ``""`` when there is none. Any other trailing segment is *not* a mode and
    is left in ``base`` untouched, so a bogus argument never silently changes how
    the router behaves.
    """
    from phantombox.router import get_overdrive_model, get_provider_filter, is_shuffle_mode

    clean = (model_identifier or "").strip()
    valid_suffixes = {"shuffle"}
    try:
        od = get_overdrive_model()
        if od:
            valid_suffixes.add("overdrive")
        pf = get_provider_filter()
        if pf:
            valid_suffixes.add(str(pf).lower())
    except Exception:
        pass
    for base in ("phantombox/router", "phantombox/routes", "phantombox/router-adaptive"):
        if clean.lower().startswith(base + "/"):
            suffix = clean[len(base) + 1 :].strip().lower()
            if suffix in valid_suffixes or suffix == "overdrive":
                return base, suffix
    return clean, ""


def parse_model_identifier(model_identifier: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Parse a model string (e.g. 'openrouter:anthropic/claude-3.7-sonnet' or 'groq/llama-3.3-70b-versatile').
    Returns (provider_info, stripped_model_id).
    If it's a local model, returns (None, model_identifier).

    Router mode suffixes (``phantombox/router/shuffle``, ``.../overdrive``,
    ``phantombox/routes/<provider>``) are stripped first: without that, every
    mode-tagged identifier fell through to the local-model branch and was sent to
    a llama.cpp server that does not exist.
    """
    clean, _mode = split_router_mode_suffix(model_identifier)
    clean = clean.strip()
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


async def _stream_openai_compatible(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    label: str,
    model_id: str = "",
    timeout: float = 120.0,
) -> AsyncGenerator[Dict[str, Any], None]:
    """POST a streaming chat request and yield OpenAI-style delta objects.

    This is the single implementation shared by every OpenAI-compatible endpoint
    (local llama.cpp, Remote PhantomBox Server, and all cloud providers), so a
    provider addition stays a data-only change in PROVIDERS.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                err_text = (await response.aread()).decode("utf-8", errors="replace")
                try:
                    err_json = json.loads(err_text)
                    message = err_json.get("error", {}).get("message") or err_text
                except Exception:
                    message = err_text
                suffix = f" for model '{model_id}'" if model_id else ""
                raise RuntimeError(f"API Error ({response.status_code}) from {label}{suffix}: {message}")

            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"]
                except Exception:
                    continue
                if delta:
                    yield delta


def _model_tool_source(model_id: str) -> str:
    """The provider a model's tools come from ('local' for llama.cpp)."""
    from phantombox.providers import parse_model_identifier

    p_info, _raw = parse_model_identifier(model_id)
    if not p_info:
        return "local"
    if p_info.get("api_type") == "router":
        return "phantombox"
    return str(p_info.get("id", ""))


def _tools_for_source(
    source: str,
    tools: List[Dict[str, Any]],
    model_id: str = "",
) -> List[Dict[str, Any]]:
    """Filter a tool pool down to what one provider can actually use.

    PhantomBox's tool model is "built-ins + tools reachable from this provider".
    Concretely that means:

    * ``source == "phantombox"`` (router) -> the full pool. The router re-pools per
      route before dispatching, so it can never hand a tool to a model that
      cannot run it.
    * any other connected provider -> PhantomBox built-ins only. Cloud inference
      APIs do not publish their own tool catalog; the tools a cloud model can
      run are the ones the *client* declares in the request. A provider's tools
      are therefore whatever MCP servers/connectors you attached to this app
      (stdio, HTTP, or hosted remote MCP), which are surfaced as MCP tools.
    """
    if not tools:
        return []
    from phantombox.capabilities import supports_tools

    if source == "phantombox":
        return tools
    if model_id and supports_tools(source, model_id) is False:
        return []
    return [t for t in tools if "__" not in str(t.get("function", {}).get("name", ""))]


def _openai_base_body(model_id: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Common request body for OpenAI-compatible /chat/completions endpoints."""
    body: Dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return body


def _tools_for_model(provider_id: str, model_id: str, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pool the tool set that is actually valid for one specific model.

    Every connected endpoint receives the PhantomBox tool pool -- built-in tools
    *plus* every connected MCP server's tools -- but not blindly:

    * a model that is *known* not to support function calling gets no schemas at
      all (sending them causes 400s on strict endpoints and wasted tokens
      everywhere else);
    * some endpoints (notably Google's OpenAI-compat layer) reject schemas that
      carry non-standard keywords, so those are stripped before sending while
      the tool itself is kept -- the model still gets the capability.
    """
    if not tools:
        return []

    from phantombox.capabilities import supports_tools

    if supports_tools(provider_id, model_id) is False:
        return []

    if (provider_id or "").lower() in ("gemini", "google"):
        cleaned: List[Dict[str, Any]] = []
        for t in tools:
            fn = dict(t.get("function", {}) or {})
            fn.pop("strict", None)
            params = dict(fn.get("parameters") or {})
            params.pop("$schema", None)
            for prop in (params.get("properties") or {}).values():
                if isinstance(prop, dict):
                    prop.pop("default", None)
            fn["parameters"] = params
            cleaned.append({"type": "function", "function": fn})
        return cleaned

    return tools


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
        body = _openai_base_body(model_identifier, messages, tools)
        # Tiny local servers do not require a model id, but sending it is harmless
        # and makes llama.cpp's router mode pick the right slot.
        async for delta in _stream_openai_compatible(
            f"http://127.0.0.1:{local_port}/v1/chat/completions",
            {},
            body,
            label=f"local:{model_identifier}",
        ):
            yield delta
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
        body = _openai_base_body(raw_model_id, messages, _tools_for_model(srv_name, raw_model_id, tools))

        async for delta in _stream_openai_compatible(
            f"{base_url}/v1/chat/completions",
            headers,
            body,
            label=f"remote server '{srv_name}'",
            model_id=raw_model_id,
            timeout=180.0,
        ):
            yield delta
        return

    # 3. Remote Cloud Provider
    api_key = get_provider_key(p_info["id"])
    if not api_key:
        raise ValueError(f"No API key configured for provider '{p_info['name']}'. Run /providers --connect to set your key.")

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
        for t in _tools_for_model(p_info["id"], raw_model_id, tools):
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

    # Standard OpenAI-compatible endpoints (OpenRouter, Groq, OpenAI, Gemini, Qwen, DeepSeek, Mistral, NVIDIA)
    base_url = p_info["base_url"].rstrip("/")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(_provider_headers(p_info, api_key))

    body = _openai_base_body(
        raw_model_id,
        _prepare_messages(p_info["id"], messages),
        _tools_for_model(p_info["id"], raw_model_id, tools),
    )
    body.update(_provider_body_extras(p_info["id"], raw_model_id))

    async for delta in _stream_openai_compatible(
        url,
        headers,
        body,
        label=p_info["name"],
        model_id=raw_model_id,
    ):
        yield delta


def _provider_headers(p_info: Dict[str, Any], api_key: str) -> Dict[str, str]:
    """Provider-specific extra request headers (data-driven where possible)."""
    headers: Dict[str, str] = dict(p_info.get("extra_headers") or {})
    if p_info["id"] == "openrouter":
        headers.setdefault("HTTP-Referer", "https://phantombox.ai")
        headers.setdefault("X-Title", "phantombox")
    return {k: str(v) for k, v in headers.items()}


def _prepare_messages(provider_id: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Provider-specific message rewriting before the request is sent."""
    # Gemini-specific OpenAI compatibility handling for thought signatures:
    # When sending assistant tool_calls back in multi-turn conversations, Google
    # requires thought_signature on toolCall parts for thinking models.
    if provider_id == "gemini":
        cleaned: List[Dict[str, Any]] = []
        for msg in messages:
            msg_copy = dict(msg)
            if msg_copy.get("role") == "assistant" and msg_copy.get("tool_calls"):
                msg_copy["tool_calls"] = [dict(tc) for tc in msg_copy["tool_calls"]]
            cleaned.append(msg_copy)
        return cleaned
    return messages


def _provider_body_extras(provider_id: str, model_id: str) -> Dict[str, Any]:
    """Provider-specific extra body fields."""
    extras: Dict[str, Any] = {}
    # Disabling the thinking budget for Gemini reasoning models prevents the
    # thought_signature validation error on tool-call turns.
    if provider_id == "gemini" and ("gemini-3" in model_id or "preview" in model_id):
        extras["reasoning_effort"] = "none"
    return extras


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
