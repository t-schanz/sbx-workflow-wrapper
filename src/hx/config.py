"""Config model and loading for ~/.config/hx/config.toml."""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel

from hx import HxError
from hx.sandbox import main_repo_root


class Config(BaseModel):
    repo: str
    kit: str = "~/.config/sbx/kits/dev"
    cpus: int = 4
    memory: str = "8g"
    target: str = "main"
    # Repo-relative paths copied into a fresh worktree (e.g. cached build artifacts).
    copy_files: list[str] = []
    # Shell command run inside the sandbox at the worktree root after create.
    post_create: str | None = None


def config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "hx" / "config.toml"


def normalize(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def load_config() -> Config:
    """Resolve the effective config for the repo hx is operating on.

    The repo is the main checkout containing the cwd (worktrees resolve to their
    main checkout), falling back to the top-level `repo` key outside any git repo.
    A matching `[projects."<repo-path>"]` section overrides the top-level keys.
    """
    path = config_path()
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    projects = data.pop("projects", {})

    repo = main_repo_root() or data.get("repo")
    if not repo:
        raise HxError(
            "no repo configured and not inside a git repository — run `hx setup`"
        )
    repo = normalize(repo)

    for project_path, overrides in projects.items():
        if normalize(project_path) == repo:
            data.update(overrides)
            break
    data["repo"] = repo
    return Config(**data)
