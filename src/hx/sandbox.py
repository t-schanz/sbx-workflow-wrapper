"""Thin subprocess layer for sbx/git, name sanitizing, clone/branch helpers."""

import subprocess
from pathlib import Path

from hx import HxError


def sanitize_name(branch: str) -> str:
    """Sandbox names must not contain '/'."""
    return branch.replace("/", "-")


def sandbox_remote(name: str) -> str:
    """Host-side git remote sbx wires to the sandbox's in-container clone."""
    return f"sandbox-{name}"


def materialize_clone_command(name: str) -> list[str]:
    """Cheaply create the in-container clone (claude prints its version, no API call).

    The writable clone does not exist until the agent is launched once; this is the
    cheapest launch that creates it and exits.
    """
    return ["sbx", "run", name, "--", "--version"]


def branch_checkout_command(
    name: str, repo: str, branch: str, target: str
) -> list[str]:
    """Check the feature branch out inside the clone (cwd = the mirrored repo path).

    Bases on origin/<branch> when it already exists on the host (resume), else on
    origin/<target>. `origin` in the clone is the read-only host source.
    """
    script = (
        f"cd '{repo}' && "
        f"if git rev-parse --verify --quiet 'origin/{branch}' >/dev/null; "
        f"then base='origin/{branch}'; else base='origin/{target}'; fi && "
        f"git checkout -B '{branch}' \"$base\""
    )
    return ["sbx", "exec", name, "--", "sh", "-c", script]


def copy_file_commands(name: str, repo: str, relative_path: str) -> list[list[str]]:
    """Copy a host file into the clone: create its parent dir, then `sbx cp` it in.

    The clone mirrors the host repo path, so the destination is repo/relative_path
    inside the sandbox. The parent may not exist in the clone (e.g. gitignored build
    artifacts), so it is created first.
    """
    destination = Path(repo) / relative_path
    return [
        ["sbx", "exec", name, "--", "mkdir", "-p", str(destination.parent)],
        ["sbx", "cp", str(destination), f"{name}:{destination}"],
    ]


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


def mr_push_command(repo: str, name: str, branch: str, target: str) -> list[str]:
    """Push the sandbox's fetched branch ref straight to origin and open an MR.

    The branch lives only in refs/sandboxes/<name>/<branch> on the host (fetched
    from the sandbox); pushing the ref directly avoids creating a host branch.
    """
    return [
        "git",
        "-C",
        repo,
        "push",
        "origin",
        f"refs/sandboxes/{name}/{branch}:refs/heads/{branch}",
        "-o",
        "merge_request.create",
        "-o",
        f"merge_request.target={target}",
        "-o",
        "merge_request.remove_source_branch",
    ]


def fetch_sandbox(repo: str, name: str, check: bool = True) -> None:
    """Fetch the sandbox's commits into refs/sandboxes/<name>/* on the host."""
    run(["git", "-C", repo, "fetch", sandbox_remote(name)], check=check)


def clone_is_dirty(name: str, repo: str) -> bool:
    """True when the in-container clone has uncommitted changes (forgotten commit)."""
    status = capture(
        ["sbx", "exec", name, "--", "git", "-C", repo, "status", "--porcelain"]
    )
    return bool(status.strip())


def unpushed_commit_count(repo: str, name: str, branch: str, target: str) -> int:
    """Commits in the sandbox's branch not yet on origin, or 0 when removal is safe.

    Safe (0) means: nothing was fetched for this branch, or the sandbox tip is
    already an ancestor of origin/<branch> (pushed via `hx mr`). Otherwise counts
    commits the sandbox has beyond origin/<branch> (or origin/<target> if the branch
    was never pushed). Call `fetch_sandbox` first so the ref is up to date.
    """
    ref = f"refs/sandboxes/{name}/{branch}"
    if not succeeds(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref]):
        return 0
    origin_branch = f"origin/{branch}"
    if succeeds(["git", "-C", repo, "rev-parse", "--verify", "--quiet", origin_branch]):
        if succeeds(
            ["git", "-C", repo, "merge-base", "--is-ancestor", ref, origin_branch]
        ):
            return 0
        base = origin_branch
    else:
        base = f"origin/{target}"
    return int(
        capture(["git", "-C", repo, "rev-list", "--count", f"{base}..{ref}"]).strip()
    )


def remove_sandbox_remote(repo: str, name: str) -> None:
    """Drop the host-side sandbox-<name> remote (best effort; ignores absence)."""
    run(["git", "-C", repo, "remote", "remove", sandbox_remote(name)], check=False)


SANDBOX_CLAUDE_MD = """\
## Sandbox workflow
You work inside a Docker sandbox on a private clone of the repository, checked
out on a dedicated feature branch. Stay on that branch — do not switch or rename it.
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
