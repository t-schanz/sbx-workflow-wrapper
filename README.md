# hx — HARPY sandbox workflow CLI

`hx` manages a per-feature development workflow for the HARPY monorepo using
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

This installs the bundled `harpy-dev` sbx kit to `~/.config/sbx/kits/harpy-dev`
(templated with your host git identity) and writes `~/.config/hx/config.toml`, prompting
for the repo path. Re-run with `--force` to overwrite an existing kit.

Config keys (`~/.config/hx/config.toml`):

```toml
repo = "/home/user/PycharmProjects/harpy-monorepo"  # required
kit = "~/.config/sbx/kits/harpy-dev"                # default
cpus = 4
memory = "8g"
target = "main"   # default MR target
```

## Commands

### `hx create BRANCH [EXTRA_SBX_FLAGS...]`

Creates a sandbox and a git worktree for `BRANCH`, copies a cached
`build/openapi/openapi.json` into the worktree if present (skips a slow gradle run),
runs the SDK setup inside the sandbox, then attaches interactively.

```sh
hx create feat/DIGREM-123-shiny-thing
hx create feat/DIGREM-123-shiny-thing -- --gpu   # extra flags pass through to sbx create
```

### `hx mr BRANCH [TARGET]` (alias: `hxmr`)

Pushes the branch from the host and auto-creates a GitLab merge request via push
options. `TARGET` defaults to `main` (configurable).

```sh
hxmr feat/DIGREM-123-shiny-thing
hxmr feat/DIGREM-123-shiny-thing develop
```

### `hx rm BRANCH` (alias: `hxrm`)

Removes the sandbox, its worktree, and the branch. If the branch has unpushed commits
and no upstream, you are prompted before anything is deleted.

```sh
hxrm feat/DIGREM-123-shiny-thing
```

## Update

```sh
uv tool upgrade hx
```
