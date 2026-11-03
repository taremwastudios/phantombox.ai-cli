"""Verify provider-scoped tool pooling: exactly what each provider's models get."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import phantombox.config as C

C.CONFIG_FILE = Path(tempfile.mkdtemp()) / "config.json"
import phantombox.providers as P

P.CATALOG_CACHE_FILE = Path(tempfile.mkdtemp()) / "cat.json"

C.set_config_val("providers", {
    "groq": {"api_key": "gsk_test"},
    "openrouter": {"api_key": "sk-or-test"},
})
P._REMOTE_MODELS_CACHE["groq"] = ["llama-3.3-70b-versatile", "whisper-large-v3"]
P._REMOTE_MODELS_CACHE["openrouter"] = ["openai/gpt-4o-mini"]

from phantombox.providers import _tools_for_source, _model_tool_source

pool = [
    {"type": "function", "function": {"name": "read_file"}},
    {"type": "function", "function": {"name": "run_command"}},
    {"type": "function", "function": {"name": "github__create_issue"}},
    {"type": "function", "function": {"name": "fs__read"}},
]


def names(ts):
    return [t["function"]["name"] for t in ts]


print("=== provider-scoped pooling (_tools_for_source) ===")
cases = [
    ("groq:llama-3.3-70b-versatile", "groq", "llama-3.3-70b-versatile"),
    ("groq:whisper-large-v3", "groq", "whisper-large-v3"),
    ("openrouter:openai/gpt-4o-mini", "openrouter", "openai/gpt-4o-mini"),
    ("phantombox/router", "phantombox", ""),
    ("qwen2.5-0.5b (local)", "local", "qwen2.5-0.5b"),
]
for label, source, mid in cases:
    out = _tools_for_source(source, pool, mid)
    print(f"  {label:34} -> {names(out)}")

print("\n=== _tools_for_model (the send-site gate) ===")
from phantombox.providers import _tools_for_model

for pid, mid in [("groq", "llama-3.3-70b-versatile"), ("groq", "whisper-large-v3"),
                 ("anthropic", "claude-3-7-sonnet-latest")]:
    print(f"  {pid}/{mid:28} -> {len(_tools_for_model(pid, mid, pool))} tool(s)")

print("\n=== model -> tool source mapping ===")
for m in ["groq:llama-3.3-70b-versatile", "phantombox/router/shuffle", "qwen2.5-0.5b"]:
    print(f"  {m:34} -> {_model_tool_source(m)!r}")

print("\n=== live turn: what a groq model actually gets ===")
from phantombox.chat import PhantomChat


async def main():
    app = PhantomChat("groq:llama-3.3-70b-versatile", 8080)
    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.6)
        for model in ("groq:llama-3.3-70b-versatile", "groq:whisper-large-v3", "phantombox/router"):
            app.model_name = model
            tools = app._tools_for_active_model()
            print(f"  {model:32} -> {len(tools)} tool(s)  notice={app._tools_disabled_notice_pending}")


asyncio.run(main())
