"""Exercise the new validation + overdrive logic without a live network."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

import phantombox.config as C

C.CONFIG_FILE = Path(tempfile.mkdtemp()) / "config.json"
import phantombox.providers as P

P.CATALOG_CACHE_FILE = Path(tempfile.mkdtemp()) / "cat.json"
P._REMOTE_MODELS_CACHE["groq"] = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
P._REMOTE_MODELS_CACHE["openrouter"] = ["openai/gpt-4o-mini", "meta-llama/llama-3.1-8b-instruct"]

from phantombox.chat import PhantomChat
from phantombox import router as R

# Simulate a connected provider with a verified route table.
C.set_config_val("providers", {"groq": {"key": "test"}})
cfg = R.get_router_config()
cfg["successful_models"] = [
    {"provider": "groq", "model_id": "llama-3.3-70b-versatile", "success_count": 1, "last_latency": 0.3},
    {"provider": "openrouter", "model_id": "openai/gpt-4o-mini", "success_count": 1, "last_latency": 0.5},
]
R.save_router_config(cfg)

available = [
    ("groq:llama-3.3-70b-versatile", "●", True),
    ("openrouter:openai/gpt-4o-mini", "●", True),
    ("openrouter:meta-llama/llama-3.1-8b-instruct", "●", True),
    ("phantombox/router", "●", True),
    ("phantombox/subagents", "●", True),
    ("qwen2.5-0.5b", "●", True),
]

print("--- /models argument validation ---")
for arg in ["groq:llama-3.3-70b-versatile", "phantombox/router", "phantombox/router/shuffle",
            "phantombox/router/overdrive", "qwen2.5-0.5b", "gpt-4o", "totally-bogus",
            "groq:does-not-exist", "gpt-4o-mini-typo"]:
    ok, reason = PhantomChat._resolve_valid_model_argument(arg, available)
    print(f"  {arg:38} -> {ok!r:32} {reason[:60]}")

print("\n--- overdrive split of slashy model ids ---")
print("  ", R.split_route_id("openrouter/openai/gpt-4o-mini"))

print("\n--- overdrive failure halts dispatch ---")
od, msg = R.set_overdrive_model("openrouter/openai/gpt-4o-mini")
print("  set:", od, "|", msg)
print("  stored:", R.get_overdrive_model())
R.mark_overdrive_failed(R.get_overdrive_model())
print("  after failure -> model:", repr(R.get_overdrive_model()), "error:", repr(R.get_overdrive_error()))

print("\n--- tool pooling per model ---")
from phantombox.providers import _tools_for_model

tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}},
         {"type": "function", "function": {"name": "gh__issue", "parameters": {"type": "object"}}}]
print("  groq capable model  :", len(_tools_for_model("groq", "llama-3.3-70b-versatile", tools)))
print("  groq llama2 (no tools):", len(_tools_for_model("groq", "llama2-70b", tools)))
print("  gemini (sanitised)  :", len(_tools_for_model("gemini", "gemini-2.0-flash", tools)))
