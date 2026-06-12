"""Thin subprocess layer for sbx/git, worktree parsing, name sanitizing."""

import subprocess
from pathlib import Path

from hx import HxError


def sanitize_name(branch: str) -> str:
    """Sandbox names must not contain '/'."""
    return branch.replace("/", "-")


def run(command: list[str], check: bool = True) -> None:
    """Run a command streaming its output to the terminal."""
    result = subprocess.run(command)
    if check and result.returncode != 0:
        raise HxError(
            f"command failed with exit code {result.returncode}: {' '.join(command)}"
        )


def capture(command: list[str]) -> str:
    """Run a command and return its stdout (for parsing)."""
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise HxError(
            f"command failed with exit code {result.returncode}: {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout


def succeeds(command: list[str]) -> bool:
    """Run a quiet probe command and report whether it exited 0."""
    return subprocess.run(command, capture_output=True).returncode == 0


def parse_worktrees(porcelain: str) -> dict[str, Path]:
    """Map branch name -> worktree path from `git worktree list --porcelain` output.

    Detached-HEAD worktrees have no `branch` line and are skipped.
    """
    worktrees: dict[str, Path] = {}
    current_path: Path | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/") and current_path is not None:
            worktrees[line.removeprefix("branch refs/heads/")] = current_path
    return worktrees


def find_worktree(repo: str, branch: str) -> Path:
    porcelain = capture(["git", "-C", repo, "worktree", "list", "--porcelain"])
    worktrees = parse_worktrees(porcelain)
    if branch not in worktrees:
        raise HxError(
            f"no worktree found for branch {branch} — did you run `hx create`?"
        )
    return worktrees[branch]


def mr_push_command(worktree: Path, target: str) -> list[str]:
    return [
        "git",
        "-C",
        str(worktree),
        "push",
        "-u",
        "origin",
        "HEAD",
        "-o",
        "merge_request.create",
        "-o",
        f"merge_request.target={target}",
        "-o",
        "merge_request.remove_source_branch",
    ]


def unpushed_commit_count(repo: str, branch: str, base: str) -> int:
    """Commits on `branch` not on `base`, or 0 when deletion is safe.

    Safe means: the branch doesn't exist, or it has an upstream (its commits
    live on the remote already).
    """
    branch_exists = succeeds(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch]
    )
    if not branch_exists:
        return 0
    has_upstream = succeeds(
        [
            "git",
            "-C",
            repo,
            "rev-parse",
            "--abbrev-ref",
            "--verify",
            "--quiet",
            f"{branch}@{{upstream}}",
        ]
    )
    if has_upstream:
        return 0
    return int(
        capture(["git", "-C", repo, "rev-list", "--count", f"{base}..{branch}"]).strip()
    )


def git_toplevel() -> str | None:
    """Git root of the cwd, or None when not inside a repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()
