import typer
from rich import print

app = typer.Typer(
    name="phantombox",
    help="Local-first offline AI runtime (phantombox.ai)",
    no_args_is_help=True,
)

@app.command()
def setup():
    """Check environment and install llama.cpp."""
    print("[bold]phantombox setup[/bold] – coming next")

@app.command()
def pull(url: str):
    """Download a model from a URL or Hugging Face link."""
    print(f"Pulling {url} …")

@app.command()
def chat(model: str = typer.Argument(..., help="Model name or path")):
    """Chat with a local model."""
    print(f"Starting chat with {model} …")

if __name__ == "__main__":
    app()