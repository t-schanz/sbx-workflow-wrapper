"""First-run bootstrap: install the bundled sbx kit and write the hx config."""

from importlib import resources
from pathlib import Path

import typer

from hx import HxError
from hx import config as config_module
from hx.sandbox import git_toplevel


def render_kit(name: str, email: str) -> str:
    template = resources.files("hx").joinpath("kit/spec.yaml.template").read_text()
    return template.format(name=name, email=email)


def write_kit(kit_dir: Path, content: str, force: bool) -> None:
    spec_path = kit_dir / "spec.yaml"
    if spec_path.exists() and not force:
        raise HxError(f"{spec_path} already exists — re-run with --force to overwrite")
    kit_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(content)


def git_identity() -> tuple[str, str]:
    from hx.sandbox import capture

    try:
        name = capture(["git", "config", "user.name"]).strip()
        email = capture(["git", "config", "user.email"]).strip()
    except HxError:
        name = email = ""
    if not name or not email:
        raise HxError(
            "git user.name / user.email are unset — configure them first "
            "(git config --global user.name ...)"
        )
    return name, email


def run_setup(force: bool) -> None:
    name, email = git_identity()

    kit_dir = Path("~/.config/sbx/kits/harpy-dev").expanduser()
    write_kit(kit_dir, render_kit(name, email), force=force)
    typer.echo(f"wrote kit to {kit_dir / 'spec.yaml'}")

    default_repo = git_toplevel() or ""
    repo = typer.prompt("HARPY repo path", default=default_repo or None)
    config_file = config_module.config_path()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(f'repo = "{Path(repo).expanduser()}"\n')
    typer.echo(f"wrote config to {config_file}")
