"""Model management for phantombox."""

from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    TextColumn,
)
from rich.table import Table

from phantombox.env import get_models_dir

console = Console()

# ----------------------------------------------------------------------
# Curated models
# ----------------------------------------------------------------------

RECOMMENDED_MODELS = {
    "qwen2.5-0.5b": {
        "name": "Qwen2.5-0.5B-Instruct",
        "size": "≈ 400 MB",
        "quant": "Q4_K_M",
        "url": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "description": "Very small & fast – good for testing",
        "filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
    },
    "qwen2.5-1.5b": {
        "name": "Qwen2.5-1.5B-Instruct",
        "size": "≈ 1.0 GB",
        "quant": "Q4_K_M",
        "url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "description": "Small but surprisingly capable",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    },
    "llama3.2-1b": {
        "name": "Llama-3.2-1B-Instruct",
        "size": "≈ 800 MB",
        "quant": "Q4_K_M",
        "url": "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "description": "Meta’s small instruction model",
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
    },
    "phi3.5-mini": {
        "name": "Phi-3.5-mini-instruct",
        "size": "≈ 2.2 GB",
        "quant": "Q4_K_M",
        "url": "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf",
        "description": "Strong small model from Microsoft",
        "filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
    },
}


def get_model_info(name: str) -> dict | None:
    """Look up a model by its short name (case-insensitive)."""
    key = name.lower().strip()
    return RECOMMENDED_MODELS.get(key)


def list_downloaded_models() -> list[Path]:
    """Return a list of .gguf files in the models directory."""
    models_dir = get_models_dir()
    return sorted(models_dir.glob("*.gguf"))


def print_recommended_models() -> None:
    """Show models that can be pulled."""
    table = Table(title="Recommended Models", show_lines=True)
    table.add_column("Command name", style="cyan", no_wrap=True)
    table.add_column("Full name")
    table.add_column("Size", style="green")
    table.add_column("Description")

    for key, m in RECOMMENDED_MODELS.items():
        table.add_row(key, m["name"], m["size"], m["description"])

    console.print(table)
    console.print()
    console.print("To download a model:")
    console.print("  [cyan]phantombox pull <command-name>[/cyan]")
    console.print("Example: [cyan]phantombox pull qwen2.5-0.5b[/cyan]\n")


def print_downloaded_models() -> None:
    """Show models already on disk."""
    models = list_downloaded_models()

    if not models:
        console.print("[yellow]No models downloaded yet.[/yellow]")
        console.print(f"Models directory: [dim]{get_models_dir()}[/dim]\n")
        return

    table = Table(title="Downloaded Models", show_lines=True)
    table.add_column("Filename", style="cyan")
    table.add_column("Size", style="green")

    for path in models:
        size_mb = path.stat().st_size / (1024 * 1024)
        table.add_row(path.name, f"{size_mb:.1f} MB")

    console.print(table)
    console.print()


# ----------------------------------------------------------------------
# Download logic
# ----------------------------------------------------------------------

class DownloadCancelled(Exception):
    pass


def _cleanup_part_file(part_path: Path):
    """Safely delete a partial download."""
    try:
        if part_path.exists():
            part_path.unlink()
            console.print(f"[dim]Cleaned up incomplete file: {part_path.name}[/dim]")
    except Exception:
        pass


def download_model(name: str) -> bool:
    """Download a model by its short name. Returns True on success."""
    info = get_model_info(name)
    if not info:
        console.print(f"[red]Unknown model:[/red] {name}")
        console.print("Run [cyan]phantombox models[/cyan] to see available models.\n")
        return False

    models_dir = get_models_dir()
    final_path = models_dir / info["filename"]
    part_path = models_dir / (info["filename"] + ".part")

    if final_path.exists():
        console.print(f"[green]Model already exists:[/green] {final_path.name}")
        return True

    # Clean any previous leftover .part file
    if part_path.exists():
        _cleanup_part_file(part_path)

    console.print(f"[bold]Downloading[/bold] {info['name']} ({info['size']})")
    console.print("[dim]Press [bold]c[/bold] to cancel[/dim]\n")

    cancelled = threading.Event()
    stop_listener = threading.Event()

    def key_listener():
        """Listen for 'c' key to cancel (Windows)."""
        try:
            import msvcrt
            while not stop_listener.is_set():
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode("utf-8", errors="ignore").lower()
                    if key == "c":
                        cancelled.set()
                        break
                time.sleep(0.05)
        except ImportError:
            pass

    listener = threading.Thread(target=key_listener, daemon=True)
    listener.start()

    def handle_exit(signum=None, frame=None):
        stop_listener.set()
        _cleanup_part_file(part_path)
        console.print("\n[yellow]Download interrupted. Partial file removed.[/yellow]")
        sys.exit(1)

    # Handle Ctrl+C and process kill
    signal.signal(signal.SIGINT, handle_exit)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_exit)

    try:
        with httpx.stream(
            "GET",
            info["url"],
            follow_redirects=True,
            timeout=httpx.Timeout(300.0, connect=30.0),
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))

            with Progress(
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Downloading", total=total or None)

                with open(part_path, "wb") as f:
                    for chunk in response.iter_bytes():
                        if cancelled.is_set():
                            progress.stop()
                            console.print()
                            confirm = console.input(
                                "[yellow]Are you sure you want to cancel the download? [y/N]: [/yellow]"
                            ).strip().lower()

                            if confirm in ("y", "yes"):
                                raise DownloadCancelled()
                            else:
                                # User chose to continue
                                cancelled.clear()
                                progress.start()
                                console.print("[green]Resuming download...[/green]")

                        f.write(chunk)
                        progress.update(task, advance=len(chunk))

        # Success → rename .part to final name
        part_path.rename(final_path)
        console.print(f"\n[bold green]Download complete![/bold green] → {final_path.name}")
        return True

    except DownloadCancelled:
        _cleanup_part_file(part_path)
        console.print("[yellow]Download cancelled. Partial file removed.[/yellow]")
        return False
    except Exception as e:
        _cleanup_part_file(part_path)
        console.print(f"[red]Download failed:[/red] {e}")
        return False
    finally:
        stop_listener.set()