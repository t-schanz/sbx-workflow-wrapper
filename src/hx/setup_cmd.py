"""First-run bootstrap: write the hx config."""

from pathlib import Path

import typer

from hx import config as config_module
from hx.sandbox import main_repo_root


def run_setup() -> None:
    config_file = config_module.config_path()
    if config_file.exists():
        typer.echo(f"{config_file} already exists — left unchanged")
        return
    default_repo = main_repo_root() or ""
    repo = typer.prompt("repo path", default=default_repo or None)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(f'repo = "{Path(repo).expanduser()}"\n')
    typer.echo(f"wrote config to {config_file}")
