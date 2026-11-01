import typer
from rich import print
from rich.console import Console

from phantombox import env

app = typer.Typer(
    name="phantombox",
    help="Local-first offline AI runtime (phantombox.ai)",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

console = Console()


@app.command()
def setup(
    force: bool = typer.Option(False, "--force", help="Force re-install even if llama is found"),
    build: bool = typer.Option(False, "--build", help="Try to build from source"),
):
    """
    Check the environment and install llama.cpp if needed.
    """
    console.print("\n[bold blue]phantombox setup[/bold blue]\n")

    status = env.print_environment_status()

    if not status["all_critical_ok"]:
        console.print("[bold red]Critical requirements missing.[/bold red]")
        console.print("Please install the missing tools and run `phantombox setup` again.\n")
        raise typer.Exit(code=1)

    console.print("[green]Environment looks good.[/green]\n")

    from phantombox.llama_cpp import get_llama_binary, install_official, install_from_source

    existing = get_llama_binary()
    if existing and not force and not build:
        console.print(f"[green]llama.cpp already available:[/green] {existing}")
        console.print("Use [bold]phantombox setup --force[/bold] to reinstall.\n")
        return

    if build:
        success = install_from_source()
    else:
        success = install_official()

    if success:
        binary = get_llama_binary()
        console.print(f"\n[bold green]Setup complete![/bold green] → {binary}")
        console.print("You can now run:")
        console.print("  [cyan]phantombox pull <model-url>[/cyan]")
        console.print("  [cyan]phantombox chat <model>[/cyan]\n")
    else:
        console.print("\n[bold red]Could not install llama.cpp automatically.[/bold red]")
        console.print("You can try:")
        console.print("  • [cyan]phantombox setup --force[/cyan]")
        console.print("  • Or install manually with: [cyan]winget install llama.cpp[/cyan]")
        console.print("  • Or build from source later.\n")
        raise typer.Exit(code=1)


@app.command()
def pull(
    name: str = typer.Argument(..., help="Model name (e.g. qwen2.5-0.5b)"),
):
    """
    Download a model by name.
    """
    from phantombox.models import download_model

    success = download_model(name)
    if not success:
        raise typer.Exit(code=1)


@app.command()
def chat(
    model: str = typer.Argument(None, help="Optional model name to connect or start"),
):
    """
    Open the phantombox chat interface.
    """
    from phantombox.chat import run_chat
    run_chat(model)


@app.command()
def models(
    list_downloaded: bool = typer.Option(
        False, "--list", "-l", help="Show models already downloaded on this machine"
    ),
):
    """
    Show available or downloaded models.
    """
    from phantombox import models as models_mod

    if list_downloaded:
        models_mod.print_downloaded_models()
    else:
        models_mod.print_recommended_models()    


@app.command()
def info():
    """Show current phantombox paths and environment info."""
    env.print_environment_status()

@app.command()
def start(
    model: str = typer.Argument(..., help="Model name to start"),
    port: int = typer.Option(8080, help="Port to run the server on"),
):
    """Start a model server in the background."""
    from phantombox.runtime import start_model
    success = start_model(model, port=port)
    if not success:
        raise typer.Exit(code=1)


@app.command()
def stop(
    model: str = typer.Argument(..., help="Model name to stop"),
):
    """Stop a running model server."""
    from phantombox.runtime import stop_model
    success = stop_model(model)
    if not success:
        raise typer.Exit(code=1)


server_app = typer.Typer(help="Manage PhantomBox LAN AI server, auth, and discovery")
app.add_typer(server_app, name="server")


@server_app.command("start")
def cli_server_start(
    port: int = typer.Option(8443, "--port", "-p", help="Port to run server on"),
):
    """Start the PhantomBox LAN AI server."""
    from phantombox.server import start_server, get_local_ip
    success, msg = start_server(port=port)
    if success:
        console.print(f"[bold green]✓[/bold green] {msg}")
    else:
        console.print(f"[bold red]✗[/bold red] {msg}")
        raise typer.Exit(code=1)


@server_app.command("close")
def cli_server_close():
    """Close and stop the PhantomBox LAN server."""
    from phantombox.server import stop_server
    success, msg = stop_server()
    if success:
        console.print(f"[bold green]✓[/bold green] {msg}")
    else:
        console.print(f"[bold yellow]![/bold yellow] {msg}")


@server_app.command("user")
def cli_server_user(
    username: str = typer.Argument(..., help="Username"),
    password: str = typer.Argument(..., help="Password"),
    privilege: str = typer.Option("regular", "--privilege", "-r", help="Privilege level: all | regular | use-only"),
):
    """Create or update a server user with privilege."""
    from phantombox.server import set_user
    if privilege not in ("all", "regular", "use-only"):
        console.print(f"[bold red]Invalid privilege:[/bold red] {privilege}. Must be 'all', 'regular', or 'use-only'.")
        raise typer.Exit(code=1)
    set_user(username, password, privilege)
    console.print(f"[bold green]✓[/bold green] User '[bold]{username}[/bold]' configured with privilege '[bold cyan]{privilege}[/bold cyan]'.")


@server_app.command("users")
def cli_server_users():
    """List configured server users."""
    from phantombox.server import list_users
    users = list_users()
    console.print(f"\n[bold cyan]Configured PhantomBox Server Users ({len(users)}):[/bold cyan]")
    if not users:
        console.print("  [dim]No users configured. Use: phantombox server user <name> <password> --privilege all[/dim]\n")
        return
    for u, priv in users.items():
        color = "red" if priv == "all" else ("yellow" if priv == "regular" else "blue")
        console.print(f"  • [bold]{u}[/bold]: [{color}]{priv}[/{color}]")
    console.print()


@server_app.command("status")
def cli_server_status():
    """Check PhantomBox server status and IP."""
    from phantombox.server import is_server_running, get_server_config, get_local_ip, list_users
    running = is_server_running()
    cfg = get_server_config()
    ip = get_local_ip()
    port = cfg.get("port", 8443)
    disc = cfg.get("discoverable", True)
    console.print(f"\n[bold cyan]PhantomBox Server Status:[/bold cyan]")
    console.print(f"  • Status: {'[bold green]ONLINE[/bold green]' if running else '[bold yellow]OFFLINE[/bold yellow]'}")
    console.print(f"  • Local IP: [bold]{ip}[/bold] (Port: {port})")
    console.print(f"  • Discoverable: {'[green]YES[/green]' if disc else '[red]NO[/red]'}")
    console.print(f"  • Registered Users: {len(list_users())}\n")


if __name__ == "__main__":
    app()