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
        assert "clone" in sandbox.SANDBOX_CLAUDE_MD
        assert "worktree" not in sandbox.SANDBOX_CLAUDE_MD

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
        command = sandbox.branch_checkout_command("feat-x", "/repo", "feat/x", "main")
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
