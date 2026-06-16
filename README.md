# hx — sandbox workflow CLI

`hx` manages a per-feature development workflow using
[Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/): one sandbox per
feature branch, each running Claude Code on a **private in-container clone** of your
repo (`sbx --clone`). The host repo is mounted read-only; the agent commits inside the
clone. The sandbox deliberately has **no git credentials** — `git commit` works inside,
`git push` does not. Pushing (and creating the GitLab merge request) happens on the host
via `hx mr`, which fetches the sandbox's commits through the `sandbox-<name>` git remote
that `sbx` wires up.

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

hx resolves the **main repository checkout containing your cwd** — if your cwd is inside
a git repository, that repo's root is used. Only outside any git repository does the
top-level `repo` key apply as a fallback.

### Project-specific workflows

Per-project settings live in `[projects."<main-checkout-path>"]` sections, merged over
the top-level defaults when the path matches the resolved repo. Any key can be
overridden per project; two optional keys hook into `hx create`, so per-project setup
lives in config, not code:

- **`copy_files`** — repo-relative paths copied from the host checkout into the
  in-container clone via `sbx cp` (parent directories are created, missing files
  skipped silently). Use it to prime caches or build artifacts that are expensive to
  regenerate.
- **`post_create`** — a shell command run *inside the sandbox* after the clone is
  created, via a throwaway `sh -c` starting at the clone root. If it fails, `hx create`
  warns and still attaches so you can finish setup manually. For anything beyond a
  one-liner, point it at a script in your repo.

Worked example — a monorepo whose Python SDK in `ai/` is generated from an OpenAPI
spec that a slow gradle run produces. Copying the host's cached spec into the clone
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

Creates a `--clone` sandbox for `BRANCH`. First refreshes `origin/<target>` on the host
so the clone's base is current (the clone has no remote credentials of its own). Then
creates the sandbox, provisions it (git identity from the host, pre-commit, the
superpowers claude plugin, workflow notes in the agent's CLAUDE.md), materializes the
in-container clone with a cheap launch, checks out `BRANCH` inside it (based on
`origin/<target>`, or `origin/BRANCH` when resuming an existing branch), copies any
configured `copy_files` into the clone, runs the configured `post_create` command, then
attaches interactively.

Provisioning deliberately happens via `sbx exec` after creation, **not** via an sbx
kit: as of sbx 0.30, passing any `--kit` skips the Claude credential seeding (you'd
get a login prompt in every sandbox), and kit commands that run the claude CLI get
their plugin enablement clobbered by sbx's later settings write.

```sh
hx create feat/PROJ-123-shiny-thing
hx create feat/PROJ-123-shiny-thing -- --gpu   # extra flags pass through to sbx create
```

### `hx mr BRANCH [TARGET]` (alias: `hxmr`)

Fetches the sandbox's commits to the host (via the `sandbox-<name>` remote) and pushes
`BRANCH` to `origin`, auto-creating a GitLab merge request via push options. Prompts if
the clone still has uncommitted changes. `TARGET` defaults to `main` (configurable).

```sh
hxmr feat/PROJ-123-shiny-thing
hxmr feat/PROJ-123-shiny-thing develop
```

### `hx rm BRANCH` (alias: `hxrm`)

Fetches the sandbox's commits first (mirrored into `refs/sandboxes/<name>/*`, which
survive removal), then removes the sandbox and drops the host-side `sandbox-<name>`
remote. If the branch has unpushed commits, you are prompted before anything is removed.

```sh
hxrm feat/PROJ-123-shiny-thing
```

## Update

```sh
uv tool upgrade hx
```
