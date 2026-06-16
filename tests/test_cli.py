import pytest
from typer.testing import CliRunner

from hx import cli, config as config_module, sandbox
from hx.config import Config

runner = CliRunner()


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


def configure(monkeypatch, repo_path, **overrides):
    config = Config(repo=str(repo_path), **overrides)
    monkeypatch.setattr(config_module, "load_config", lambda: config)


@pytest.fixture
def recorded_runs(monkeypatch):
    calls = []

    def record(command, check=True):
        calls.append(command)

    monkeypatch.setattr(sandbox, "run", record)
    return calls


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
        assert recorded_runs[0] == [
            "git",
            "-C",
            str(repo_path),
            "fetch",
            "origin",
            "main",
        ]
        assert recorded_runs[1][:3] == ["sbx", "create", "--clone"]

    def test_failed_host_fetch_warns_but_continues(self, repo, monkeypatch):
        calls = []

        def run(command, check=True):
            calls.append(command)
            if "fetch" in command and "origin" in command:
                raise cli.HxError("offline")

        monkeypatch.setattr(sandbox, "run", run)
        result = runner.invoke(cli.app, ["create", "feat/x"])
        assert result.exit_code == 0
        assert "could not refresh" in result.output
        assert any(c[:3] == ["sbx", "create", "--clone"] for c in calls)
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
        assert [
            "sbx",
            "exec",
            "feat-x",
            "--",
            "mkdir",
            "-p",
            str(repo_path / "build" / "openapi"),
        ] in recorded_runs
        assert [
            "sbx",
            "cp",
            str(repo_path / "build" / "openapi" / "openapi.json"),
            f"feat-x:{repo_path / 'build' / 'openapi' / 'openapi.json'}",
        ] in recorded_runs

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
