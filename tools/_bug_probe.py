"""Confirm the reported bugs before fixing."""
import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from phantombox.providers import parse_model_identifier, get_provider

print("--- overdrive arg parsing ---")
od = "groq/llama-3.3-70b-versatile"
pid, _, mid = od.partition("/")
print("partition ->", repr(pid), repr(mid), "| provider known:", get_provider(pid) is not None)

for mid in ("openai/gpt-4o-mini", "meta-llama/llama-3.1-8b-instruct"):
    od2 = f"openrouter/{mid}"
    pid2, _, mid2 = od2.partition("/")
    print(f"{od2!r} -> pid={pid2!r} mid={mid2!r}  (model truncated!)")

print("\n--- model selection validation ---")
for target in ("gpt-4o", "gpt-4o-not-real", "totally-bogus"):
    p_info, raw = parse_model_identifier(target)
    print(f"{target!r}: provider={p_info and p_info['id']} raw={raw!r}")

print("\n--- router mode suffixes ---")
for m in ("phantombox/router", "phantombox/router/overdrive", "phantombox/routes/shuffle",
          "phantombox/routes/groq"):
    p, raw = parse_model_identifier(m)
    print(f"{m!r}: provider={p and p['id']} raw={raw!r}")
