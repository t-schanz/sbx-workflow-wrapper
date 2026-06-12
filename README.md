# hx — sandbox workflow CLI

`hx` manages a per-feature development workflow using
[Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/): one sandbox plus one
git worktree per feature branch. Each sandbox runs Claude Code on a dedicated worktree
of your repo. The sandbox deliberately has **no git credentials** — `git commit` works
inside, `git push` does not. Pushing (and creating the GitLab merge request) happens on
the host via `hx mr`.

## Install

```sh
uv tool install git+https://gitlab.example.com/your-group/hx.git
```

This exposes three commands: `hx`, plus `hxmr` and `hxrm` as muscle-memory aliases for
`hx mr` and `hx rm`.

## First run

```sh
hx setup
```

This installs the bundled `dev` sbx kit to `~/.config/sbx/kits/dev` (templated with
your host git identity) and writes `~/.config/hx/config.toml`, prompting for the repo
path. Re-run with `--force` to overwrite an existing kit.

Config keys (`~/.config/hx/config.toml`):

```toml
repo = "/home/user/PycharmProjects/my-repo"  # required
kit = "~/.config/sbx/kits/dev"               # default
cpus = 4
memory = "8g"
target = "main"   # default MR target / base for the unpushed-commit check in `hx rm`
```

### Project-specific workflows

Two optional config keys hook into `hx create`, so per-project setup lives in config,
not code:

- **`copy_files`** — repo-relative paths copied from the main checkout into a fresh
  worktree (parent directories are created, missing files skipped silently). Use it to
  prime caches or build artifacts that are expensive to regenerate.
- **`post_create`** — a shell command run *inside the sandbox* after creation, via a
  throwaway `sh -c` starting at the worktree root (a `cd` inside it needs no undoing).
  If it fails, `hx create` warns and still attaches so you can finish setup manually.
  For anything beyond a one-liner, point it at a script in your repo.

Worked example — a monorepo whose Python SDK in `ai/` is generated from an OpenAPI
spec that a slow gradle run produces. Copying the host's cached spec into the worktree
first lets `gen-sdk` skip gradle entirely:

```toml
copy_files = ["build/openapi/openapi.json"]
post_create = "cd ai && uv sync --group dev --group tools && uv run make gen-sdk"
```

## Commands

### `hx create BRANCH [EXTRA_SBX_FLAGS...]`

Creates a sandbox and a git worktree for `BRANCH`, copies any configured `copy_files`
into the worktree, runs the configured `post_create` command inside the sandbox, then
attaches interactively.

```sh
hx create feat/PROJ-123-shiny-thing
hx create feat/PROJ-123-shiny-thing -- --gpu   # extra flags pass through to sbx create
```

### `hx mr BRANCH [TARGET]` (alias: `hxmr`)

Pushes the branch from the host and auto-creates a GitLab merge request via push
options. `TARGET` defaults to `main` (configurable).

```sh
hxmr feat/PROJ-123-shiny-thing
hxmr feat/PROJ-123-shiny-thing develop
```

### `hx rm BRANCH` (alias: `hxrm`)

Removes the sandbox, its worktree, and the branch. If the branch has unpushed commits
and no upstream, you are prompted before anything is deleted.

```sh
hxrm feat/PROJ-123-shiny-thing
```

## Update

```sh
uv tool upgrade hx
```
