import json
import subprocess
import sys
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
    def test_runs_via_exec_after_create(self):
        command = sandbox.install_plugins_command("feat-x")
        assert command[:6] == ["sbx", "exec", "feat-x", "--", "sh", "-c"]

    def test_adds_every_marketplace_before_installing(self):
        script = sandbox.install_plugins_command("feat-x")[6]
        for marketplace in sandbox.MARKETPLACES:
            assert f"marketplace add {marketplace} 2>/dev/null" in script
        first_install = script.index("plugin install")
        for marketplace in sandbox.MARKETPLACES:
            assert script.index(f"marketplace add {marketplace}") < first_install

    def test_installs_the_host_session_plugins(self):
        script = sandbox.install_plugins_command("feat-x")[6]
        for plugin in sandbox.PLUGINS:
            assert f"claude plugin install {plugin}" in script

    def test_covers_ponytail_and_mattpocock(self):
        assert "ponytail@ponytail" in sandbox.PLUGINS
        assert "mattpocock-skills@claude-plugins-official" in sandbox.PLUGINS
        assert "DietrichGebert/ponytail" in sandbox.MARKETPLACES


class TestHostJsonKey:
    def test_reads_the_requested_key(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text('{"mcpServers": {"telecontext": {"type": "http"}}, "other": 1}')
        assert sandbox.host_json_key(path, "mcpServers") == {
            "telecontext": {"type": "http"}
        }

    def test_missing_file_is_empty(self, tmp_path):
        assert sandbox.host_json_key(tmp_path / "absent.json", "mcpServers") == {}

    def test_missing_key_is_empty(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text('{"other": 1}')
        assert sandbox.host_json_key(path, "mcpServers") == {}

    def test_null_key_is_empty(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text('{"mcpServers": null}')
        assert sandbox.host_json_key(path, "mcpServers") == {}


class TestMergeJsonCommands:
    def test_copies_then_merges_under_the_key(self, tmp_path):
        payload = tmp_path / "hx-mcp-0.json"
        commands = sandbox.merge_json_commands(
            "feat-x", payload, "mcpServers", "/home/agent/.claude.json"
        )
        assert commands[0] == ["sbx", "cp", str(payload), "feat-x:/tmp/hx-mcp-0.json"]
        assert commands[1][:6] == [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "python3",
            "-c",
        ]
        assert commands[1][7:] == [
            "/home/agent/.claude.json",
            "mcpServers",
            "/tmp/hx-mcp-0.json",
        ]

    def test_merge_script_updates_existing_keys_and_removes_the_payload(self, tmp_path):
        target = tmp_path / "claude.json"
        target.write_text('{"mcpServers": {"old": 1}, "keepMe": true}')
        payload = tmp_path / "payload.json"
        payload.write_text('{"telecontext": {"type": "http"}}')
        subprocess.run(
            [
                sys.executable,
                "-c",
                sandbox.MERGE_JSON_SCRIPT,
                str(target),
                "mcpServers",
                str(payload),
            ],
            check=True,
        )
        merged = json.loads(target.read_text())
        assert merged["mcpServers"] == {"old": 1, "telecontext": {"type": "http"}}
        assert merged["keepMe"] is True
        assert not payload.exists()

    def test_merge_script_creates_a_missing_target(self, tmp_path):
        target = tmp_path / "nested" / "credentials.json"
        payload = tmp_path / "payload.json"
        payload.write_text('{"telecontext|abc": {"accessToken": "t"}}')
        subprocess.run(
            [
                sys.executable,
                "-c",
                sandbox.MERGE_JSON_SCRIPT,
                str(target),
                "mcpOAuth",
                str(payload),
            ],
            check=True,
        )
        assert json.loads(target.read_text()) == {
            "mcpOAuth": {"telecontext|abc": {"accessToken": "t"}}
        }


class TestProvisionMcp:
    def _stub_host(self, monkeypatch, tmp_path, servers, oauth):
        config = tmp_path / "claude.json"
        config.write_text(json.dumps({"mcpServers": servers}))
        credentials = tmp_path / "credentials.json"
        credentials.write_text(json.dumps({"mcpOAuth": oauth}))
        monkeypatch.setattr(
            sandbox,
            "MCP_TARGETS",
            (
                (config, "mcpServers", "/home/agent/.claude.json"),
                (credentials, "mcpOAuth", "/home/agent/.claude/.credentials.json"),
            ),
        )

    def test_copies_servers_and_tokens_into_the_sandbox(self, monkeypatch, tmp_path):
        self._stub_host(
            monkeypatch,
            tmp_path,
            {"telecontext": {"type": "http"}},
            {"telecontext|abc": {"accessToken": "t"}},
        )
        payloads = {}
        calls = []

        def run(command, check=True):
            calls.append(command)
            if command[1] == "cp":
                payloads[command[2]] = Path(command[2]).read_text()

        monkeypatch.setattr(sandbox, "run", run)
        sandbox.provision_mcp("feat-x")

        merges = [c for c in calls if c[1] == "exec"]
        assert [c[8] for c in merges] == ["mcpServers", "mcpOAuth"]
        assert [c[7] for c in merges] == [
            "/home/agent/.claude.json",
            "/home/agent/.claude/.credentials.json",
        ]
        assert json.loads(list(payloads.values())[0]) == {
            "telecontext": {"type": "http"}
        }

    def test_empty_host_config_runs_nothing(self, monkeypatch, tmp_path):
        self._stub_host(monkeypatch, tmp_path, {}, {})
        calls = []
        monkeypatch.setattr(
            sandbox, "run", lambda command, check=True: calls.append(command)
        )
        sandbox.provision_mcp("feat-x")
        assert calls == []

    def test_staged_payload_is_not_world_readable(self, monkeypatch, tmp_path):
        self._stub_host(
            monkeypatch, tmp_path, {}, {"telecontext|abc": {"accessToken": "t"}}
        )
        modes = []
        monkeypatch.setattr(
            sandbox,
            "run",
            lambda command, check=True: (
                modes.append(Path(command[2]).stat().st_mode)
                if command[1] == "cp"
                else None
            ),
        )
        sandbox.provision_mcp("feat-x")
        assert modes and all(mode & 0o077 == 0 for mode in modes)


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


class TestSandboxRemote:
    def test_prefixes_name(self):
        assert sandbox.sandbox_remote("feat-x") == "sandbox-feat-x"


class TestMaterializeCloneCommand:
    def test_runs_claude_version_via_sbx_run(self):
        assert sandbox.materialize_clone_command("feat-x") == [
            "sbx",
            "run",
            "--name",
            "feat-x",
            "--",
            "--version",
        ]


class TestBranchCheckoutCommand:
    def test_passes_values_as_positional_args(self):
        command = sandbox.branch_checkout_command("feat-x", "/repo", "feat/x", "main")
        assert command == [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "sh",
            "-c",
            'cd "$1" && '
            'if git rev-parse --verify --quiet "origin/$2" >/dev/null; '
            'then base="origin/$2"; else base="origin/$3"; fi && '
            'git checkout -B "$2" "$base"',
            "sh",
            "/repo",
            "feat/x",
            "main",
        ]

    def test_branch_with_apostrophe_is_not_interpolated_into_script(self):
        command = sandbox.branch_checkout_command(
            "feat-x", "/repo", "feat/o'brien", "main"
        )
        script = command[6]
        # the branch value must NOT appear in the script body (passed as argv instead)
        assert "o'brien" not in script
        assert command[-2] == "feat/o'brien"


class TestAllowUnattendedToolsCommand:
    def test_merges_an_allow_list_into_the_agent_settings(self, tmp_path, monkeypatch):
        command = sandbox.allow_unattended_tools_command("feat-x")
        assert command[:6] == ["sbx", "exec", "feat-x", "--", "python3", "-c"]
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            '{"permissions": {"allow": ["Bash"], "deny": ["Read(**)"]}}'
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        subprocess.run(
            [sys.executable, "-c", command[6], *command[7:]], check=True, cwd=tmp_path
        )
        written = json.loads(settings.read_text())["permissions"]
        assert written["allow"].count("Bash") == 1
        assert {"Write", "Edit", "Task"} <= set(written["allow"])
        assert written["deny"] == ["Read(**)"]


class TestHeadlessAgentCommand:
    def test_starts_in_the_repo_without_bypassing_permissions(self, tmp_path):
        prompt = tmp_path / "ticket.md"
        prompt.write_text("do the thing")
        command = sandbox.headless_agent_command("feat-x", "/repo", prompt)
        assert command[:5] == ["sbx", "exec", "feat-x", "--", "sh"]
        assert "--permission-mode acceptEdits" in command[6]
        assert "bypass" not in command[6]
        assert "dangerously" not in command[6]
        assert command[-2:] == ["/repo", "do the thing"]

    def test_prompt_is_never_interpolated_into_the_script(self, tmp_path):
        prompt = tmp_path / "ticket.md"
        prompt.write_text('$(rm -rf /) "; whoami')
        command = sandbox.headless_agent_command("feat-x", "/repo", prompt)
        assert "rm -rf" not in command[6]
        assert command[-1] == '$(rm -rf /) "; whoami'


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
