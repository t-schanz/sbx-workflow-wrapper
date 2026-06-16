# hx → `sbx --clone` Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the `hx` wrapper from the removed `sbx --branch` (host-worktree) model to the new `sbx --clone` (in-container clone) model in sbx v0.32, keeping the 1 sandbox : 1 branch : 1 feature UX.

**Architecture:** The feature branch now lives in a writable clone *inside* the container (at the same path as the host repo). hx creates the sandbox with `--clone`, materializes the clone with a cheap `sbx run -- --version`, checks out the feature branch via `sbx exec`, then attaches. `hx mr`/`hx rm` retrieve the agent's commits from the host via the `sandbox-<name>` git remote (`git fetch`), pushing to origin without ever creating a host branch or worktree.

**Tech Stack:** Python 3, Typer (CLI), pydantic (config), pytest, ruff, uv. Subprocess shells out to `sbx` and `git`.

**Reference spec:** `docs/superpowers/specs/2026-06-16-hx-sbx-clone-migration-design.md`

**Key facts (verified against sbx v0.32.0):**
- The in-container clone path **equals `config.repo`** (the workspace path passed to `sbx create` is mirrored inside the container as `$WORKSPACE_DIR`).
- The clone does not exist until the agent is launched; `sbx run <name> -- --version` materializes it cheaply (no API call, exits 0).
- `git fetch sandbox-<name>` on the host lands the clone's commits in `refs/sandboxes/<name>/<branch>` (these survive `sbx rm`).
- `hx mr`/`hx rm` require the sandbox to be running (the git-daemon serving the `sandbox-<name>` remote runs with it). This plan assumes the sandbox is running; failures surface as a clear `HxError` from the fetch.

---

## File Structure

- `src/hx/sandbox.py` — subprocess/git/sbx helpers. **Most changes here.** Remove worktree helpers; add clone/fetch helpers; rewrite `mr_push_command` and `unpushed_commit_count`; rewrite `SANDBOX_CLAUDE_MD`.
- `src/hx/cli.py` — Typer commands. Reorder `create`; rewire `mr` and `rm`.
- `src/hx/config.py` — only docstring/comment wording (`worktree` → `clone`). No behavior change.
- `tests/test_sandbox.py` — rewrite worktree-based tests around clone/fetch helpers.
- `tests/test_cli.py` — rewrite command-sequence expectations.
- `README.md` — rewrite model description and command sections.

---

## Task 1: Rewrite `SANDBOX_CLAUDE_MD` and remove worktree helpers from `sandbox.py`

**Files:**
- Modify: `src/hx/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Update the `SANDBOX_CLAUDE_MD` test**

In `tests/test_sandbox.py`, replace the body of `test_writes_claude_md_only_if_missing` (currently lines ~156-163) so it no longer asserts the word "worktree":

```python
    def test_writes_claude_md_only_if_missing(self):
        commands = sandbox.provision_commands("feat-x", "Jane Doe", "jane@example.com")
        write_command = next(c for c in commands if sandbox.SANDBOX_CLAUDE_MD in c)
        assert write_command[:5] == ["sbx", "exec", "feat-x", "--", "python3"]
        script = write_command[6]
        assert "exists" in script
        assert "Sandbox workflow" in sandbox.SANDBOX_CLAUDE_MD
        assert "hxmr <branch>" in sandbox.SANDBOX_CLAUDE_MD
        assert "clone" in sandbox.SANDBOX_CLAUDE_MD
        assert "worktree" not in sandbox.SANDBOX_CLAUDE_MD
```

- [ ] **Step 2: Delete the obsolete worktree/branch test classes**

In `tests/test_sandbox.py`, delete these whole classes (they test functions being removed): `TestParseWorktrees`, `TestFindWorktree`, `TestEnsureBranch`, `TestWorktreeIsDirty`. Also delete the module-level fixtures `PORCELAIN_TWO_WORKTREES` and `PORCELAIN_WITH_DETACHED` (lines ~20-42). Leave `TestSanitizeName`, `TestInstallPluginsCommand`, `TestProvisionCommands`, `TestGitIdentity` intact. (`TestMrPushCommand` and `TestUnpushedCommitCount` are rewritten in later tasks — leave them for now; they will fail until then.)

- [ ] **Step 3: Run the CLAUDE_MD test to verify it fails**

Run: `uv run pytest tests/test_sandbox.py::TestProvisionCommands::test_writes_claude_md_only_if_missing -v`
Expected: FAIL — current `SANDBOX_CLAUDE_MD` contains "worktree" and lacks "clone".

- [ ] **Step 4: Rewrite `SANDBOX_CLAUDE_MD` and remove worktree helpers**

In `src/hx/sandbox.py`, replace the `SANDBOX_CLAUDE_MD` constant (lines ~136-142) with:

```python
SANDBOX_CLAUDE_MD = """\
## Sandbox workflow
You work inside a Docker sandbox on a private clone of the repository, checked
out on a dedicated feature branch. Stay on that branch — do not switch or rename it.
git commit works here. git push does NOT - the sandbox has no git credentials by design.
To push and open a merge request, ask the user to run on the host: hxmr <branch>
"""
```

Then delete these four functions entirely from `src/hx/sandbox.py`: `parse_worktrees` (lines ~39-51), `find_worktree` (~54-61), `worktree_is_dirty` (~64-66), and `ensure_branch` (~91-106). (`mr_push_command` and `unpushed_commit_count` stay for now; rewritten in Tasks 3 and 4.)

- [ ] **Step 5: Run the CLAUDE_MD test to verify it passes**

Run: `uv run pytest tests/test_sandbox.py::TestProvisionCommands -v`
Expected: PASS (all `TestProvisionCommands` tests).

- [ ] **Step 6: Commit**

```bash
git add src/hx/sandbox.py tests/test_sandbox.py
git commit -m "Rewrite sandbox CLAUDE.md notes and drop worktree helpers"
```

---

## Task 2: Add clone helpers (`sandbox_remote`, materialize, branch checkout, copy)

**Files:**
- Modify: `src/hx/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sandbox.py`:

```python
class TestSandboxRemote:
    def test_prefixes_name(self):
        assert sandbox.sandbox_remote("feat-x") == "sandbox-feat-x"


class TestMaterializeCloneCommand:
    def test_runs_claude_version_via_sbx_run(self):
        assert sandbox.materialize_clone_command("feat-x") == [
            "sbx",
            "run",
            "feat-x",
            "--",
            "--version",
        ]


class TestBranchCheckoutCommand:
    def test_checks_out_branch_in_clone_basing_on_origin(self):
        command = sandbox.branch_checkout_command(
            "feat-x", "/repo", "feat/x", "main"
        )
        assert command[:6] == ["sbx", "exec", "feat-x", "--", "sh", "-c"]
        script = command[6]
        assert "cd '/repo'" in script
        assert "origin/feat/x" in script
        assert "origin/main" in script
        assert "git checkout -B 'feat/x'" in script


class TestCopyFileCommands:
    def test_makes_parent_then_copies_into_clone(self):
        commands = sandbox.copy_file_commands(
            "feat-x", "/repo", "build/openapi/openapi.json"
        )
        assert commands == [
            ["sbx", "exec", "feat-x", "--", "mkdir", "-p", "/repo/build/openapi"],
            [
                "sbx",
                "cp",
                "/repo/build/openapi/openapi.json",
                "feat-x:/repo/build/openapi/openapi.json",
            ],
        ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_sandbox.py::TestSandboxRemote tests/test_sandbox.py::TestMaterializeCloneCommand tests/test_sandbox.py::TestBranchCheckoutCommand tests/test_sandbox.py::TestCopyFileCommands -v`
Expected: FAIL with `AttributeError: module 'hx.sandbox' has no attribute 'sandbox_remote'` (etc.).

- [ ] **Step 3: Implement the helpers**

In `src/hx/sandbox.py`, add these functions (place them after `sanitize_name`, before `run` is fine — keep related helpers together; the `Path` import already exists at the top of the file):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_sandbox.py::TestSandboxRemote tests/test_sandbox.py::TestMaterializeCloneCommand tests/test_sandbox.py::TestBranchCheckoutCommand tests/test_sandbox.py::TestCopyFileCommands -v`
Expected: PASS (all four classes).

- [ ] **Step 5: Commit**

```bash
git add src/hx/sandbox.py tests/test_sandbox.py
git commit -m "Add clone materialize/branch-checkout/copy helpers"
```

---

## Task 3: Rewrite `mr_push_command` and add `clone_is_dirty`

**Files:**
- Modify: `src/hx/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Rewrite the `mr_push_command` test and add a `clone_is_dirty` test**

In `tests/test_sandbox.py`, replace the entire `TestMrPushCommand` class with:

```python
class TestMrPushCommand:
    def test_pushes_sandbox_ref_to_origin_with_mr_options(self):
        command = sandbox.mr_push_command("/repo", "feat-x", "feat/x", "main")
        assert command == [
            "git",
            "-C",
            "/repo",
            "push",
            "origin",
            "refs/sandboxes/feat-x/feat/x:refs/heads/feat/x",
            "-o",
            "merge_request.create",
            "-o",
            "merge_request.target=main",
            "-o",
            "merge_request.remove_source_branch",
        ]

    def test_custom_target(self):
        command = sandbox.mr_push_command("/repo", "feat-x", "feat/x", "develop")
        assert "merge_request.target=develop" in command


class TestCloneIsDirty:
    def test_dirty_when_status_has_output(self, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: " M ai/foo.py\n")
        assert sandbox.clone_is_dirty("feat-x", "/repo") is True

    def test_clean_when_status_is_empty(self, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: "")
        assert sandbox.clone_is_dirty("feat-x", "/repo") is False

    def test_queries_status_inside_the_clone(self, monkeypatch):
        seen = {}

        def capture(command):
            seen["command"] = command
            return ""

        monkeypatch.setattr(sandbox, "capture", capture)
        sandbox.clone_is_dirty("feat-x", "/repo")
        assert seen["command"] == [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "git",
            "-C",
            "/repo",
            "status",
            "--porcelain",
        ]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_sandbox.py::TestMrPushCommand tests/test_sandbox.py::TestCloneIsDirty -v`
Expected: FAIL — `mr_push_command` has the old 2-arg signature; `clone_is_dirty` does not exist.

- [ ] **Step 3: Implement**

In `src/hx/sandbox.py`, replace the existing `mr_push_command` function (old lines ~69-84) with:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_sandbox.py::TestMrPushCommand tests/test_sandbox.py::TestCloneIsDirty -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hx/sandbox.py tests/test_sandbox.py
git commit -m "Rewrite mr_push_command for sandbox refs; add fetch_sandbox and clone_is_dirty"
```

---

## Task 4: Rewrite `unpushed_commit_count` and add `remove_sandbox_remote`

**Files:**
- Modify: `src/hx/sandbox.py`
- Test: `tests/test_sandbox.py`

- [ ] **Step 1: Rewrite the unpushed-count tests and `FakeGit`**

In `tests/test_sandbox.py`, replace the `FakeGit` class, the `_patch_git` helper, and the `TestUnpushedCommitCount` class (old lines ~234-276) with:

```python
class FakeGit:
    """Simulates the git ref queries the rm guard makes against the sandbox ref."""

    def __init__(self, sandbox_ref_exists, origin_branch_exists, is_ancestor, count):
        self.sandbox_ref_exists = sandbox_ref_exists
        self.origin_branch_exists = origin_branch_exists
        self.is_ancestor = is_ancestor
        self.count = count

    def capture(self, command):
        if "rev-list" in command:
            return f"{self.count}\n"
        raise AssertionError(f"unexpected capture: {command}")

    def succeeds(self, command):
        if "merge-base" in command:
            return self.is_ancestor
        target = command[-1]
        if target.startswith("refs/sandboxes/"):
            return self.sandbox_ref_exists
        if target.startswith("origin/"):
            return self.origin_branch_exists
        raise AssertionError(f"unexpected probe: {command}")


def _patch_git(monkeypatch, fake):
    monkeypatch.setattr(sandbox, "capture", fake.capture)
    monkeypatch.setattr(sandbox, "succeeds", fake.succeeds)


class TestUnpushedCommitCount:
    def test_no_fetched_ref_is_safe(self, monkeypatch):
        _patch_git(monkeypatch, FakeGit(False, False, False, 5))
        assert sandbox.unpushed_commit_count("/repo", "feat-x", "feat/x", "main") == 0

    def test_already_pushed_branch_is_safe(self, monkeypatch):
        # origin/<branch> exists and the sandbox tip is an ancestor of it
        _patch_git(monkeypatch, FakeGit(True, True, True, 5))
        assert sandbox.unpushed_commit_count("/repo", "feat-x", "feat/x", "main") == 0

    def test_origin_branch_ahead_counts_against_origin_branch(self, monkeypatch):
        _patch_git(monkeypatch, FakeGit(True, True, False, 2))
        assert sandbox.unpushed_commit_count("/repo", "feat-x", "feat/x", "main") == 2

    def test_no_origin_branch_counts_against_target(self, monkeypatch):
        _patch_git(monkeypatch, FakeGit(True, False, False, 4))
        assert sandbox.unpushed_commit_count("/repo", "feat-x", "feat/x", "main") == 4


class TestRemoveSandboxRemote:
    def test_removes_remote_best_effort(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            sandbox, "run", lambda command, check=True: calls.append((command, check))
        )
        sandbox.remove_sandbox_remote("/repo", "feat-x")
        assert calls == [
            (["git", "-C", "/repo", "remote", "remove", "sandbox-feat-x"], False)
        ]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_sandbox.py::TestUnpushedCommitCount tests/test_sandbox.py::TestRemoveSandboxRemote -v`
Expected: FAIL — old `unpushed_commit_count` has signature `(repo, branch, base)` and queries `@{upstream}`; `remove_sandbox_remote` does not exist.

- [ ] **Step 3: Implement**

In `src/hx/sandbox.py`, replace the existing `branch_exists` and `unpushed_commit_count` functions (old lines ~87-133) with:

```python
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
        if succeeds(["git", "-C", repo, "merge-base", "--is-ancestor", ref, origin_branch]):
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
```

Note: `branch_exists` is removed (no remaining caller). Confirm nothing else references it (`grep -rn branch_exists src/`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: PASS for the whole `tests/test_sandbox.py` file (all sandbox-layer tests now green).

- [ ] **Step 5: Commit**

```bash
git add src/hx/sandbox.py tests/test_sandbox.py
git commit -m "Rewrite unpushed_commit_count for sandbox refs; add remove_sandbox_remote"
```

---

## Task 5: Rewrite `cli.create`

**Files:**
- Modify: `src/hx/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Rewrite the `TestCreate` fixtures and tests**

In `tests/test_cli.py`, replace the `repo` fixture (old lines ~10-28) with a clone-model fixture (no worktree, no `ensure_branch`):

```python
@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.setattr(
        config_module, "load_config", lambda: Config(repo=str(repo_path))
    )
    monkeypatch.setattr(
        sandbox, "git_identity", lambda: ("Jane Doe", "jane@example.com")
    )
    return repo_path
```

Then replace the entire `TestCreate` class with:

```python
class TestCreate:
    def test_invokes_exact_commands(self, repo, recorded_runs):
        repo_path = repo
        result = runner.invoke(cli.app, ["create", "feat/x", "--", "--gpu"])
        assert result.exit_code == 0
        assert recorded_runs == [
            ["git", "-C", str(repo_path), "fetch", "origin", "main"],
            [
                "sbx",
                "create",
                "--clone",
                "--name",
                "feat-x",
                "--cpus",
                "4",
                "--memory",
                "8g",
                "claude",
                str(repo_path),
                "--gpu",
            ],
            *sandbox.provision_commands("feat-x", "Jane Doe", "jane@example.com"),
            sandbox.materialize_clone_command("feat-x"),
            sandbox.branch_checkout_command("feat-x", str(repo_path), "feat/x", "main"),
            ["sbx", "run", "feat-x"],
        ]

    def test_host_fetch_runs_before_sbx_create(self, repo, recorded_runs):
        repo_path = repo
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        assert recorded_runs[0] == ["git", "-C", str(repo_path), "fetch", "origin", "main"]
        assert recorded_runs[1][:3] == ["sbx", "create", "--clone"]

    def test_failed_host_fetch_warns_but_continues(self, repo, monkeypatch):
        repo_path = repo
        calls = []

        def run(command, check=True):
            calls.append(command)
            if "fetch" in command and "origin" in command:
                raise cli.HxError("offline")

        monkeypatch.setattr(sandbox, "run", run)
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        assert "could not refresh" in result.output
        assert calls[-1] == ["sbx", "run", "feat-x"]

    def test_post_create_runs_inside_clone(self, repo, recorded_runs, monkeypatch):
        repo_path = repo
        configure(monkeypatch, repo_path, post_create="make gen-sdk")
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        assert recorded_runs[-2] == [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "sh",
            "-c",
            f"cd '{repo_path}' && make gen-sdk",
        ]
        assert recorded_runs[-1] == ["sbx", "run", "feat-x"]

    def test_copy_files_copied_into_clone(self, repo, recorded_runs, monkeypatch):
        repo_path = repo
        configure(monkeypatch, repo_path, copy_files=["build/openapi/openapi.json"])
        source = repo_path / "build" / "openapi" / "openapi.json"
        source.parent.mkdir(parents=True)
        source.write_text('{"openapi": "3.1.0"}')
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        for command in sandbox.copy_file_commands(
            "feat-x", str(repo_path), "build/openapi/openapi.json"
        ):
            assert command in recorded_runs

    def test_missing_copy_files_are_skipped(self, repo, recorded_runs, monkeypatch):
        repo_path = repo
        configure(monkeypatch, repo_path, copy_files=["does/not/exist.json"])
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        for command in sandbox.copy_file_commands(
            "feat-x", str(repo_path), "does/not/exist.json"
        ):
            assert command not in recorded_runs

    def test_post_create_failure_warns_but_continues(self, repo, monkeypatch):
        repo_path = repo
        configure(monkeypatch, repo_path, post_create="make gen-sdk")
        calls = []

        def run(command, check=True):
            calls.append(command)
            if command[-1].endswith("make gen-sdk"):
                raise cli.HxError("boom")

        monkeypatch.setattr(sandbox, "run", run)
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        assert "post-create setup failed" in result.output
        assert calls[-1] == ["sbx", "run", "feat-x"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py::TestCreate -v`
Expected: FAIL — `create` still passes `--branch`, calls `ensure_branch`/`find_worktree`, and copies on the host filesystem.

- [ ] **Step 3: Rewrite `create` in `src/hx/cli.py`**

Replace the entire `create` function body (old lines ~38-106) with (the decorators and signature on lines ~34-43 stay unchanged):

```python
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

    typer.echo("materializing the in-container clone...")
    sandbox.run(sandbox.materialize_clone_command(name))
    sandbox.run(sandbox.branch_checkout_command(name, config.repo, branch, config.target))

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

    sandbox.run(["sbx", "run", name], check=False)
```

Note: `shutil` is no longer used in `cli.py` (copies now go through `sbx cp`). Remove the `import shutil` line (old line ~4). `Path` is still used (`from pathlib import Path`) — keep it.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py::TestCreate -v`
Expected: PASS (all `TestCreate` tests).

- [ ] **Step 5: Commit**

```bash
git add src/hx/cli.py tests/test_cli.py
git commit -m "Rewrite hx create for the --clone model"
```

---

## Task 6: Rewrite `cli.mr`

**Files:**
- Modify: `src/hx/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Rewrite the `TestMr` class**

In `tests/test_cli.py`, replace the entire `TestMr` class with:

```python
class TestMr:
    @pytest.fixture(autouse=True)
    def clean_clone(self, monkeypatch):
        monkeypatch.setattr(sandbox, "clone_is_dirty", lambda name, repo: False)

    def test_fetches_then_pushes_with_default_target(self, repo, recorded_runs):
        repo_path = repo
        result = runner.invoke(cli.app, ["mr", "feat/x"])
        assert result.exit_code == 0
        assert recorded_runs == [
            ["git", "-C", str(repo_path), "fetch", "sandbox-feat-x"],
            sandbox.mr_push_command(str(repo_path), "feat-x", "feat/x", "main"),
        ]

    def test_pushes_with_explicit_target(self, repo, recorded_runs):
        repo_path = repo
        result = runner.invoke(cli.app, ["mr", "feat/x", "develop"])
        assert result.exit_code == 0
        assert recorded_runs[-1] == sandbox.mr_push_command(
            str(repo_path), "feat-x", "feat/x", "develop"
        )

    def test_dirty_clone_prompts_and_declined_aborts(
        self, repo, recorded_runs, monkeypatch
    ):
        monkeypatch.setattr(sandbox, "clone_is_dirty", lambda name, repo: True)
        result = runner.invoke(cli.app, ["mr", "feat/x"], input="n\n")
        assert result.exit_code == 1
        assert "uncommitted changes" in result.output
        assert recorded_runs == []

    def test_dirty_clone_prompts_and_accepted_pushes(
        self, repo, recorded_runs, monkeypatch
    ):
        repo_path = repo
        monkeypatch.setattr(sandbox, "clone_is_dirty", lambda name, repo: True)
        result = runner.invoke(cli.app, ["mr", "feat/x"], input="y\n")
        assert result.exit_code == 0
        assert recorded_runs[-1] == sandbox.mr_push_command(
            str(repo_path), "feat-x", "feat/x", "main"
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py::TestMr -v`
Expected: FAIL — `mr` still calls `find_worktree`/`worktree_is_dirty` and the old `mr_push_command`.

- [ ] **Step 3: Rewrite `mr` in `src/hx/cli.py`**

Replace the entire `mr` function (old lines ~109-127) with (keep the `@app.command()` and `@handle_errors` decorators above it):

```python
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
    sandbox.run(sandbox.mr_push_command(config.repo, name, branch, target or config.target))
```

Wait — ordering: the dirty check must run before the fetch in the implementation? The test `test_fetches_then_pushes_with_default_target` expects `recorded_runs` to be exactly `[fetch, push]` with the dirty check mocked (not a `sandbox.run`, so it never appears in `recorded_runs`). The dirty check uses `sandbox.capture` via `clone_is_dirty`, not `sandbox.run`, so its position relative to the fetch does not affect `recorded_runs`. Keep the dirty check first (fail fast before fetching). The two recorded runs are fetch then push — matches.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py::TestMr -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hx/cli.py tests/test_cli.py
git commit -m "Rewrite hx mr to fetch sandbox commits and push from the host"
```

---

## Task 7: Rewrite `cli.rm`

**Files:**
- Modify: `src/hx/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Rewrite the `TestRm` class**

In `tests/test_cli.py`, replace the entire `TestRm` class with:

```python
class TestRm:
    def test_safe_branch_removed_without_prompt(self, repo, recorded_runs, monkeypatch):
        repo_path = repo
        monkeypatch.setattr(
            sandbox, "unpushed_commit_count", lambda repo, name, branch, target: 0
        )
        result = runner.invoke(cli.app, ["rm", "feat/x"])
        assert result.exit_code == 0
        assert recorded_runs == [
            ["git", "-C", str(repo_path), "fetch", "sandbox-feat-x"],
            ["sbx", "rm", "--force", "feat-x"],
            ["git", "-C", str(repo_path), "remote", "remove", "sandbox-feat-x"],
        ]

    def test_unpushed_commits_prompt_declined_aborts(
        self, repo, recorded_runs, monkeypatch
    ):
        monkeypatch.setattr(
            sandbox, "unpushed_commit_count", lambda repo, name, branch, target: 3
        )
        result = runner.invoke(cli.app, ["rm", "feat/x"], input="n\n")
        assert result.exit_code == 1
        # the preserving fetch still ran, but nothing was removed
        assert ["sbx", "rm", "--force", "feat-x"] not in recorded_runs

    def test_unpushed_commits_prompt_accepted_removes(
        self, repo, recorded_runs, monkeypatch
    ):
        repo_path = repo
        monkeypatch.setattr(
            sandbox, "unpushed_commit_count", lambda repo, name, branch, target: 3
        )
        result = runner.invoke(cli.app, ["rm", "feat/x"], input="y\n")
        assert result.exit_code == 0
        assert "3 unpushed commit(s)" in result.output
        assert recorded_runs[-2:] == [
            ["sbx", "rm", "--force", "feat-x"],
            ["git", "-C", str(repo_path), "remote", "remove", "sandbox-feat-x"],
        ]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py::TestRm -v`
Expected: FAIL — `rm` uses the old `unpushed_commit_count(repo, branch, base)` signature and does not fetch or remove the remote.

- [ ] **Step 3: Rewrite `rm` in `src/hx/cli.py`**

Replace the entire `rm` function (old lines ~130-144) with (keep the `@app.command()` and `@handle_errors` decorators):

```python
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
```

Note: `remove_sandbox_remote` calls `sandbox.run(..., check=False)`, so it is recorded by the `recorded_runs` fixture as the host `git remote remove` command — matching the test.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py::TestRm -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hx/cli.py tests/test_cli.py
git commit -m "Rewrite hx rm to fetch-preserve commits and clean up the sandbox remote"
```

---

## Task 8: Tidy `config.py` and `pyproject.toml` wording

**Files:**
- Modify: `src/hx/config.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update the two field comments**

In `src/hx/config.py`, change the `copy_files` and `post_create` comments (lines ~22-25) to reflect the clone model:

```python
    # Repo-relative paths copied into the in-container clone (e.g. cached build artifacts).
    copy_files: list[str] = []
    # Shell command run inside the sandbox at the clone root after create.
    post_create: str | None = None
```

- [ ] **Step 2: Update the project description in `pyproject.toml`**

Change the `description` line (currently "Sandbox workflow CLI: one Docker sandbox + git worktree per feature branch") to:

```toml
description = "Sandbox workflow CLI: one Docker sandbox (sbx --clone) per feature branch"
```

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all tests).

- [ ] **Step 4: Commit**

```bash
git add src/hx/config.py pyproject.toml
git commit -m "Update config and project wording for the clone model"
```

---

## Task 9: Rewrite `README.md`

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite the model intro (lines ~1-8)**

Replace the opening paragraph with:

```markdown
# hx — sandbox workflow CLI

`hx` manages a per-feature development workflow using
[Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/): one sandbox per
feature branch, each running Claude Code on a **private in-container clone** of your
repo (`sbx --clone`). The host repo is mounted read-only; the agent commits inside the
clone. The sandbox deliberately has **no git credentials** — `git commit` works inside,
`git push` does not. Pushing (and creating the GitLab merge request) happens on the host
via `hx mr`, which fetches the sandbox's commits through the `sandbox-<name>` git remote
that `sbx` wires up.
```

- [ ] **Step 2: Update the "Project-specific workflows" bullets (lines ~50-57)**

Replace the `copy_files` and `post_create` bullet descriptions with:

```markdown
- **`copy_files`** — repo-relative paths copied from the host checkout into the
  in-container clone via `sbx cp` (parent directories are created, missing files
  skipped silently). Use it to prime caches or build artifacts that are expensive to
  regenerate.
- **`post_create`** — a shell command run *inside the sandbox* after the clone is
  created, via a throwaway `sh -c` starting at the clone root. If it fails, `hx create`
  warns and still attaches so you can finish setup manually. For anything beyond a
  one-liner, point it at a script in your repo.
```

Also change "Copying the host's cached spec into the worktree" (line ~60) to "Copying the host's cached spec into the clone".

- [ ] **Step 3: Rewrite the `hx create` section (lines ~74-86)**

Replace the description paragraph with:

```markdown
### `hx create BRANCH [EXTRA_SBX_FLAGS...]`

Creates a `--clone` sandbox for `BRANCH`. First refreshes `origin/<target>` on the host
so the clone's base is current (the clone has no remote credentials of its own). Then
creates the sandbox, provisions it (git identity from the host, pre-commit, the
superpowers claude plugin, workflow notes in the agent's CLAUDE.md), materializes the
in-container clone with a cheap launch, checks out `BRANCH` inside it (based on
`origin/<target>`, or `origin/BRANCH` when resuming an existing branch), copies any
configured `copy_files` into the clone, runs the configured `post_create` command, then
attaches interactively.
```

(Leave the "Provisioning deliberately happens via `sbx exec`…" paragraph and the two example commands that follow unchanged.)

- [ ] **Step 4: Rewrite the `hx mr` and `hx rm` sections (lines ~93-110)**

Replace those two sections with:

```markdown
### `hx mr BRANCH [TARGET]` (alias: `hxmr`)

Fetches the sandbox's commits to the host (via the `sandbox-<name>` remote) and pushes
`BRANCH` to `origin`, auto-creating a GitLab merge request via push options. Prompts if
the clone still has uncommitted changes. `TARGET` defaults to `main` (configurable).

```sh
hxmr feat/PROJ-123-shiny-thing
hxmr feat/PROJ-123-shiny-thing develop
```

### `hx rm BRANCH` (alias: `hxrm`)

Fetches the sandbox's commits first (mirrored into `refs/sandboxes/<name>/*`, which
survive removal), then removes the sandbox and drops the host-side `sandbox-<name>`
remote. If the branch has unpushed commits, you are prompted before anything is removed.

```sh
hxrm feat/PROJ-123-shiny-thing
```
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Rewrite README for the sbx --clone workflow"
```

---

## Task 10: Full verification and manual smoke test

**Files:** none (verification only)

- [ ] **Step 1: Lint and full test suite**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`
Expected: ruff reports no issues; all tests pass. Fix any lint/format issues (e.g. line length) and re-run.

- [ ] **Step 2: Confirm no stale references remain**

Run: `grep -rniE "worktree|--branch|ensure_branch|find_worktree|parse_worktrees" src/ README.md`
Expected: no matches (an empty result). If any appear, they are leftovers — fix them.

- [ ] **Step 3: Manual end-to-end smoke test (real sbx, throwaway repo)**

This exercises the real `sbx`/`git` round trip the unit tests mock. Run from a scratch repo so nothing real is touched:

```bash
# scratch git repo with a remote-less origin won't allow the final push,
# so just exercise create + the fetch round trip here.
tmp=$(mktemp -d); cd "$tmp"; git init -q -b main
git config user.email t@e.com; git config user.name t
echo hi > file.txt; git add -A; git commit -qm init
# point hx at this repo for the test
XDG_CONFIG_HOME="$tmp/cfg" mkdir -p "$tmp/cfg/hx"
printf 'repo = "%s"\ncpus = 2\nmemory = "2g"\n' "$tmp" > "$tmp/cfg/hx/config.toml"
# run create from inside the repo (main_repo_root resolves cwd)
XDG_CONFIG_HOME="$tmp/cfg" uv run hx create feat/smoke -- --quiet
```

Expected: the command creates a sandbox `feat-smoke`, provisions it, materializes the clone, checks out `feat/smoke`, and attaches Claude. Inside the agent (or via a second terminal), confirm `git -C "$tmp" branch` shows `feat/smoke` is the current branch in the clone, then exit.

- [ ] **Step 4: Verify the fetch + unpushed accounting**

After making a commit inside the sandbox (`sbx exec feat-smoke -- sh -c "cd $tmp && git commit --allow-empty -qm work"`):

```bash
git -C "$tmp" fetch sandbox-feat-smoke
git -C "$tmp" for-each-ref | grep feat-smoke   # expect refs/sandboxes/feat-smoke/feat/smoke
XDG_CONFIG_HOME="$tmp/cfg" uv run hx rm feat/smoke   # should report the unpushed commit and prompt
```

Expected: `hx rm` reports `1 unpushed commit(s)` and prompts; answering `y` removes the sandbox and the `sandbox-feat-smoke` remote, while `refs/sandboxes/feat-smoke/*` remain.

- [ ] **Step 5: Clean up the smoke test**

```bash
sbx rm --force feat-smoke 2>/dev/null
# remove the throwaway repo dir reported as $tmp
```

- [ ] **Step 6: Final commit (if Step 1 required fixes)**

```bash
git add -A
git commit -m "Fix lint/format after clone migration"
```

---

## Self-Review notes (already applied)

- **Spec coverage:** create reorder (Task 5), `--clone` create (Task 5), host fetch-first (Task 5), materialize trigger (Tasks 2/5), in-clone branch checkout (Tasks 2/5), `sbx cp` copy_files (Tasks 2/5), in-clone post_create (Task 5), mr fetch+push (Tasks 3/6), rm fetch-preserve + remote cleanup + keep refs (Tasks 4/7), CLAUDE.md rewrite (Task 1), README rewrite (Task 9), config wording (Task 8). All spec sections map to a task.
- **Removed symbols** (`parse_worktrees`, `find_worktree`, `worktree_is_dirty`, `ensure_branch`, `branch_exists`) have their tests deleted in the same task that removes them (Tasks 1, 4).
- **Signature consistency:** `mr_push_command(repo, name, branch, target)`, `unpushed_commit_count(repo, name, branch, target)`, `clone_is_dirty(name, repo)`, `fetch_sandbox(repo, name, check=True)`, `copy_file_commands(name, repo, relative_path)`, `branch_checkout_command(name, repo, branch, target)`, `materialize_clone_command(name)`, `remove_sandbox_remote(repo, name)` — used consistently across `cli.py` and tests.
