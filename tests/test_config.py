import pytest

from hx import HxError
from hx import config as config_module


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def write_config(config_home, content):
    config_dir = config_home / "hx"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(content)


class TestLoadConfig:
    def test_loads_all_keys(self, config_home):
        write_config(
            config_home,
            'repo = "/repo"\nkit = "/kit"\ncpus = 8\nmemory = "16g"\ntarget = "dev"\n',
        )
        config = config_module.load_config()
        assert config.repo == "/repo"
        assert config.kit == "/kit"
        assert config.cpus == 8
        assert config.memory == "16g"
        assert config.target == "dev"

    def test_defaults(self, config_home):
        write_config(config_home, 'repo = "/repo"\n')
        config = config_module.load_config()
        assert config.kit == "~/.config/sbx/kits/harpy-dev"
        assert config.cpus == 4
        assert config.memory == "8g"
        assert config.target == "main"

    def test_missing_repo_falls_back_to_git_root(self, config_home, monkeypatch):
        monkeypatch.setattr(config_module, "git_toplevel", lambda: "/cwd-repo")
        config = config_module.load_config()
        assert config.repo == "/cwd-repo"

    def test_missing_repo_outside_git_gives_helpful_error(
        self, config_home, monkeypatch
    ):
        monkeypatch.setattr(config_module, "git_toplevel", lambda: None)
        with pytest.raises(HxError, match="hx setup"):
            config_module.load_config()
