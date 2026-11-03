"""Probe which Textual CSS properties/selectors are valid for the prompt card."""
import os
import sys
import tempfile

sys.path.insert(0, "src")

from textual.css.stylesheet import Stylesheet

TMP = os.path.join(tempfile.mkdtemp(), "probe.tcss")

PROPS = [
    "background: #161b22;",
    "border: round #f97316;",
    "border: outer round #f97316;",
    "border: inner round #f97316;",
    "border-top: round #f97316;",
    "padding: 0;",
    "outline: round #f97316;",
    "background-tint: 0%;",
    "border-title-color: #f97316;",
    "layers: base bg;",
]

SELECTORS = [
    "#a { display: block; }",
    "#a:focus-within { display: block; }",
    "#a > * { display: block; }",
    "#a:has(:focus) { display: block; }",
    "#a PromptArea { display: block; }",
    "#a > PromptArea { display: block; }",
    "#a > Widget { display: block; }",
    "#a:focus { display: block; }",
]


def deepest(exc):
    seen = set()
    while True:
        cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
        if cause is None or id(cause) in seen:
            return exc
        seen.add(id(cause))
        exc = cause


from pathlib import Path


def check(css: str) -> str:
    with open(TMP, "w", encoding="utf-8") as f:
        f.write(css + "\n")
    try:
        Stylesheet().read_all([Path(TMP)])
        return "OK"
    except Exception as exc:
        d = deepest(exc)
        return f"{type(d).__name__}: {d}"


print("-- properties --")
for p in PROPS:
    print(f"{check('#a { ' + p + ' }'):70}  {p}")

print("\n-- selectors --")
for sel in SELECTORS:
    print(f"{check(sel):70}  {sel}")

print("\n-- baseline --")
print(check("#a { display: block; }"))
