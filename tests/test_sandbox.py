from pathlib import Path

import pytest

from hx import HxError
from hx import sandbox


class TestSanitizeName:
    def test_replaces_every_slash_with_dash(self):
        assert sandbox.sanitize_name("feat/DIGREM-123-x") == "feat-DIGREM-123-x"

    def test_handles_multiple_slashes(self):
        assert sandbox.sanitize_name("a/b/c") == "a-b-c"

    def test_leaves_plain_names_alone(self):
        assert sandbox.sanitize_name("main") == "main"


PORCELAIN_TWO_WORKTREES = """\
worktree /home/user/repo
HEAD 1111111111111111111111111111111111111111
branch refs/heads/main

worktree /home/user/repo/.sbx/feat-x-worktrees/feat/x
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feat/x
"""

PORCELAIN_WITH_DETACHED = """\
worktree /home/user/repo
HEAD 1111111111111111111111111111111111111111
branch refs/heads/main

worktree /home/user/repo/.sbx/detached
HEAD 3333333333333333333333333333333333333333
detached

worktree /home/user/repo/.sbx/feat-y-worktrees/feat/y
HEAD 4444444444444444444444444444444444444444
branch refs/heads/feat/y
"""


class TestParseWorktrees:
    def test_finds_matching_branch(self):
        worktrees = sandbox.parse_worktrees(PORCELAIN_TWO_WORKTREES)
        assert worktrees["feat/x"] == Path(
            "/home/user/repo/.sbx/feat-x-worktrees/feat/x"
        )

    def test_branch_not_found(self):
        worktrees = sandbox.parse_worktrees(PORCELAIN_TWO_WORKTREES)
        assert "feat/missing" not in worktrees

    def test_multiple_worktrees_all_parsed(self):
        worktrees = sandbox.parse_worktrees(PORCELAIN_TWO_WORKTREES)
        assert set(worktrees) == {"main", "feat/x"}

    def test_detached_head_blocks_skipped(self):
        worktrees = sandbox.parse_worktrees(PORCELAIN_WITH_DETACHED)
        assert set(worktrees) == {"main", "feat/y"}


class TestFindWorktree:
    def test_returns_path_for_branch(self, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: PORCELAIN_TWO_WORKTREES)
        path = sandbox.find_worktree("/home/user/repo", "feat/x")
        assert path == Path("/home/user/repo/.sbx/feat-x-worktrees/feat/x")

    def test_raises_for_missing_branch(self, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: PORCELAIN_TWO_WORKTREES)
        with pytest.raises(HxError, match="feat/missing"):
            sandbox.find_worktree("/home/user/repo", "feat/missing")


class TestMrPushCommand:
    def test_builds_exact_push_command(self):
        command = sandbox.mr_push_command(Path("/wt"), "main")
        assert command == [
            "git",
            "-C",
            "/wt",
            "push",
            "-u",
            "origin",
            "HEAD",
            "-o",
            "merge_request.create",
            "-o",
            "merge_request.target=main",
            "-o",
            "merge_request.remove_source_branch",
        ]

    def test_custom_target(self):
        command = sandbox.mr_push_command(Path("/wt"), "develop")
        assert "merge_request.target=develop" in command


class TestInstallPluginsCommand:
    def test_installs_superpowers_via_exec_after_create(self):
        command = sandbox.install_plugins_command("feat-x")
        assert command == [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "sh",
            "-c",
            "claude plugin marketplace add anthropics/claude-plugins-official"
            " 2>/dev/null;"
            " claude plugin install superpowers@claude-plugins-official",
        ]


class TestProvisionCommands:
    def test_sets_git_identity_via_exec(self):
        commands = sandbox.provision_commands("feat-x", "Jane Doe", "jane@example.com")
        assert [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "git",
            "config",
            "--global",
            "user.name",
            "Jane Doe",
        ] in commands
        assert [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "git",
            "config",
            "--global",
            "user.email",
            "jane@example.com",
        ] in commands

    def test_installs_pre_commit(self):
        commands = sandbox.provision_commands("feat-x", "Jane Doe", "jane@example.com")
        assert [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "uv",
            "tool",
            "install",
            "pre-commit",
        ] in commands

    def test_writes_claude_md_only_if_missing(self):
        commands = sandbox.provision_commands("feat-x", "Jane Doe", "jane@example.com")
        write_command = next(c for c in commands if sandbox.SANDBOX_CLAUDE_MD in c)
        assert write_command[:5] == ["sbx", "exec", "feat-x", "--", "python3"]
        script = write_command[6]
        assert "exists" in script
        assert "Sandbox workflow" in sandbox.SANDBOX_CLAUDE_MD
        assert "hxmr <branch>" in sandbox.SANDBOX_CLAUDE_MD

    def test_ends_with_plugin_install(self):
        commands = sandbox.provision_commands("feat-x", "Jane Doe", "jane@example.com")
        assert commands[-1] == sandbox.install_plugins_command("feat-x")

    def test_disables_co_authored_by_in_agent_settings(self):
        commands = sandbox.provision_commands("feat-x", "Jane Doe", "jane@example.com")
        merge_command = next(
            c for c in commands if '{"includeCoAuthoredBy": false}' in c
        )
        assert merge_command[:5] == ["sbx", "exec", "feat-x", "--", "python3"]
        script = merge_command[6]
        assert "settings.json" in script
        assert "update" in script


class TestEnsureBranch:
    def _record(self, monkeypatch, branch_exists, fetch_fails=False):
        calls = []

        def run(command, check=True):
            calls.append(command)
            if fetch_fails and "fetch" in command:
                raise HxError("offline")

        monkeypatch.setattr(sandbox, "run", run)
        monkeypatch.setattr(sandbox, "succeeds", lambda command: branch_exists)
        return calls

    def test_existing_branch_is_left_alone(self, monkeypatch):
        calls = self._record(monkeypatch, branch_exists=True)
        sandbox.ensure_branch("/repo", "feat/x", "main")
        assert calls == []

    def test_missing_branch_is_created_from_origin_target(self, monkeypatch):
        calls = self._record(monkeypatch, branch_exists=False)
        sandbox.ensure_branch("/repo", "feat/x", "main")
        assert calls == [
            ["git", "-C", "/repo", "fetch", "origin", "main"],
            ["git", "-C", "/repo", "branch", "feat/x", "origin/main"],
        ]

    def test_fetch_failure_falls_back_to_local_target(self, monkeypatch):
        calls = self._record(monkeypatch, branch_exists=False, fetch_fails=True)
        sandbox.ensure_branch("/repo", "feat/x", "main")
        assert calls[-1] == ["git", "-C", "/repo", "branch", "feat/x", "main"]


class TestWorktreeIsDirty:
    def test_dirty_when_status_has_output(self, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: " M ai/foo.py\n")
        assert sandbox.worktree_is_dirty(Path("/wt")) is True

    def test_clean_when_status_is_empty(self, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: "")
        assert sandbox.worktree_is_dirty(Path("/wt")) is False


class TestGitIdentity:
    def test_reads_host_git_config(self, monkeypatch):
        values = {"user.name": "Jane Doe\n", "user.email": "jane@example.com\n"}
        monkeypatch.setattr(sandbox, "capture", lambda command: values[command[-1]])
        assert sandbox.git_identity() == ("Jane Doe", "jane@example.com")

    def test_unset_identity_is_an_error(self, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: "")
        with pytest.raises(HxError, match="user.name"):
            sandbox.git_identity()


class FakeGit:
    """Simulates the three git queries the rm guard makes."""

    def __init__(self, branch_exists, has_upstream, unpushed_count):
        self.branch_exists = branch_exists
        self.has_upstream = has_upstream
        self.unpushed_count = unpushed_count

    def capture(self, command):
        if "rev-list" in command:
            return f"{self.unpushed_count}\n"
        raise AssertionError(f"unexpected capture: {command}")

    def succeeds(self, command):
        if "@{upstream}" in command[-1]:
            return self.has_upstream
        if "--verify" in command:
            return self.branch_exists
        raise AssertionError(f"unexpected probe: {command}")


def _patch_git(monkeypatch, fake):
    monkeypatch.setattr(sandbox, "capture", fake.capture)
    monkeypatch.setattr(sandbox, "succeeds", fake.succeeds)


class TestUnpushedCommitCount:
    def test_branch_missing_is_safe(self, monkeypatch):
        _patch_git(monkeypatch, FakeGit(False, False, 5))
        assert sandbox.unpushed_commit_count("/repo", "feat/x", "main") == 0

    def test_branch_with_upstream_is_safe(self, monkeypatch):
        _patch_git(monkeypatch, FakeGit(True, True, 5))
        assert sandbox.unpushed_commit_count("/repo", "feat/x", "main") == 0

    def test_no_upstream_zero_commits(self, monkeypatch):
        _patch_git(monkeypatch, FakeGit(True, False, 0))
        assert sandbox.unpushed_commit_count("/repo", "feat/x", "main") == 0

    def test_no_upstream_with_commits(self, monkeypatch):
        _patch_git(monkeypatch, FakeGit(True, False, 3))
        assert sandbox.unpushed_commit_count("/repo", "feat/x", "main") == 3
