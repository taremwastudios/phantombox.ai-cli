"""Manage llama.cpp installation for phantombox."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from phantombox.env import get_bin_dir

console = Console()


def find_system_llama() -> Path | None:
    """Return an existing llama binary on PATH."""
    candidates = [
        "llama",
        "llama.exe",
        "llama-cli",
        "llama-cli.exe",
        "main",
        "main.exe",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return Path(path)
    return None


def get_llama_binary() -> Path | None:
    """Return the best available llama binary (system first, then our own)."""
    system = find_system_llama()
    if system:
        return system

    bin_dir = get_bin_dir()
    for name in ["llama.exe", "llama", "llama-cli.exe", "llama-cli"]:
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def install_official() -> bool:
    """Try the official llama.cpp installer."""
    system = platform.system().lower()

    if system == "windows":
        console.print("[bold]Running official Windows installer...[/bold]")
        console.print("This will download and install the latest llama.cpp binary.\n")
        try:
            # Official one-liner
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "irm https://llama.app/install.ps1 | iex"],
                check=False,
            )
            if result.returncode == 0 and find_system_llama():
                console.print("[green]Official installer succeeded.[/green]")
                return True
            else:
                console.print("[yellow]Official installer finished but binary not found on PATH yet.[/yellow]")
                console.print("You may need to restart the terminal.\n")
                return False
        except Exception as e:
            console.print(f"[red]Official installer failed: {e}[/red]")
            return False

    elif system in ("linux", "darwin"):
        console.print("[bold]Running official installer...[/bold]")
        try:
            result = subprocess.run(
                ["bash", "-c", "curl -LsSf https://llama.app/install.sh | sh"],
                check=False,
            )
            return result.returncode == 0 and find_system_llama() is not None
        except Exception as e:
            console.print(f"[red]Official installer failed: {e}[/red]")
            return False

    return False


def install_from_source() -> bool:
    """Placeholder for source build. Gives clear instructions."""
    console.print("\n[bold yellow]Source build is not fully automated yet.[/bold yellow]")
    console.print("To build llama.cpp yourself you need:")
    console.print("  • Git")
    console.print("  • CMake")
    console.print("  • A C++ compiler (Visual Studio Build Tools on Windows)")
    console.print()
    console.print("Recommended commands:")
    console.print("  git clone https://github.com/ggml-org/llama.cpp")
    console.print("  cd llama.cpp")
    console.print("  cmake -B build")
    console.print("  cmake --build build --config Release")
    console.print()
    return False