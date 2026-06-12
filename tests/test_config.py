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


def in_repo(monkeypatch, repo_path):
    monkeypatch.setattr(config_module, "main_repo_root", lambda: repo_path)


class TestLoadConfig:
    def test_loads_all_keys(self, config_home, monkeypatch):
        in_repo(monkeypatch, None)
        write_config(
            config_home,
            'repo = "/repo"\ncpus = 8\nmemory = "16g"\ntarget = "dev"\n'
            'copy_files = ["build/openapi/openapi.json"]\n'
            'post_create = "make gen-sdk"\n',
        )
        config = config_module.load_config()
        assert config.repo == "/repo"
        assert config.cpus == 8
        assert config.memory == "16g"
        assert config.target == "dev"
        assert config.copy_files == ["build/openapi/openapi.json"]
        assert config.post_create == "make gen-sdk"

    def test_defaults(self, config_home, monkeypatch):
        in_repo(monkeypatch, None)
        write_config(config_home, 'repo = "/repo"\n')
        config = config_module.load_config()
        assert config.cpus == 4
        assert config.memory == "8g"
        assert config.target == "main"
        assert config.copy_files == []
        assert config.post_create is None

    def test_cwd_repo_wins_over_configured_repo(self, config_home, monkeypatch):
        in_repo(monkeypatch, "/cwd-repo")
        write_config(config_home, 'repo = "/other-repo"\n')
        assert config_module.load_config().repo == "/cwd-repo"

    def test_missing_repo_outside_git_gives_helpful_error(
        self, config_home, monkeypatch
    ):
        in_repo(monkeypatch, None)
        write_config(config_home, "cpus = 8\n")
        with pytest.raises(HxError, match="hx setup"):
            config_module.load_config()


PROJECTS_CONFIG = """\
cpus = 8
target = "main"

[projects."/harpy"]
target = "develop"
copy_files = ["build/openapi/openapi.json"]
post_create = "cd ai && make gen-sdk"
"""


class TestProjectSections:
    def test_matching_project_overrides_defaults(self, config_home, monkeypatch):
        in_repo(monkeypatch, "/harpy")
        write_config(config_home, PROJECTS_CONFIG)
        config = config_module.load_config()
        assert config.repo == "/harpy"
        assert config.target == "develop"
        assert config.copy_files == ["build/openapi/openapi.json"]
        assert config.post_create == "cd ai && make gen-sdk"
        assert config.cpus == 8  # top-level defaults still apply

    def test_other_repo_gets_no_project_hooks(self, config_home, monkeypatch):
        in_repo(monkeypatch, "/some-other-repo")
        write_config(config_home, PROJECTS_CONFIG)
        config = config_module.load_config()
        assert config.repo == "/some-other-repo"
        assert config.target == "main"
        assert config.copy_files == []
        assert config.post_create is None

    def test_project_key_paths_are_normalized(self, config_home, monkeypatch):
        in_repo(monkeypatch, str(config_home / "harpy"))
        write_config(
            config_home,
            f'[projects."{config_home}/../{config_home.name}/harpy/"]\n'
            'post_create = "make gen-sdk"\n',
        )
        assert config_module.load_config().post_create == "make gen-sdk"

    def test_fallback_repo_still_matches_its_project_section(
        self, config_home, monkeypatch
    ):
        in_repo(monkeypatch, None)
        write_config(config_home, 'repo = "/harpy"\n' + PROJECTS_CONFIG)
        config = config_module.load_config()
        assert config.repo == "/harpy"
        assert config.post_create == "cd ai && make gen-sdk"
