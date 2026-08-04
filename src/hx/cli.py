"""Typer app with create/mr/rm/setup plus the hxmr/hxrm alias entry points."""

import functools
from pathlib import Path
from typing import Annotated

import typer

from hx import HxError
from hx import config as config_module
from hx import sandbox, setup_cmd

app = typer.Typer(
    help="Sandbox workflow: one Docker sandbox (sbx --clone) per feature branch.",
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


def prepare_sandbox(
    config: config_module.Config, branch: str, extra_sbx_flags: list[str]
) -> str:
    """Create a --clone sandbox for BRANCH and run the full setup, without attaching."""
    name = sandbox.sanitize_name(branch)
    git_user_name, git_user_email = sandbox.git_identity()

    # Keep the clone's base branch fresh; the clone itself has no remote creds.
    try:
        sandbox.run(["git", "-C", config.repo, "fetch", "origin", config.target])
    except HxError:
        typer.echo(
            f"could not refresh origin/{config.target} — basing the clone on local refs"
        )

    sandbox.run(
        [
            "sbx",
            "create",
            "--clone",
            "--name",
            name,
            "--cpus",
            str(config.cpus),
            "--memory",
            config.memory,
            *(["--template", config.template] if config.template else []),
            "claude",
            config.repo,
            *extra_sbx_flags,
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

    try:
        sandbox.provision_mcp(name)
    except (HxError, OSError, ValueError) as error:
        typer.echo(f"MCP setup failed (continuing): {error}")

    # The clone must exist before the steps below can touch it; unlike provisioning,
    # a failure here is fatal (handle_errors turns it into a clean exit 1).
    typer.echo("materializing the in-container clone...")
    sandbox.run(sandbox.materialize_clone_command(name))
    sandbox.run(
        sandbox.branch_checkout_command(name, config.repo, branch, config.target)
    )

    for relative_path in config.copy_files:
        source = Path(config.repo) / relative_path
        if source.exists():
            for command in sandbox.copy_file_commands(name, config.repo, relative_path):
                sandbox.run(command)

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
                    f"cd '{config.repo}' && {config.post_create}",
                ]
            )
        except HxError:
            typer.echo("post-create setup failed — run it manually inside the sandbox")

    return name


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
    """Create a --clone sandbox for BRANCH, run the configured setup, then attach.

    Extra flags after BRANCH are passed through verbatim to `sbx create`.
    """
    config = config_module.load_config()
    name = prepare_sandbox(config, branch, list(context.args))
    sandbox.run(["sbx", "run", "--name", name], check=False)


@app.command()
@handle_errors
def work(
    branch: Annotated[
        str, typer.Argument(help="Feature branch to create the sandbox for.")
    ],
    prompt_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="File whose contents are handed to the agent as its task.",
        ),
    ],
) -> None:
    """Provision a sandbox for BRANCH and let the agent work the prompt unattended.

    The agent's tools are allow-listed first (see sandbox.allow_unattended_tools_command)
    so nothing waits for an approval, and it commits
    on BRANCH inside the clone. Nothing is merged or pushed: the commits are fetched to
    the host afterwards, reachable via the sandbox-<name> remote and
    refs/sandboxes/<name>/*, so a human opens the merge request with `hx mr BRANCH`.
    """
    config = config_module.load_config()
    name = prepare_sandbox(config, branch, [])
    sandbox.run(sandbox.allow_unattended_tools_command(name))
    typer.echo(f"agent working on {branch} in sandbox {name}...")
    try:
        sandbox.run(sandbox.headless_agent_command(name, config.repo, prompt_file))
    finally:
        sandbox.fetch_sandbox(config.repo, name, check=False)


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
    """Fetch the sandbox's commits and open a GitLab merge request from the host."""
    config = config_module.load_config()
    name = sandbox.sanitize_name(branch)
    if sandbox.clone_is_dirty(name, config.repo) and not typer.confirm(
        f"clone for {branch} has uncommitted changes — push anyway?"
    ):
        raise HxError("aborted — commit the changes in the sandbox first")
    sandbox.fetch_sandbox(config.repo, name)
    sandbox.run(
        sandbox.mr_push_command(config.repo, name, branch, target or config.target)
    )


@app.command()
@handle_errors
def rm(
    branch: Annotated[
        str, typer.Argument(help="Feature branch whose sandbox to remove.")
    ],
) -> None:
    """Remove the sandbox and its host remote (prompts if unpushed work would be lost).

    Fetches first so the commits are mirrored into refs/sandboxes/<name>/* (which
    survive removal) before anything is deleted.
    """
    config = config_module.load_config()
    name = sandbox.sanitize_name(branch)
    sandbox.fetch_sandbox(config.repo, name, check=False)
    unpushed = sandbox.unpushed_commit_count(config.repo, name, branch, config.target)
    if unpushed > 0 and not typer.confirm(
        f"branch {branch} has {unpushed} unpushed commit(s) — delete anyway?"
    ):
        raise HxError("aborted")
    sandbox.run(["sbx", "rm", "--force", name])
    sandbox.remove_sandbox_remote(config.repo, name)


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
