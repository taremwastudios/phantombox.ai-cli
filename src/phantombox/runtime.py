"""Manage running model servers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from rich.console import Console

from phantombox.env import get_data_dir, get_models_dir
from phantombox.llama_cpp import get_llama_binary
from phantombox.models import get_model_info, list_downloaded_models

console = Console()

RUNTIME_FILE = get_data_dir() / "running.json"


def _load_running() -> dict:
    if not RUNTIME_FILE.exists():
        return {}
    try:
        return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_running(data: dict):
    RUNTIME_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def find_model_path(name: str) -> Path | None:
    """Resolve a model name to a .gguf file."""
    info = get_model_info(name)
    if info:
        path = get_models_dir() / info["filename"]
        if path.exists():
            return path

    # fallback: search by filename
    for p in list_downloaded_models():
        if name.lower() in p.name.lower() or name.lower() in p.stem.lower():
            return p
    return None


def start_model(name: str, port: int = 8080) -> bool:
    """Start a model server in the background."""
    binary = get_llama_binary()
    if not binary:
        console.print("[red]llama.cpp not found. Run [cyan]phantombox setup[/cyan] first.[/red]")
        return False

    model_path = find_model_path(name)
    if not model_path:
        console.print(f"[red]Model not found:[/red] {name}")
        console.print("Download it with: [cyan]phantombox pull <name>[/cyan]")
        return False

    running = _load_running()
    if name in running:
        console.print(f"[yellow]Model '{name}' is already running on port {running[name]['port']}[/yellow]")
        return True

    # Start llama server
    cmd = [
        str(binary),
        "serve",
        "-m", str(model_path),
        "--port", str(port),
        "--host", "127.0.0.1",
    ]

    try:
        # Start detached
        if os.name == "nt":
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
        else:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        # Give it a moment to start
        time.sleep(1.5)

        if process.poll() is not None:
            console.print("[red]Failed to start the model server.[/red]")
            return False

        running[name] = {
            "pid": process.pid,
            "port": port,
            "model_path": str(model_path),
            "started_at": time.time(),
        }
        _save_running(running)

        console.print(f"[green]Started[/green] {name} on port {port} (pid {process.pid})")
        return True

    except Exception as e:
        console.print(f"[red]Failed to start model:[/red] {e}")
        return False


def stop_model(name: str) -> bool:
    """Stop a running model."""
    running = _load_running()
    if name not in running:
        console.print(f"[yellow]Model '{name}' is not running.[/yellow]")
        return False

    info = running[name]
    pid = info["pid"]

    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)

        del running[name]
        _save_running(running)
        console.print(f"[green]Stopped[/green] {name}")
        return True
    except Exception as e:
        console.print(f"[red]Failed to stop model:[/red] {e}")
        # Clean up the registry anyway
        del running[name]
        _save_running(running)
        return False


def list_running() -> dict:
    return _load_running()