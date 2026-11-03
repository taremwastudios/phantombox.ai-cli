"""PhantomBox Autonomous Smart Router Engine.

When 'phantombox/router' is selected:
- Intelligently scans connected providers and candidate models until a 200 OK response is obtained.
- Implements an adaptive routing algorithm:
    * Records every model/provider that delivers a 200 OK response with latency.
    * Prioritizes proven working models (fastest/most reliable).
    * Conducts up to N (default: 10) random exploratory calls before defaulting to assured working models.
    * For users with multiple funded keys, cycles through verified high-speed models.
    * Returns response metadata (winning model, provider, elapsed time) to display:
      'Response returned by; <provider>/<model> via phantombox after Xs'.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from phantombox.config import load_config, set_config_val


def get_router_config() -> Dict[str, Any]:
    """Retrieve router configuration."""
    cfg = load_config()
    router_cfg = cfg.get("router")
    if not isinstance(router_cfg, dict):
        router_cfg = {
            "enabled": True,
            "max_exploratory_attempts": 10,
            "timeout_per_attempt": 6.0,
            "strategy": "adaptive",
            "successful_models": [],
        }
    return router_cfg


def save_router_config(router_cfg: Dict[str, Any]) -> None:
    """Save router configuration."""
    set_config_val("router", router_cfg)


def record_successful_model(provider_id: str, model_id: str, latency: float) -> None:
    """Store or update verified working model stats."""
    cfg = get_router_config()
    models = list(cfg.get("successful_models", []))
    # Normalise an already-prefixed model id ('openrouter/model' -> 'model') so the
    # same route can never be stored twice under different spellings.
    if model_id.startswith(f"{provider_id}/"):
        model_id = model_id[len(provider_id) + 1:]
    full_id = f"{provider_id}/{model_id}"

    # Check if already in list
    found = False
    for item in models:
        if (item.get("full_id") == full_id
                or (item.get("provider") == provider_id and item.get("model_id") == model_id)
                or (item.get("full_id") or "").lower() == full_id.lower()):
            item["success_count"] = item.get("success_count", 1) + 1
            item["last_latency"] = round(latency, 2)
            item["last_success"] = time.time()
            found = True
            break

    if not found:
        models.append({
            "full_id": full_id,
            "provider": provider_id,
            "model_id": model_id,
            "success_count": 1,
            "last_latency": round(latency, 2),
            "last_success": time.time(),
        })

    # Sort by success count and low latency
    models.sort(key=lambda x: (-x.get("success_count", 0), x.get("last_latency", 999)))
    cfg["successful_models"] = models[:50]  # retain top 50 proven models
    save_router_config(cfg)


def remove_successful_model(provider_id: str, model_id: str) -> bool:
    """Remove a model from the list of successful pings if it fails or returns an error."""
    cfg = get_router_config()
    models = list(cfg.get("successful_models", []))
    full_id = f"{provider_id}/{model_id}" if not model_id.startswith(f"{provider_id}/") else model_id

    original_count = len(models)
    models = [
        item for item in models
        if not (
            item.get("full_id") == full_id
            or (item.get("provider") == provider_id and item.get("model_id") == model_id)
            or (item.get("model_id") == full_id)
        )
    ]
    if len(models) < original_count:
        cfg["successful_models"] = models
        save_router_config(cfg)
        return True
    return False


def get_verified_models() -> List[Dict[str, Any]]:
    """List of all verified working models stored by router."""
    cfg = get_router_config()
    return cfg.get("successful_models", [])


def clear_router_history() -> None:
    """Reset router learning history."""
    cfg = get_router_config()
    cfg["successful_models"] = []
    save_router_config(cfg)


# ---------------------------------------------------------------------------
# Router routing modes: exclude list, shuffle, overdrive, provider filter
# ---------------------------------------------------------------------------

def _normalize_model_query(query: str) -> List[Tuple[str, str]]:
    """Match a user-typed model query ('mistral/ministral-8b-latest', 'ministral-8b',
    or 'ministral-8b-latest') against verified routes. Returns matching (provider, model)
    tuples. Matching is case-insensitive and supports partial (substring) model names.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    _, _, model_q = q.partition("/")
    matches: List[Tuple[str, str]] = []
    for v in get_verified_models():
        p = (v.get("provider") or "").lower()
        m = (v.get("model_id") or "").lower()
        full = f"{p}/{m}"
        if "/" in q:
            if full == q or (model_q and (m == model_q or model_q in m)):
                matches.append((v.get("provider"), v.get("model_id")))
        else:
            if q in p or q in m or q in full:
                matches.append((v.get("provider"), v.get("model_id")))
    return matches


def exclude_route(query: str) -> Tuple[bool, str]:
    """Explicitly exclude a model from the route list. Returns (ok, message)."""
    matches = _normalize_model_query(query)
    if not matches:
        return False, f"No verified route matches '{query}'. Run /phantombox --routes to see available routes."
    cfg = get_router_config()
    excluded = list(cfg.get("excluded_models", []))
    newly = []
    for p, m in matches:
        full = f"{p}/{m}"
        if full not in excluded:
            excluded.append(full)
            newly.append(full)
    cfg["excluded_models"] = excluded
    save_router_config(cfg)
    if newly:
        return True, f"Excluded {len(newly)} route(s): {', '.join(newly)}"
    return True, f"Already excluded: {', '.join(f'{p}/{m}' for p, m in matches)}"


def include_route(query: str) -> Tuple[bool, str]:
    """Re-include a previously excluded model. A system ping verifies it still
    returns 200 OK before it is restored to the active route list.
    Returns (ok, message).
    """
    cfg = get_router_config()
    excluded = list(cfg.get("excluded_models", []))
    if not excluded:
        return False, "No models are currently excluded."

    q = (query or "").strip().lower()
    hits = [e for e in excluded if q in e.lower()] if q else []
    if not hits:
        return False, f"'{query}' is not in the excluded list. Excluded: {excluded}"

    # Real 200 OK ping check before restoring each excluded route
    async def _check() -> Tuple[List[str], List[str]]:
        ok_list, bad_list = [], []
        for full in hits:
            pid, _, mid = full.partition("/")
            ok, _ = await _ping_model(pid, mid)
            (ok_list if ok else bad_list).append(full)
        return ok_list, bad_list

    restored, failed = _run_async(_check())
    cfg = get_router_config()
    kept = [e for e in list(cfg.get("excluded_models", [])) if e not in restored]
    cfg["excluded_models"] = kept

    # A route can be excluded yet absent from the verified table (e.g. it was
    # purged after an error). Coming back out of the exclude list must actually
    # re-add it, otherwise 'include' reports success but routing never uses it.
    existing = {(v.get("provider", ""), v.get("model_id", "")) for v in cfg.get("successful_models", [])}
    existing_lower = {f"{p}/{m}".lower() for p, m in existing}
    routes = list(cfg.get("successful_models", []))
    for full in restored:
        if full.lower() in existing_lower:
            continue
        pid, _, mid = full.partition("/")
        routes.append({
            "full_id": full,
            "provider": pid,
            "model_id": mid,
            "success_count": 1,
            "last_latency": 999,
            "last_success": time.time(),
        })
        existing_lower.add(full.lower())
    cfg["successful_models"] = routes
    save_router_config(cfg)
    msg_parts = []
    if restored:
        msg_parts.append(f"Re-included {len(restored)} route(s) after 200 OK ping: {', '.join(restored)}")
    if failed:
        msg_parts.append(f"Skipped (no longer 200 OK): {', '.join(failed)}")
    return bool(restored), ". ".join(msg_parts) or "Nothing re-included."


def get_excluded_models() -> List[str]:
    """List of user-excluded route ids ('provider/model')."""
    return list(get_router_config().get("excluded_models", []))


def _is_excluded(full_id: str, excluded_set: set) -> bool:
    """Exclusion matching that survives model id casing / prefix differences.

    Exclusion entries are stored by _normalize_model_query() using the *stored*
    spelling of the route, while the route queue may carry a different spelling
    of the same model (e.g. 'openrouter/DeepSeek-V3' vs 'openrouter/deepseek-v3').
    Compare case-insensitively, and also accept a suffix match so entries such as
    'mistral/ministral-8b' still exclude 'mistral/mistral-ministral-8b'.
    """
    low = (full_id or "").lower()
    if low in excluded_set:
        return True
    return any(e == low or low.endswith("/" + e) for e in excluded_set)


def set_shuffle_mode(enabled: bool) -> None:
    """Shuffle mode: route order is randomized on every call instead of fastest-first."""
    cfg = get_router_config()
    cfg["shuffle_mode"] = bool(enabled)
    save_router_config(cfg)


def is_shuffle_mode() -> bool:
    return bool(get_router_config().get("shuffle_mode", False))


def set_overdrive_model(query: str) -> Tuple[bool, str]:
    """Overdrive: commit to exactly one model until its quota dies."""
    matches = _normalize_model_query(query)
    if not matches:
        return False, f"No verified route matches '{query}'. Run /phantombox --routes to see available routes."
    p, m = matches[0]
    cfg = get_router_config()
    cfg["overdrive_model"] = f"{p}/{m}"
    cfg["overdrive_error"] = ""
    save_router_config(cfg)
    return True, f"Overdrive engaged: locked to {p}/{m} until its quota dies."


def clear_overdrive_model() -> None:
    cfg = get_router_config()
    cfg["overdrive_model"] = ""
    cfg["overdrive_error"] = ""
    save_router_config(cfg)


def get_overdrive_model() -> str:
    return str(get_router_config().get("overdrive_model", "") or "")


def split_route_id(full_id: str) -> Tuple[str, str]:
    """Split 'provider/model_id' on the FIRST slash only.

    Model ids routinely contain slashes (``openrouter/openai/gpt-4o-mini``,
    ``openrouter/meta-llama/llama-3.1-8b-instruct``). Naive ``partition("/")``
    handling elsewhere truncated those to ``openai`` / ``meta-llama`` and every
    request then failed with an unknown-model error.
    """
    pid, _, mid = (full_id or "").strip().partition("/")
    return pid, mid


def get_overdrive_error() -> str:
    """Model id that overdrive gave up on (empty when overdrive is healthy)."""
    return str(get_router_config().get("overdrive_error", "") or "")


def mark_overdrive_failed(model_id: str) -> None:
    """Stop overdrive dispatch and remember which model died.

    Overdrive is a deliberate lock, so a failure must NOT silently fall through
    to another model: the chat surfaces a system message telling the user to
    pick another model / release overdrive / change the overdrive setting.
    """
    cfg = get_router_config()
    cfg["overdrive_error"] = model_id or ""
    cfg["overdrive_model"] = ""
    save_router_config(cfg)


def clear_overdrive_error() -> None:
    cfg = get_router_config()
    cfg["overdrive_error"] = ""
    save_router_config(cfg)


def _connection_aliases() -> Dict[str, str]:
    """Map every stored provider connection key (lowercase) to its canonical id.

    Keys can arrive in several spellings depending on how they were connected:
    'anthropic', 'claude', 'phantomboxai', 'openrouter' ... Sub-provider keys such
    as 'openrouter' are canonicalized, everything else falls back to itself when it
    is a known provider id.
    """
    from phantombox.providers import PROVIDERS

    cfg = load_config()
    raw = cfg.get("providers", {}) or {}
    aliases: Dict[str, str] = {}
    for key in raw.keys() if isinstance(raw, dict) else []:
        k = str(key).strip().lower()
        if not k:
            continue
        p = PROVIDERS.get(k)
        aliases[k] = (p or {}).get("id", k)
    return aliases


def _resolve_connected_provider(query: str) -> str:
    """Resolve a user-typed provider name to a canonical connected provider id.

    Matching order (case-insensitive):
      1. exact provider id / stored connection key / provider display name
      2. provider tag (e.g. 'Claude' -> anthropic, 'Router' -> phantombox)
      3. unambiguous substring match
    Returns '' when nothing (or more than one provider) matches.
    """
    from phantombox.providers import PROVIDERS, list_connected_providers, get_provider_key

    q = (query or "").strip().lower()
    if not q:
        return ""

    connected = [p for p in list_connected_providers() if p != "phantombox"]
    aliases = _connection_aliases()

    # 1. exact id / stored connection key / display name / tag
    #    ('openrouter', 'Groq', 'Anthropic Claude', 'Claude', 'NVIDIA NIM', ...)
    for pid in connected:
        info = PROVIDERS.get(pid, {})
        names = {
            pid.lower(),
            str(info.get("name", "")).lower(),
            str(info.get("tag", "")).lower(),
        }
        if q in names:
            return pid
    canonical = aliases.get(q, q)
    if canonical in connected:
        return canonical

    # 2. unambiguous substring match across ids, names, tags and stored keys
    #    (case-insensitive). Only accepted when exactly one provider matches, so
    #    'gro' -> groq but 'a' never silently picks an arbitrary provider.
    hits = set()
    for pid in connected:
        info = PROVIDERS.get(pid, {})
        hay = " ".join([
            pid.lower(),
            str(info.get("name", "")).lower(),
            str(info.get("tag", "")).lower(),
        ])
        if q in hay:
            hits.add(pid)
    for k, canonical in aliases.items():
        if q in k and get_provider_key(canonical):
            hits.add(canonical)
    hits.discard("")
    if len(hits) == 1:
        return next(iter(hits))
    return ""


def verify_provider_active(provider_id: str, limit: int = 3) -> bool:
    """Live check that at least one model of `provider_id` answers a 200-OK probe.

    Used when engaging the provider filter so a connected key with dead quota is
    reported clearly instead of silently producing an empty route table.
    """
    from phantombox.providers import fetch_cached_or_default_models

    models = fetch_cached_or_default_models(provider_id)[:limit]
    if not models:
        return False

    async def _probe_all() -> bool:
        for mid in models:
            ok, _ = await _ping_model(provider_id, mid)
            if ok:
                return True
        return False

    return bool(_run_async(_probe_all()))


def set_provider_filter(query: str) -> Tuple[bool, str]:
    """Provider filter: only route through models of this provider."""
    q = (query or "").strip()
    if not q:
        return False, "Provider name required."
    cfg = get_router_config()
    match = _resolve_connected_provider(q)
    if not match:
        from phantombox.providers import list_connected_providers
        connected = [p for p in list_connected_providers() if p != "phantombox"]
        verified_providers = sorted({(v.get("provider") or "") for v in get_verified_models()})
        detail = f"connected: {connected or 'none'}"
        if verified_providers:
            detail += f"; with verified routes: {verified_providers}"
        return False, f"Provider '{q}' not found among connected providers ({detail})."

    # A connected key does not guarantee working models: make sure the provider
    # actually has verified routes, otherwise the filter would route nowhere.
    verified_providers = {(v.get("provider") or "").lower() for v in get_verified_models()}
    if match.lower() not in verified_providers:
        if not verify_provider_active(match):
            return False, (
                f"Provider '{match}' is connected but has no verified routes and none of its models "
                f"answered a probe. Check its quota/API key, then run /phantombox --routes rescan {match}."
            )

    cfg["provider_filter"] = match
    save_router_config(cfg)
    return True, f"Provider filter engaged: only routing through '{match}'."


def clear_provider_filter() -> None:
    cfg = get_router_config()
    cfg["provider_filter"] = ""
    save_router_config(cfg)


def get_provider_filter() -> str:
    return str(get_router_config().get("provider_filter", "") or "")


def _run_async(coro):
    """Run an async coroutine from sync code, handling an existing running loop."""
    import concurrent.futures
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor() as ex:
        return ex.submit(asyncio.run, coro).result()


def assemble_candidate_queue(
    connected_providers: List[str],
    provider_models_map: Dict[str, List[str]],
    max_exploratory: int = 10,
) -> List[Tuple[str, str, bool]]:
    """Assemble an intelligent model probe queue (provider_id, raw_model_id, is_assured).

    Adaptive algorithm logic:
    1. If verified working models exist, select the top assured models.
    2. Collect exploratory candidates from connected providers (randomized).
    3. Limit exploratory attempts to `max_exploratory` (default 10) before falling back to assured models.
    4. If many verified models exist, randomly interleave them for load distribution and speed.
    """
    verified = get_verified_models()
    verified_set = set()
    assured_queue: List[Tuple[str, str, bool]] = []

    for v in verified:
        p = v.get("provider", "")
        m = v.get("model_id", "")
        if p in connected_providers:
            assured_queue.append((p, m, True))
            verified_set.add((p, m))

    # Collect unverified exploratory models across connected providers
    unverified_pool: List[Tuple[str, str, bool]] = []
    for p in connected_providers:
        m_list = provider_models_map.get(p, [])
        for m in m_list:
            if (p, m) not in verified_set:
                unverified_pool.append((p, m, False))

    random.shuffle(unverified_pool)

    candidate_queue: List[Tuple[str, str, bool]] = []

    if assured_queue:
        # If user has verified models:
        # Try up to max_exploratory unverified candidates, then fall back immediately to assured models
        exploratory_picks = unverified_pool[:max_exploratory]
        # Interleave or place exploratory first, backed by guaranteed working ones
        candidate_queue.extend(exploratory_picks)
        # Randomize order of assured models if multiple exist to distribute calls
        shuffled_assured = list(assured_queue)
        random.shuffle(shuffled_assured)
        candidate_queue.extend(shuffled_assured)
    else:
        # No history yet: explore all connected models in random order
        candidate_queue.extend(unverified_pool)

    return candidate_queue


async def _ping_model(pid: str, mid: str, timeout: float = 6.0) -> Tuple[bool, float]:
    """Send a tiny probe request to a single cloud model endpoint.

    Returns (ok, latency). ok is True when the endpoint either accepted the request
    (HTTP 200) or streamed a genuinely useful first delta.

    Why the distinction matters: many healthy models begin a stream with a chunk
    that carries no text — a role-only preamble, a reasoning block, or nothing at
    all before the first token — and some answer a bare 'ping' with a tool call.
    Treating "first chunk has content" as the success test reported those models
    as dead, so a rescan silently dropped models that work fine when selected
    individually. Any HTTP-level failure (401/429/404, network error, timeout)
    still raises from the provider layer and is correctly rejected here.
    """
    from phantombox.providers import stream_chat_deltas

    probe_messages = [{"role": "user", "content": "ping"}]
    start = time.time()
    try:
        gen = stream_chat_deltas(
            model_identifier=f"{pid}:{mid}",
            local_port=8080,
            messages=probe_messages,
            tools=[],
        )
        # Just need the first chunk to confirm the endpoint is alive & authorized
        first = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
        latency = time.time() - start
        # Close the generator promptly to free the connection
        try:
            await gen.aclose()
        except Exception:
            pass

        # A chunk with real content/tools is an unambiguous success. An empty or
        # role-only delta still means the endpoint answered 200 and started a
        # valid stream, so it counts too (the connection may simply not have
        # produced its first token yet).
        if not isinstance(first, dict):
            return False, latency
        return True, latency
    except (asyncio.TimeoutError, StopAsyncIteration, Exception):
        return False, time.time() - start


async def scan_all_routes(max_concurrent: int = 16, timeout_per_model: Optional[float] = None, provider_filter: Optional[str] = None, force_catalog: bool = False) -> List[Dict[str, Any]]:
    """Ping EVERY cloud model available on /models via its real endpoint.

    - Excludes local models, phantombox/router and phantombox/subagents.
    - Runs probes concurrently (bounded) for a full scan in seconds, not minutes.
    - Only models returning a successful 200-OK-style response are stored in the
      routes list; anything erroring is dropped.
    - Results are persisted into router config as `successful_models` (the routes).
    - provider_filter: when given, only that provider's models are pinged. Existing
      verified routes from OTHER providers are preserved (merged) so a targeted
      rescan never wipes the rest of the route table.
    - force_catalog: re-fetch the provider /models catalog instead of trusting an
      earlier (possibly empty) session cache. Used by explicit rescans.

    Returns the fresh list of verified routes.
    """
    from phantombox.providers import list_connected_providers, fetch_cached_or_default_models, fetch_provider_models

    router_cfg = get_router_config()
    timeout = float(timeout_per_model or router_cfg.get("timeout_per_attempt", 6.0))

    connected = [p for p in list_connected_providers() if p != "phantombox"]

    # Targeted scan: restrict to the requested provider. The name must resolve
    # exactly the way /phantombox --routes provider --list prints it.
    if provider_filter:
        resolved = _resolve_connected_provider(provider_filter)
        if not resolved:
            return []
        connected = [resolved]

    if not connected:
        return []

    # Ensure every scanned provider's LIVE catalog is in the in-memory cache.
    # Without this the scan builds zero candidates and silently returns [] when
    # the chat warm-up task hasn't finished (or failed) in this process.
    await asyncio.gather(*(fetch_provider_models(pid, force=force_catalog) for pid in connected))

    candidates: List[Tuple[str, str]] = []
    for pid in connected:
        for mid in fetch_cached_or_default_models(pid):
            candidates.append((pid, mid))

    if not candidates:
        return []

    semaphore = asyncio.Semaphore(max(1, max_concurrent))

    async def bounded_ping(pid: str, mid: str) -> Tuple[str, str, bool, float]:
        async with semaphore:
            ok, latency = await _ping_model(pid, mid, timeout=timeout)
            return pid, mid, ok, latency

    # Probe in bounded waves instead of one giant gather. Providers rate-limit
    # aggressively under a thundering herd of simultaneous requests, which made
    # healthy models fail their probe; waves keep the pressure sane while still
    # finishing a large catalog quickly.
    results: List[Tuple[str, str, bool, float]] = []
    wave_size = max(1, max_concurrent)
    for start in range(0, len(candidates), wave_size):
        wave = candidates[start:start + wave_size]
        wave_results = await asyncio.gather(*(bounded_ping(pid, mid) for pid, mid in wave))
        results.extend(wave_results)

        # Start the next wave once the fastest responses land, rather than
        # waiting for the slowest model in the current one.
        await asyncio.sleep(0.15)

    # Rebuild the routes list: ONLY currently-responding models survive
    fresh_routes: List[Dict[str, Any]] = []
    for pid, mid, ok, latency in results:
        if ok:
            fresh_routes.append({
                "full_id": f"{pid}/{mid}",
                "provider": pid,
                "model_id": mid,
                "success_count": 1,
                "last_latency": round(latency, 2),
                "last_success": time.time(),
            })

    # Sort fastest-first: speed means everything
    fresh_routes.sort(key=lambda x: x.get("last_latency", 999))

    cfg = get_router_config()
    if provider_filter:
        # Targeted rescan: keep verified routes from OTHER providers intact,
        # replacing only the scanned provider's entries. Also carry over each
        # model's historical success_count so stats aren't reset every scan.
        p_lower = connected[0].lower() if connected else ""
        kept = [
            v for v in cfg.get("successful_models", [])
            if (v.get("provider") or "").lower() != p_lower
        ]
        old_stats = {
            v.get("full_id"): v
            for v in cfg.get("successful_models", [])
            if (v.get("provider") or "").lower() == p_lower
        }
        for r in fresh_routes:
            prev = old_stats.get(r["full_id"])
            if prev:
                r["success_count"] = prev.get("success_count", 1) + 1
        cfg["successful_models"] = sorted(kept + fresh_routes, key=lambda x: x.get("last_latency", 999))
    else:
        # Full rescan: the route table is rebuilt entirely
        cfg["successful_models"] = fresh_routes
    cfg["last_scan"] = time.time()
    save_router_config(cfg)

    return fresh_routes





def _tools_for_local(local_model: str, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tool pool for a local llama.cpp fallback (local names in one bucket)."""
    from phantombox.providers import _tools_for_source

    return _tools_for_source("local", tools, local_model)


async def stream_router_completion(
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    enable_subagents_mode: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute the phantombox/router flow — BUILT FOR SPEED, ZERO BACKGROUND WORK:

    1. If no verified routes exist yet (first ever use), runs one full scan of all
       cloud models so there is a route table to tap.
    2. INSTANTLY taps a verified route (fastest-latency first, respecting stickiness
       for mid-turn tool loops) — no probing loop delays the user's response.
    3. If a tapped route errors, it is scraped off the routes list immediately and
       the next route is tried — the user never waits on a dead endpoint.
    4. NO background rescans (they slowed the UI). Routes only change when models
       error out, or when the user explicitly runs `/phantombox --routes rescan`.
    """
    from phantombox.providers import (
        list_connected_providers,
        get_provider,
        fetch_cached_or_default_models,
        stream_chat_deltas,
        get_pinned_router_model,
        set_pinned_router_model,
        clear_pinned_router_model,
    )

    start_total_time = time.time()
    router_cfg = get_router_config()
    timeout_per_attempt = router_cfg.get("timeout_per_attempt", 6.0)

    # A previous overdrive run already died: stay stopped until the user picks a
    # new model / releases overdrive. Dispatch must never resume on its own.
    failed_overdrive = get_overdrive_error()
    if failed_overdrive and not get_overdrive_model():
        yield {
            "content": (
                f"Overdrive is halted because `{failed_overdrive}` failed earlier.\n\n"
                "Run one of these to continue:\n"
                "- `/phantombox --routes overdrive <model>` to lock onto another model\n"
                "- `/phantombox --routes overdrive off` to return to adaptive routing\n"
                "- `/models` to pick a specific working model"
            )
        }
        return

    connected = list_connected_providers()
    # Filter out phantombox itself to prevent recursion
    connected = [p for p in connected if p != "phantombox"]

    if not connected:
        yield {
            "content": (
                "[bold yellow]PhantomBox Router Notice:[/bold yellow] No external cloud providers connected yet!\n"
                "Connect providers using [cyan]/providers --connect[/cyan] (e.g. OpenRouter, Groq, Claude, OpenAI, Gemini) "
                "to allow the PhantomBox Router to discover and dispatch models."
            )
        }
        return

    # Ensure a route table exists at all (one-time scan on first request)
    verified = get_verified_models()
    if not verified:
        yield {"content": "[dim]PhantomBox Router: scanning available routes once (first run)...[/dim]\n"}
        verified = await scan_all_routes()
        if verified:
            yield {"content": f"[dim]Found [bold]{len(verified)}[/bold] working routes.[/dim]\n\n"}

    # Build the tap order from verified routes. Adaptive mode is fastest-first
    # with the pinned (stickiness) route first for mid-turn tool loops.
    # No unverified fallback candidates: only scanned 200-OK routes are used.
    #
    # User-controlled routing modes are applied here, in priority order:
    #   1. overdrive    — lock to exactly one model (until quota dies)
    #   2. provider     — only models from a specific provider
    #   3. excluded     — user-excluded models are stripped from the queue
    #   4. shuffle      — pick a random route per call (pin is ignored)
    pinned = get_pinned_router_model()
    full_queue: List[Tuple[str, str, bool]] = []

    excluded_set = {e.lower() for e in get_excluded_models()}
    provider_filter = get_provider_filter().lower()
    overdrive_full = get_overdrive_model().lower()

    shuffle = is_shuffle_mode()
    if overdrive_full:
        od_pid, od_mid = split_route_id(overdrive_full)
        full_queue.append((od_pid, od_mid, True))
    else:
        # Candidates = every verified route that survives the active filters.
        # Applying the filters to the *whole* table (rather than only to the tail)
        # means a pinned route can never bypass --routes provider / exclude.
        candidates: List[Tuple[str, str, bool]] = []
        seen_routes = set()
        for v in verified:
            pid_v, mid_v = v.get("provider", ""), v.get("model_id", "")
            if not pid_v or not mid_v or pid_v not in connected:
                continue
            key = (pid_v, mid_v)
            if key in seen_routes:
                continue
            if _is_excluded(f"{pid_v}/{mid_v}", excluded_set):
                continue
            if provider_filter and pid_v.lower() != provider_filter:
                continue
            seen_routes.add(key)
            candidates.append((pid_v, mid_v, True))

        # Tool-capability gating: when this turn carries tools, only verified
        # models that can actually emit tool calls are eligible. Models *known*
        # not to support them are kept as a last-resort tail (so a turn still
        # gets an answer rather than "no routes"), but shuffle and the pin are
        # never allowed to promote them into the working set.
        incapable: List[Tuple[str, str, bool]] = []
        if tools:
            from phantombox.capabilities import tools_supported_or_unknown
            capable = []
            for cand in candidates:
                (capable if tools_supported_or_unknown(cand[0], cand[1]) else incapable).append(cand)
            candidates = capable

        if shuffle and len(candidates) > 1:
            # Shuffle = pure random choice among ALL eligible verified models.
            # The previous implementation always kept the first entry (the
            # mid-turn pinned model) at the head, so every call re-selected the
            # same model. The pin is only a mid-turn/tool-loop stickiness signal
            # and must never win over an explicit shuffle request.
            random.shuffle(candidates)
        elif pinned and pinned[0] in connected:
            # Adaptive mode: prefer the pinned model (mid-turn tool loops), then
            # hand over to the fastest-first order for the rest of the queue.
            pin_key = (pinned[0], pinned[1])
            if pin_key in seen_routes:
                candidates = [c for c in candidates if (c[0], c[1]) != pin_key]
                candidates.insert(0, (pinned[0], pinned[1], True))

        full_queue = candidates + incapable

    if not full_queue:
        reasons = []
        if excluded_set:
            reasons.append(f"{len(excluded_set)} user-excluded")
        if provider_filter:
            reasons.append(f"provider filter '{provider_filter}'")
        extra = f" (filtered by: {', '.join(reasons)})" if reasons else ""
        yield {"content": f"[red]No models available across connected providers to route through{extra}.[/red]"}
        return

    winning_provider = ""
    winning_model = ""
    winning_time_str = ""

    # Channel the per-model tool pool back to the caller. stream_chat_deltas()
    # cannot know which route will win, so each candidate sends its OWN valid
    # tool set (below) and the winning spec is reported here: PhantomboxChat uses
    # it to resolve the active model, so the next turn's tool pool is built for
    # the model that actually answers rather than for 'phantombox/router'.
    winning_model = ""

    # Attempt models in queue.
    #   - Verified routes are tapped instantly (they passed the last scan).
    #   - If a tapped route still fails (quota died between scans), it is purged
    #     immediately and the next route/fallback candidate is tried.
    for pid, mid, _is_assured in full_queue:
        p_info = get_provider(pid)
        p_name = p_info.get("name", pid) if p_info else pid
        full_spec = f"{pid}:{mid}"

        buffered_chunks: List[Dict[str, Any]] = []

        # --- Pool tools for THIS model -------------------------------------
        # The router forwards the full PhantomBox pool, but each route receives
        # only what its own provider can use: built-ins for a plain cloud model,
        # or nothing at all for a model that cannot call tools.
        from phantombox.providers import _tools_for_source

        route_tools = _tools_for_source(pid, tools, mid)

        # --- Probe phase: confirm the model answers with real output ---
        # Read deltas until one carries actual content or a tool call. Healthy
        # models often start with a role-only preamble or a reasoning block; the
        # previous version accepted that empty first chunk as the whole answer and
        # streamed nothing, which looked like the route was broken.
        try:
            gen = stream_chat_deltas(
                model_identifier=full_spec,
                local_port=8080,
                messages=messages,
                tools=route_tools,
            )

            async def get_first_output():
                async for candidate in gen:
                    if candidate.get("tool_calls") or (candidate.get("content") or "").strip():
                        return candidate
                raise StopAsyncIteration

            first_delta = await asyncio.wait_for(get_first_output(), timeout=timeout_per_attempt)
            buffered_chunks.append(first_delta)
        except (asyncio.TimeoutError, StopAsyncIteration, Exception):
            # Model failed returning an error / timed out / streamed nothing:
            # purge it from the successful pings list and clear stickiness if pinned.
            # A stray empty stream is NOT proof the route is dead, so only remove
            # it when no output arrived at all before the timeout.
            if not buffered_chunks:
                remove_successful_model(pid, mid)
            if pinned and (pid, mid) == pinned:
                clear_pinned_router_model()
            continue

        # --- Win phase: model verified, stream the full response ---
        elapsed = time.time() - start_total_time
        winning_provider = p_name
        winning_model = f"{pid}/{mid}"
        winning_time_str = f"{elapsed:.1f}s" if elapsed >= 1.0 else f"{int(elapsed*1000)}ms"

        # Lock stickiness so subsequent tool-calling loops use this exact model
        set_pinned_router_model(pid, mid)

        # Record in adaptive learning history
        record_successful_model(pid, mid, elapsed)

        # If in phantombox/subagents mode: dynamically configure or ensure helper subagent exists
        if enable_subagents_mode:
            from phantombox.subagents import get_subagent, register_subagent
            # If default subagent1 does not exist, initialize it with this responsive model
            sub1 = get_subagent("subagent1")
            if not sub1:
                register_subagent(
                    nickname="subagent1",
                    model=f"{pid}:{mid}",
                    role="Autonomous Helper",
                    scripts=["suggestions"],
                )

        # Yield initial buffered chunk(s)
        for c in buffered_chunks:
            yield c

        # Stream the remainder of the response.
        # If the stream dies mid-response, purge the model as well so it is not
        # treated as reliably working on the next turn.
        try:
            async for remaining_delta in gen:
                yield remaining_delta
        except Exception:
            remove_successful_model(pid, mid)

        # Append imprint attribution badge (using standard clean Markdown instead of Rich markup tags)
        mode_str = " (subagent1)" if enable_subagents_mode else ""
        imprint_footer = (
            f"\n\n---\n"
            f"*Response returned by: **{winning_model}** via **phantombox**{mode_str} after {winning_time_str}*"
        )
        yield {
            "content": imprint_footer,
            "router_meta": {
                "model": winning_model,
                "provider": winning_provider,
                "time": winning_time_str,
                "subagents_mode": enable_subagents_mode,
                "resolved_from": "phantombox/router",
            },
        }
        return

    # If ALL verified routes failed (every one errored and got scraped off the list):
    clear_pinned_router_model()
    total_elapsed = time.time() - start_total_time
    remaining = len(get_verified_models())

    # --- Overdrive failure: a lock is a lock -------------------------------
    # Overdrive means "use exactly this model until it breaks". When it breaks,
    # silently rerouting to a different model would defeat the whole point, so
    # dispatch stops here and the user is told exactly how to proceed.
    if overdrive_full:
        mark_overdrive_failed(overdrive_full)
        yield {
            "content": (
                "**PhantomBox Overdrive stopped.**\n\n"
                f"Overdrive was locked to `{overdrive_full}`, and that model has failed "
                "(quota exhausted, rate limited, or the model is unavailable).\n"
                "Per overdrive rules the router did **not** fall back to another model.\n\n"
                "**Choose one of these to continue:**\n"
                "1. **Pick another model** — `/phantombox --routes overdrive <model>` with one of:\n"
                + "\n".join(
                    f"     - `{v.get('provider')}/{v.get('model_id')}`"
                    for v in get_verified_models()[:10]
                )
                + "\n2. **Go back to adaptive routing** — `/phantombox --routes overdrive off` "
                "(or switch to `phantombox/router` in `/models`).\n"
                "3. **Re-verify the routes** — `/phantombox --routes rescan` if you think quota was restored.\n"
                "4. **Switch models directly** — `/models` to choose a specific working model."
            ),
            "router_meta": {"overdrive_failed": overdrive_full},
        }
        return

    # Automatic Fallback: Check if any local model is currently running
    from phantombox.runtime import list_running
    running_local = list_running()

    if running_local:
        local_name = next(iter(running_local.keys()))
        local_port = running_local[local_name].get("port", 8080)
        fallback_notice = (
            f"> *Notice: All cloud model resources exhausted. Automatically falling back to local model `{local_name}` (port {local_port}).*\n\n"
        )
        yield {"content": fallback_notice}

        try:
            local_gen = stream_chat_deltas(
                model_identifier=local_name,
                local_port=local_port,
                messages=messages,
                tools=_tools_for_local(local_name, tools),
            )
            async for local_delta in local_gen:
                yield local_delta

            imprint_footer = (
                f"\n\n---\n"
                f"*Response returned by: **local/{local_name}** via **phantombox** after {time.time() - start_total_time:.1f}s*"
            )
            yield {"content": imprint_footer}
            return
        except Exception as local_err:
            yield {"content": f"\n\nLocal fallback error: {local_err}"}
            return

    # No local model running: give clear, actionable instructions
    yield {
        "content": (
            f"**PhantomBox Router Alert:** All verified routes are exhausted ({len(full_queue)} routes were scraped off "
            f"after errors across {len(connected)} providers in {total_elapsed:.1f}s; {remaining} routes remain on the list).\n\n"
            f"**What you can do:**\n"
            f"1. **Re-scan all routes** to find models whose quota is still valid:\n"
            f"   - Run [cyan]/phantombox --routes rescan[/cyan] — this re-pings every cloud model and rebuilds the route table\n"
            f"2. **Start a local offline model** (zero cost, zero API keys):\n"
            f"   - Run `phantombox models -l` to see downloaded models\n"
            f"   - Run `phantombox start <model_name>` (e.g. `phantombox start qwen2.5-0.5b`)\n"
            f"   - Or switch directly to any downloaded model in `/models`\n"
            f"3. **Check your cloud credits & API keys** via `/providers --connect`\n"
            f"4. **Download a lightweight local model** using `phantombox pull qwen2.5-0.5b`"
        )
    }
