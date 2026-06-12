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


def worktree_is_dirty(worktree: Path) -> bool:
    """True when the worktree has uncommitted changes (forgotten `git commit`)."""
    return bool(capture(["git", "-C", str(worktree), "status", "--porcelain"]).strip())


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


def branch_exists(repo: str, branch: str) -> bool:
    return succeeds(["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch])


def ensure_branch(repo: str, branch: str, target: str) -> None:
    """Create a missing branch at origin/<target>.

    `git worktree add -b` (what `sbx create --branch` does) would fork from
    whatever the main checkout happens to have checked out — basing explicitly
    keeps feature branches off each other's commits. Existing branches are
    reused as-is.
    """
    if branch_exists(repo, branch):
        return
    try:
        run(["git", "-C", repo, "fetch", "origin", target])
        base = f"origin/{target}"
    except HxError:
        base = target
    run(["git", "-C", repo, "branch", branch, base])


def unpushed_commit_count(repo: str, branch: str, base: str) -> int:
    """Commits on `branch` not on `base`, or 0 when deletion is safe.

    Safe means: the branch doesn't exist, or it has an upstream (its commits
    live on the remote already).
    """
    if not branch_exists(repo, branch):
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


SANDBOX_CLAUDE_MD = """\
## Sandbox workflow
You run inside a Docker sandbox on a dedicated git worktree (branch = sandbox name).
Work only inside this worktree, never in the main repo checkout next to it.
git commit works here. git push does NOT - the sandbox has no git credentials by design.
To push and open a merge request, ask the user to run on the host: hxmr <branch>
"""

WRITE_IF_MISSING_SCRIPT = """\
import pathlib, sys

path = pathlib.Path(sys.argv[1]).expanduser()
if not path.exists():
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sys.argv[2])
"""

MERGE_SETTINGS_SCRIPT = """\
import json, pathlib, sys

path = pathlib.Path.home() / ".claude" / "settings.json"
settings = json.loads(path.read_text()) if path.exists() else {}
settings.update(json.loads(sys.argv[1]))
path.write_text(json.dumps(settings, indent=2) + "\\n")
"""


def git_identity() -> tuple[str, str]:
    name = capture(["git", "config", "user.name"]).strip()
    email = capture(["git", "config", "user.email"]).strip()
    if not name or not email:
        raise HxError(
            "git user.name / user.email are unset — configure them first "
            "(git config --global user.name ...)"
        )
    return name, email


def install_plugins_command(name: str) -> list[str]:
    return [
        "sbx",
        "exec",
        name,
        "--",
        "sh",
        "-c",
        "claude plugin marketplace add anthropics/claude-plugins-official"
        " 2>/dev/null;"
        " claude plugin install superpowers@claude-plugins-official",
    ]


def provision_commands(
    name: str, git_user_name: str, git_user_email: str
) -> list[list[str]]:
    """Sandbox setup via exec AFTER `sbx create` — deliberately NOT an sbx kit.

    Kits break sbx's provisioning (sbx 0.30): any --kit skips the credential
    seeding (login prompt in every sandbox), and kit commands that run the
    claude CLI additionally get their plugin enablement clobbered by sbx's
    later settings write.
    """
    exec_prefix = ["sbx", "exec", name, "--"]
    return [
        # git identity (sbx forwarding is unreliable)
        [*exec_prefix, "git", "config", "--global", "user.name", git_user_name],
        [*exec_prefix, "git", "config", "--global", "user.email", git_user_email],
        # pre-commit binary (repo git hooks call it)
        [*exec_prefix, "uv", "tool", "install", "pre-commit"],
        # workflow notes for the agent
        [
            *exec_prefix,
            "python3",
            "-c",
            WRITE_IF_MISSING_SCRIPT,
            "~/.claude/CLAUDE.md",
            SANDBOX_CLAUDE_MD,
        ],
        # commits from the sandbox should carry only the user's identity
        [
            *exec_prefix,
            "python3",
            "-c",
            MERGE_SETTINGS_SCRIPT,
            '{"includeCoAuthoredBy": false}',
        ],
        install_plugins_command(name),
    ]


def main_repo_root() -> str | None:
    """Root of the main repository checkout for the cwd, or None outside a repo.

    Resolves through worktrees: the common git dir belongs to the main checkout,
    so per-project config matches no matter which worktree you run hx from.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return str(Path(result.stdout.strip()).parent)
