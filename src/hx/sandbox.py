"""Thin subprocess layer for sbx/git, name sanitizing, clone/branch helpers."""

import json
import subprocess
import tempfile
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
    return ["sbx", "run", "--name", name, "--", "--version"]


def branch_checkout_command(
    name: str, repo: str, branch: str, target: str
) -> list[str]:
    """Check the feature branch out inside the clone (cwd = the mirrored repo path).

    Bases on origin/<branch> when it already exists on the host (resume), else on
    origin/<target>. `origin` in the clone is the read-only host source. The repo,
    branch, and target are passed as positional args (not interpolated) so values
    containing shell metacharacters (e.g. an apostrophe in a branch name) are safe.
    """
    script = (
        'cd "$1" && '
        'if git rev-parse --verify --quiet "origin/$2" >/dev/null; '
        'then base="origin/$2"; else base="origin/$3"; fi && '
        'git checkout -B "$2" "$base"'
    )
    return ["sbx", "exec", name, "--", "sh", "-c", script, "sh", repo, branch, target]


def headless_agent_command(name: str, repo: str, prompt_file: Path) -> list[str]:
    """Run the agent unattended on the prompt file, starting in the clone's repo root.

    `auto` lets the classifier judge the shell commands that
    allow_unattended_tools_command did not pre-clear; see there for why bypass mode is
    not used. Repo path and prompt travel as positional args so neither is interpreted
    by the shell.
    """
    script = 'cd "$1" && claude -p "$2" --permission-mode auto'
    return [
        "sbx",
        "exec",
        name,
        "--",
        "sh",
        "-c",
        script,
        "sh",
        repo,
        prompt_file.read_text(),
    ]


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


UNATTENDED_TOOLS = (
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "Skill",
    "mcp__telecontext",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
)

ALLOW_TOOLS_SCRIPT = """\
import json, pathlib, sys

path = pathlib.Path.home() / ".claude" / "settings.json"
settings = json.loads(path.read_text()) if path.exists() else {}
allow = settings.setdefault("permissions", {}).setdefault("allow", [])
allow.extend(tool for tool in sys.argv[1:] if tool not in allow)
path.write_text(json.dumps(settings, indent=2) + "\\n")
"""


def allow_unattended_tools_command(
    name: str, bash_patterns: tuple[str, ...] | list[str] = ()
) -> list[str]:
    """Pre-clear the tools an unattended agent needs, so nothing waits for a human.

    Bypass mode is not an option: the organization's managed settings
    (~/.claude/remote-settings.json) set `disableBypassPermissionsMode`, so both
    `--permission-mode bypassPermissions` and `--dangerously-skip-permissions` leave the
    agent waiting for an approval that nobody can give.

    Pre-cleared are the file and search tools, whose damage a throwaway clone on its own
    branch absorbs, plus the git commands the agent needs to commit and the project's own
    build and test commands (`allow_bash`). Every other shell command is left to the
    `auto` permission mode the agent runs in, and the managed deny rules (reading
    secrets, sudo, rm -rf, force push) apply throughout, because deny wins over allow.
    """
    return [
        "sbx",
        "exec",
        name,
        "--",
        "python3",
        "-c",
        ALLOW_TOOLS_SCRIPT,
        *UNATTENDED_TOOLS,
        *(f"Bash({pattern})" for pattern in bash_patterns),
    ]


def git_identity() -> tuple[str, str]:
    name = capture(["git", "config", "user.name"]).strip()
    email = capture(["git", "config", "user.email"]).strip()
    if not name or not email:
        raise HxError(
            "git user.name / user.email are unset — configure them first "
            "(git config --global user.name ...)"
        )
    return name, email


MARKETPLACES = ("anthropics/claude-plugins-official", "DietrichGebert/ponytail")

PLUGINS = (
    "superpowers@claude-plugins-official",
    "mattpocock-skills@claude-plugins-official",
    "ponytail@ponytail",
)


def install_plugins_command(name: str) -> list[str]:
    """Add the marketplaces, then install the plugins the host session uses.

    Runs after create, not at image build time: sbx rewrites
    ~/.claude/settings.json when the sandbox is created, which would drop the
    enabledPlugins entry an earlier install had written.
    """
    script = "; ".join(
        [
            *(
                f"claude plugin marketplace add {marketplace} 2>/dev/null"
                for marketplace in MARKETPLACES
            ),
            *(f"claude plugin install {plugin}" for plugin in PLUGINS),
        ]
    )
    return ["sbx", "exec", name, "--", "sh", "-c", script]


MERGE_JSON_SCRIPT = """\
import json, pathlib, sys

target, key, source = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
data = json.loads(target.read_text()) if target.exists() else {}
data.setdefault(key, {}).update(json.loads(source.read_text()))
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(data, indent=2) + "\\n")
source.unlink()
"""

HOST_CLAUDE_CONFIG = Path.home() / ".claude.json"
HOST_CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

MCP_TARGETS = (
    # (host file, key to copy, file inside the sandbox)
    (HOST_CLAUDE_CONFIG, "mcpServers", "/home/agent/.claude.json"),
    (HOST_CLAUDE_CREDENTIALS, "mcpOAuth", "/home/agent/.claude/.credentials.json"),
)


def host_json_key(path: Path, key: str) -> dict:
    """Read one top-level object from a host JSON file, {} when absent."""
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get(key) or {}


def merge_json_commands(
    name: str, payload: Path, key: str, target: str
) -> list[list[str]]:
    """Copy a JSON fragment into the sandbox and merge it under `key` in `target`.

    The fragment travels as a file rather than an argv value because it can hold
    OAuth tokens, which would otherwise show up in the process list. The merge
    script deletes it once applied.
    """
    staged = f"/tmp/{payload.name}"
    return [
        ["sbx", "cp", str(payload), f"{name}:{staged}"],
        [
            "sbx",
            "exec",
            name,
            "--",
            "python3",
            "-c",
            MERGE_JSON_SCRIPT,
            target,
            key,
            staged,
        ],
    ]


def provision_mcp(name: str) -> None:
    """Replicate the host's user-scope MCP servers and their OAuth tokens.

    Sandboxes deliberately ignore user-level host config (~/.claude), so an
    http/sse server configured on the host is invisible inside and its OAuth
    login would have to be repeated per sandbox. Copying both halves keeps the
    agent's tool set identical without a manual /mcp login.
    """
    with tempfile.TemporaryDirectory() as staging:
        for index, (host_path, key, target) in enumerate(MCP_TARGETS):
            fragment = host_json_key(host_path, key)
            if not fragment:
                continue
            payload = Path(staging) / f"hx-mcp-{index}.json"
            payload.write_text(json.dumps(fragment))
            payload.chmod(0o600)
            for command in merge_json_commands(name, payload, key, target):
                run(command)


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
