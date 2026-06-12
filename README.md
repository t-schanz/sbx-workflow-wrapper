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

This writes `~/.config/hx/config.toml`, prompting for the repo path (an existing config
is left unchanged).

Config keys (`~/.config/hx/config.toml`):

```toml
repo = "/home/user/PycharmProjects/my-repo"  # fallback when outside any git repo
cpus = 4
memory = "8g"
target = "main"   # default MR target / base for the unpushed-commit check in `hx rm`
```

### Which repo does hx operate on?

hx resolves the **main repository checkout containing your cwd** — running from inside
any worktree of a repo (including the `.sbx/...` worktrees hx creates) resolves to the
main checkout, since they share a common git dir. Only outside any git repository does
the top-level `repo` key apply as a fallback.

### Project-specific workflows

Per-project settings live in `[projects."<main-checkout-path>"]` sections, merged over
the top-level defaults when the path matches the resolved repo. Any key can be
overridden per project; two optional keys hook into `hx create`, so per-project setup
lives in config, not code:

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
[projects."/home/user/PycharmProjects/my-monorepo"]
copy_files = ["build/openapi/openapi.json"]
post_create = "cd ai && uv sync --group dev --group tools && uv run make gen-sdk"
```

Running `hx` from a repo without a `[projects]` section just uses the top-level
defaults — no hooks run.

## Commands

### `hx create BRANCH [EXTRA_SBX_FLAGS...]`

Creates a sandbox and a git worktree for `BRANCH`, provisions the sandbox (git
identity from the host, pre-commit, the superpowers claude plugin, workflow notes in
the agent's CLAUDE.md), copies any configured `copy_files` into the worktree, runs the
configured `post_create` command inside the sandbox, then attaches interactively.

Provisioning deliberately happens via `sbx exec` after creation, **not** via an sbx
kit: as of sbx 0.30, passing any `--kit` skips the Claude credential seeding (you'd
get a login prompt in every sandbox), and kit commands that run the claude CLI get
their plugin enablement clobbered by sbx's later settings write.

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
