from hx import setup_cmd


class TestRunSetup:
    def test_existing_config_is_not_overwritten(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        config_file = tmp_path / "hx" / "config.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text('repo = "/repo"\n[projects."/repo"]\ncpus = 8\n')
        setup_cmd.run_setup()
        assert '[projects."/repo"]' in config_file.read_text()

    def test_writes_config_with_prompted_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(setup_cmd, "main_repo_root", lambda: "/cwd-repo")
        monkeypatch.setattr(setup_cmd.typer, "prompt", lambda *a, **k: "/cwd-repo")
        setup_cmd.run_setup()
        config_file = tmp_path / "hx" / "config.toml"
        assert 'repo = "/cwd-repo"' in config_file.read_text()
