from pathlib import Path

import pytest
from typer.testing import CliRunner

from hx import cli, config as config_module, sandbox
from hx.config import Config

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    repo_path.mkdir()
    monkeypatch.setattr(
        config_module, "load_config", lambda: Config(repo=str(repo_path))
    )
    porcelain = (
        f"worktree {repo_path}\nHEAD 1111\nbranch refs/heads/main\n\n"
        f"worktree {worktree}\nHEAD 2222\nbranch refs/heads/feat/x\n"
    )
    monkeypatch.setattr(sandbox, "capture", lambda command: porcelain)
    return repo_path, worktree


@pytest.fixture
def recorded_runs(monkeypatch):
    calls = []

    def record(command, check=True):
        calls.append(command)

    monkeypatch.setattr(sandbox, "run", record)
    return calls


class TestCreate:
    def test_invokes_exact_commands(self, repo, recorded_runs):
        repo_path, worktree = repo
        result = runner.invoke(cli.app, ["create", "feat/x", "--", "--gpu"])
        assert result.exit_code == 0
        assert recorded_runs[0] == [
            "sbx",
            "create",
            "--branch",
            "feat/x",
            "--name",
            "feat-x",
            "--cpus",
            "4",
            "--memory",
            "8g",
            "--kit",
            str(Path("~/.config/sbx/kits/harpy-dev").expanduser()),
            "claude",
            str(repo_path),
            "--gpu",
        ]
        assert recorded_runs[1] == [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "sh",
            "-c",
            f"cd '{worktree}/ai' && uv sync --group dev --group tools"
            " && uv run make gen-sdk",
        ]
        assert recorded_runs[2] == ["sbx", "run", "feat-x"]

    def test_copies_openapi_spec_when_present(self, repo, recorded_runs):
        repo_path, worktree = repo
        source = repo_path / "build" / "openapi" / "openapi.json"
        source.parent.mkdir(parents=True)
        source.write_text('{"openapi": "3.1.0"}')
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        copied = worktree / "build" / "openapi" / "openapi.json"
        assert copied.read_text() == '{"openapi": "3.1.0"}'

    def test_sdk_failure_warns_but_continues(self, repo, monkeypatch):
        calls = []

        def run(command, check=True):
            calls.append(command)
            if command[0] == "sbx" and command[1] == "exec":
                raise cli.HxError("boom")

        monkeypatch.setattr(sandbox, "run", run)
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        assert "SDK setup failed" in result.output
        assert calls[-1] == ["sbx", "run", "feat-x"]

    def test_missing_worktree_is_an_error(self, repo, recorded_runs, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: "")
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 1


class TestMr:
    def test_pushes_with_default_target(self, repo, recorded_runs):
        _, worktree = repo
        result = runner.invoke(cli.app, ["mr", "feat/x"])
        assert result.exit_code == 0
        assert recorded_runs == [sandbox.mr_push_command(worktree, "main")]

    def test_pushes_with_explicit_target(self, repo, recorded_runs):
        _, worktree = repo
        result = runner.invoke(cli.app, ["mr", "feat/x", "develop"])
        assert result.exit_code == 0
        assert recorded_runs == [sandbox.mr_push_command(worktree, "develop")]

    def test_missing_worktree_is_an_error(self, repo, recorded_runs, monkeypatch):
        monkeypatch.setattr(sandbox, "capture", lambda command: "")
        result = runner.invoke(cli.app, ["mr", "feat/x"])
        assert result.exit_code == 1


class TestRm:
    def test_safe_branch_removed_without_prompt(self, repo, recorded_runs, monkeypatch):
        monkeypatch.setattr(
            sandbox, "unpushed_commit_count", lambda repo, branch, base: 0
        )
        result = runner.invoke(cli.app, ["rm", "feat/x"])
        assert result.exit_code == 0
        assert recorded_runs == [["sbx", "rm", "--force", "feat-x"]]

    def test_unpushed_commits_prompt_declined_aborts(
        self, repo, recorded_runs, monkeypatch
    ):
        monkeypatch.setattr(
            sandbox, "unpushed_commit_count", lambda repo, branch, base: 3
        )
        result = runner.invoke(cli.app, ["rm", "feat/x"], input="n\n")
        assert result.exit_code == 1
        assert recorded_runs == []

    def test_unpushed_commits_prompt_accepted_removes(
        self, repo, recorded_runs, monkeypatch
    ):
        monkeypatch.setattr(
            sandbox, "unpushed_commit_count", lambda repo, branch, base: 3
        )
        result = runner.invoke(cli.app, ["rm", "feat/x"], input="y\n")
        assert result.exit_code == 0
        assert "3 unpushed commit(s)" in result.output
        assert recorded_runs == [["sbx", "rm", "--force", "feat-x"]]
