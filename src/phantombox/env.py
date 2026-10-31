"""Environment detection and setup for phantombox."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from platformdirs import user_data_dir
from rich.console import Console
from rich.table import Table

console = Console()

APP_NAME = "phantombox"
APP_AUTHOR = "phantombox.ai"

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------

def get_data_dir() -> Path:
    """Return the platform-specific data directory for phantombox."""
    path = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_bin_dir() -> Path:
    path = get_data_dir() / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_models_dir() -> Path:
    path = get_data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_llama_dir() -> Path:
    path = get_data_dir() / "llama.cpp"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ----------------------------------------------------------------------
# Detection helpers
# ----------------------------------------------------------------------

def get_platform_info() -> dict:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        os_name = "macos"
    elif system == "windows":
        os_name = "windows"
    else:
        os_name = "linux"

    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine

    return {
        "os": os_name,
        "arch": arch,
        "system": system,
        "machine": machine,
        "python": sys.version.split()[0],
    }


def check_command(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def check_python_version() -> tuple[bool, str]:
    major, minor = sys.version_info[:2]
    ok = major == 3 and minor >= 10
    return ok, f"{major}.{minor}"


def run_command(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or result.stdout.strip()
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------------
# Main status report
# ----------------------------------------------------------------------

def print_environment_status() -> dict:
    """Print a clear status table and return the results as a dict."""
    info = get_platform_info()
    python_ok, python_ver = check_python_version()

    checks = {
        "Platform": f"{info['os']} ({info['arch']})",
        "Python ≥ 3.10": (python_ok, python_ver),
        "Git": (check_command("git"), shutil.which("git") or "not found"),
        "curl": (check_command("curl"), shutil.which("curl") or "not found"),
        "CMake": (check_command("cmake"), shutil.which("cmake") or "not found"),
    }

    # Compiler check (rough)
    if info["os"] == "windows":
        compiler_ok = check_command("cl") or check_command("gcc")
        compiler_name = "MSVC / GCC"
    elif info["os"] == "macos":
        compiler_ok = check_command("clang")
        compiler_name = "Clang (Xcode CLI tools)"
    else:
        compiler_ok = check_command("g++") or check_command("clang++")
        compiler_name = "g++ / clang++"

    checks["C++ Compiler"] = (compiler_ok, compiler_name)

    table = Table(title="phantombox Environment Check", show_header=True, header_style="bold")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Details")

    all_ok = True
    for name, value in checks.items():
        if isinstance(value, tuple):
            ok, detail = value
            status = "[green]OK[/green]" if ok else "[red]MISSING[/red]"
            if not ok:
                all_ok = False
            table.add_row(name, status, str(detail))
        else:
            table.add_row(name, "[green]OK[/green]", str(value))

    console.print(table)
    console.print()

    # Show paths
    console.print(f"[bold]Data directory:[/bold]  {get_data_dir()}")
    console.print(f"[bold]Models directory:[/bold] {get_models_dir()}")
    console.print(f"[bold]llama.cpp dir:[/bold]   {get_llama_dir()}")
    console.print()

    return {
        "platform": info,
        "python_ok": python_ok,
        "git_ok": check_command("git"),
        "curl_ok": check_command("curl"),
        "cmake_ok": check_command("cmake"),
        "compiler_ok": compiler_ok,
        "all_critical_ok": python_ok and check_command("git"),
    }