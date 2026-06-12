"""Typer app with create/mr/rm/setup plus the hxmr/hxrm alias entry points."""

import functools
import shutil
from pathlib import Path
from typing import Annotated

import typer

from hx import HxError
from hx import config as config_module
from hx import sandbox, setup_cmd

app = typer.Typer(
    help="Sandbox workflow: one Docker sandbox + git worktree per feature branch.",
    no_args_is_help=True,
)


def handle_errors(function):
    """Turn expected failures into a one-line stderr message and exit code 1."""

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except HxError as error:
            typer.echo(f"hx: {error}", err=True)
            raise typer.Exit(1) from None

    return wrapper


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
@handle_errors
def create(
    context: typer.Context,
    branch: Annotated[
        str, typer.Argument(help="Feature branch to create the sandbox for.")
    ],
) -> None:
    """Create a sandbox + worktree for BRANCH, run the configured setup, then attach.

    Extra flags after BRANCH are passed through verbatim to `sbx create`.
    """
    config = config_module.load_config()
    name = sandbox.sanitize_name(branch)
    git_user_name, git_user_email = sandbox.git_identity()
    sandbox.ensure_branch(config.repo, branch, config.target)

    sandbox.run(
        [
            "sbx",
            "create",
            "--branch",
            branch,
            "--name",
            name,
            "--cpus",
            str(config.cpus),
            "--memory",
            config.memory,
            "claude",
            config.repo,
            *context.args,
        ]
    )

    typer.echo("provisioning the sandbox (git identity, pre-commit, claude plugins)...")
    for provision_command in sandbox.provision_commands(
        name, git_user_name, git_user_email
    ):
        try:
            sandbox.run(provision_command)
        except HxError as error:
            typer.echo(f"provisioning step failed (continuing): {error}")

    worktree = sandbox.find_worktree(config.repo, branch)

    for relative_path in config.copy_files:
        source = Path(config.repo) / relative_path
        if source.exists():
            destination = worktree / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, destination)

    if config.post_create:
        typer.echo("running post-create setup inside the sandbox...")
        try:
            sandbox.run(
                [
                    "sbx",
                    "exec",
                    name,
                    "--",
                    "sh",
                    "-c",
                    f"cd '{worktree}' && {config.post_create}",
                ]
            )
        except HxError:
            typer.echo("post-create setup failed — run it manually inside the sandbox")

    sandbox.run(["sbx", "run", name], check=False)


@app.command()
@handle_errors
def mr(
    branch: Annotated[str, typer.Argument(help="Feature branch to push.")],
    target: Annotated[
        str | None,
        typer.Argument(
            help="MR target branch (default: configured target, usually main)."
        ),
    ] = None,
) -> None:
    """Push BRANCH from the host and auto-create a GitLab merge request."""
    config = config_module.load_config()
    worktree = sandbox.find_worktree(config.repo, branch)
    if sandbox.worktree_is_dirty(worktree) and not typer.confirm(
        f"worktree for {branch} has uncommitted changes — push anyway?"
    ):
        raise HxError("aborted — commit the changes in the sandbox first")
    sandbox.run(sandbox.mr_push_command(worktree, target or config.target))


@app.command()
@handle_errors
def rm(
    branch: Annotated[
        str, typer.Argument(help="Feature branch whose sandbox to remove.")
    ],
) -> None:
    """Remove the sandbox, its worktree, and the branch (prompts if work would be lost)."""
    config = config_module.load_config()
    unpushed = sandbox.unpushed_commit_count(config.repo, branch, config.target)
    if unpushed > 0 and not typer.confirm(
        f"branch {branch} has {unpushed} unpushed commit(s) — delete anyway?"
    ):
        raise HxError("aborted")
    sandbox.run(["sbx", "rm", "--force", sandbox.sanitize_name(branch)])


@app.command()
@handle_errors
def setup() -> None:
    """Write the hx config (first-run bootstrap); existing config is left unchanged."""
    setup_cmd.run_setup()


def main() -> None:
    app()


def hxmr_main() -> None:
    """Alias entry point: hxmr BRANCH [TARGET] == hx mr BRANCH [TARGET]."""
    typer.run(mr)


def hxrm_main() -> None:
    """Alias entry point: hxrm BRANCH == hx rm BRANCH."""
    typer.run(rm)
