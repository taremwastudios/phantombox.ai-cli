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
    full_id = f"{provider_id}/{model_id}" if not model_id.startswith(f"{provider_id}/") else model_id

    # Check if already in list
    found = False
    for item in models:
        if item.get("full_id") == full_id or (item.get("provider") == provider_id and item.get("model_id") == model_id):
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

    Returns (ok, latency). ok is True ONLY if the endpoint answered with a valid
    streaming first chunk (i.e. an effective 200 OK). Any error, timeout, auth
    failure or quota exhaustion returns ok=False.
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
        await gen.aclose()
        content = first.get("content") if isinstance(first, dict) else None
        return True, latency if content is not None else (True, latency)[1] if False else latency
    except (asyncio.TimeoutError, StopAsyncIteration, Exception):
        return False, time.time() - start


async def scan_all_routes(max_concurrent: int = 8, timeout_per_model: Optional[float] = None) -> List[Dict[str, Any]]:
    """Ping EVERY cloud model available on /models via its real endpoint.

    - Excludes local models, phantombox/router and phantombox/subagents.
    - Runs probes concurrently (bounded) for a full scan in seconds, not minutes.
    - Only models returning a successful 200-OK-style response are stored in the
      routes list; anything erroring is dropped.
    - Results are persisted into router config as `successful_models` (the routes).

    Returns the fresh list of verified routes.
    """
    from phantombox.providers import list_connected_providers, fetch_cached_or_default_models

    router_cfg = get_router_config()
    timeout = float(timeout_per_model or router_cfg.get("timeout_per_attempt", 6.0))

    connected = [p for p in list_connected_providers() if p != "phantombox"]

    candidates: List[Tuple[str, str]] = []
    for pid in connected:
        for mid in fetch_cached_or_default_models(pid):
            candidates.append((pid, mid))

    if not candidates:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)

    async def bounded_ping(pid: str, mid: str) -> Tuple[str, str, bool, float]:
        async with semaphore:
            ok, latency = await _ping_model(pid, mid, timeout=timeout)
            return pid, mid, ok, latency

    results = await asyncio.gather(*(bounded_ping(pid, mid) for pid, mid in candidates))

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
    cfg["successful_models"] = fresh_routes
    cfg["last_scan"] = time.time()
    save_router_config(cfg)

    return fresh_routes





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

    connected = list_connected_providers()
    # Filter out phantombox itself to prevent recursion
    connected = [p for p in connected if p != "phantombox"]

    if not connected:
        yield {
            "content": (
                "[bold yellow]PhantomBox Router Notice:[/bold yellow] No external cloud providers connected yet!\n"
                "Connect providers using [cyan]/mcp --connect[/cyan] (e.g. OpenRouter, Groq, Claude, OpenAI, Gemini) "
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

    # Build the tap order strictly from verified routes (fastest first),
    # with pinned (stickiness) route at the very front for mid-turn tool loops.
    # No unverified fallback candidates: only scanned 200-OK routes are used.
    pinned = get_pinned_router_model()
    full_queue: List[Tuple[str, str, bool]] = []
    if pinned and pinned[0] in connected:
        full_queue.append((pinned[0], pinned[1], True))
    for v in verified:
        pid_v, mid_v = v.get("provider", ""), v.get("model_id", "")
        if pid_v in connected and (not pinned or (pid_v, mid_v) != pinned):
            full_queue.append((pid_v, mid_v, True))

    if not full_queue:
        yield {"content": "[red]No models available across connected providers to route through.[/red]"}
        return

    winning_provider = ""
    winning_model = ""
    winning_time_str = ""

    # Attempt models in queue.
    #   - Verified routes are tapped instantly (they passed the last scan).
    #   - If a tapped route still fails (quota died between scans), it is purged
    #     immediately and the next route/fallback candidate is tried.
    for pid, mid, _is_assured in full_queue:
        p_info = get_provider(pid)
        p_name = p_info.get("name", pid) if p_info else pid
        full_spec = f"{pid}:{mid}"

        buffered_chunks: List[Dict[str, Any]] = []

        # --- Probe phase: verify this model actually returns a 200 OK first chunk ---
        try:
            gen = stream_chat_deltas(
                model_identifier=full_spec,
                local_port=8080,
                messages=messages,
                tools=tools,
            )

            async def get_first_chunk():
                return await gen.__anext__()

            first_delta = await asyncio.wait_for(get_first_chunk(), timeout=timeout_per_attempt)
            buffered_chunks.append(first_delta)
        except (asyncio.TimeoutError, StopAsyncIteration, Exception):
            # Model failed returning an error / timed out / streamed nothing:
            # purge it from the successful pings list and clear stickiness if pinned.
            remove_successful_model(pid, mid)
            if pinned and pid == pinned[0] and mid == pinned[1]:
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
            },
        }
        return

    # If ALL verified routes failed (every one errored and got scraped off the list):
    clear_pinned_router_model()
    total_elapsed = time.time() - start_total_time
    remaining = len(get_verified_models())

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
                tools=tools,
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
            f"3. **Check your cloud credits & API keys** via `/mcp --connect`\n"
            f"4. **Download a lightweight local model** using `phantombox pull qwen2.5-0.5b`"
        )
    }
