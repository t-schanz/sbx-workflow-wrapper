"""Config model and loading for ~/.config/hx/config.toml."""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel

from hx import HxError
from hx.sandbox import git_toplevel


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


def load_config() -> Config:
    path = config_path()
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    if "repo" not in data:
        repo = git_toplevel()
        if repo is None:
            raise HxError(
                "no repo configured and not inside a git repository — run `hx setup`"
            )
        data["repo"] = repo
    return Config(**data)
