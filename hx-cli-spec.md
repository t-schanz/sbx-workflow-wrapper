# Spec: `hx` — HARPY sandbox workflow CLI

Generate a complete, installable Python CLI repo in this (empty) folder. You have no
other context — everything you need is in this document. Build exactly this; where the
spec is silent, prefer the simplest thing that works (KISS, YAGNI).

## Purpose & background

The tool manages a per-feature development workflow for the HARPY monorepo using
**Docker Sandboxes (`sbx`)** — a CLI already installed on the host that runs Claude Code
inside a Docker container. The workflow: one sandbox + one git worktree per feature
branch.

Key facts about `sbx` the tool relies on (do NOT reinvent these — shell out to `sbx`):

- `sbx create --branch BRANCH --name NAME [flags] claude REPO_PATH` creates a sandbox,
  creates a git worktree for BRANCH at `<repo>/.sbx/<name>-worktrees/<branch>`, and
  mounts the whole git repo root path-identically (virtiofs) so git works inside.
- `sbx exec NAME -- CMD...` runs a command inside a sandbox.
- `sbx run NAME` attaches to a sandbox interactively.
- `sbx rm --force NAME` removes the sandbox AND deletes its worktree AND its branch.
- Sandbox names must not contain `/` — derive them from branch names by replacing every
  `/` with `-`.
- The sandbox deliberately has **no git credentials**: `git commit` works inside,
  `git push` does not. Pushing happens on the host (that's the `mr` command).

## Commands

One Typer app with three subcommands, exposed as console scripts `hx`, plus `hxmr` and
`hxrm` as aliases for muscle memory (thin entry points calling the same functions):

| Script | Equivalent | Purpose |
|---|---|---|
| `hx create BRANCH [EXTRA...]` | — | new sandbox + worktree + SDK setup, then attach |
| `hx mr BRANCH [TARGET]` | `hxmr BRANCH [TARGET]` | push from host, auto-create GitLab MR |
| `hx rm BRANCH` | `hxrm BRANCH` | remove sandbox + worktree + branch |
| `hx setup` | — | install the bundled sbx kit + write config |

All commands take `--help` (Typer gives this for free — write good help text from the
descriptions below).

### `hx create BRANCH [EXTRA_SBX_FLAGS...]`

1. Sanitize name: `branch.replace("/", "-")`.
2. Run:
   `sbx create --branch BRANCH --name NAME --cpus 4 --memory 8g --kit ~/.config/sbx/kits/harpy-dev claude REPO [extra flags…]`
   Pass any extra CLI args through verbatim (Typer: allow unknown/extra args). Abort on
   non-zero exit.
3. Locate the worktree for BRANCH by parsing `git -C REPO worktree list --porcelain`
   (find the `worktree <path>` block whose `branch refs/heads/<BRANCH>` line matches).
   Error out if not found.
4. If `REPO/build/openapi/openapi.json` exists, copy it to
   `<worktree>/build/openapi/openapi.json` (create dirs). This lets the SDK generation
   in the next step skip a slow gradle run.
5. Run inside the sandbox (print a progress line first):
   `sbx exec NAME -- sh -c "cd '<worktree>/ai' && uv sync --group dev --group tools && uv run make gen-sdk"`
   On failure, warn (`SDK setup failed — run it manually inside the sandbox`) but
   continue.
6. Attach: `sbx run NAME` (inherit stdio; this is interactive).

### `hx mr BRANCH [TARGET]` (alias `hxmr`)

TARGET defaults to `main`. Locate the worktree as above (error if missing), then run on
the host:

```
git -C <worktree> push -u origin HEAD \
  -o merge_request.create \
  -o merge_request.target=<TARGET> \
  -o merge_request.remove_source_branch
```

GitLab push options create the MR — no API token or python-gitlab needed.

### `hx rm BRANCH` (alias `hxrm`)

Because `sbx rm --force` also deletes the branch, guard against losing work:

1. If the branch exists (`git -C REPO rev-parse --verify --quiet BRANCH`) and has **no
   upstream** (`git rev-parse --abbrev-ref --verify --quiet BRANCH@{upstream}` fails),
   count `git rev-list --count main..BRANCH`. If > 0, prompt
   `branch X has N unpushed commit(s) — delete anyway? [y/N]` and abort unless `y`.
2. Run `sbx rm --force NAME` (sanitized name).

### `hx setup`

First-run bootstrap:

1. Write the bundled kit (see below) to `~/.config/sbx/kits/harpy-dev/spec.yaml`,
   templating the git identity from host `git config user.name` / `user.email` (error
   if unset). Don't overwrite an existing kit without `--force`.
2. Write config (see below) — prompt for the repo path, defaulting to the git root of
   the cwd if inside a git repo.

## Bundled sbx kit

Ship as package data (e.g. `src/hx/kit/spec.yaml.template`) and install via `hx setup`.
Exact content, with `{name}` / `{email}` templated:

```yaml
schemaVersion: "1"
kind: mixin
name: harpy-dev
displayName: HARPY Dev Setup
description: Superpowers plugin, pre-commit and git identity for HARPY sandboxes

commands:
  install:
    - command: "claude plugin marketplace add anthropics/claude-plugins-official 2>/dev/null; claude plugin install superpowers@claude-plugins-official"
      user: "1000"
      description: Install superpowers plugin (explicit add - the bundled marketplace snapshot is stale)
    - command: "uv tool install pre-commit"
      user: "1000"
      description: pre-commit binary (repo git hooks call it)
    - command: "git config --global user.name '{name}' && git config --global user.email {email}"
      user: "1000"
      description: git identity (sbx forwarding is unreliable)

  initFiles:
    - path: /home/agent/.claude/CLAUDE.md
      onlyIfMissing: true
      mode: "0644"
      description: HARPY sandbox workflow notes for the agent
      content: |
        ## HARPY sandbox workflow
        You run inside a Docker sandbox on a dedicated git worktree (branch = sandbox name).
        Work only inside this worktree, never in the main repo checkout next to it.
        git commit works here. git push does NOT - the sandbox has no git credentials by design.
        To push and open a merge request, ask the user to run on the host: hxmr <branch>
```

## Configuration

`~/.config/hx/config.toml` (respect `$XDG_CONFIG_HOME`), parsed with stdlib `tomllib`
into a small pydantic `BaseModel`. Keys (keep them terse):

```toml
repo = "/home/user/PycharmProjects/harpy-monorepo"  # required
kit = "~/.config/sbx/kits/harpy-dev"                # default shown
cpus = 4
memory = "8g"
target = "main"        # default MR target / base for unpushed-commit count
```

Resolution for `repo`: config value if set; else git root of cwd; else a clear error
telling the user to run `hx setup`.

## Tech & repo layout

- Python ≥ 3.12, `uv`-managed. `pyproject.toml` with `[project.scripts]`:
  `hx`, `hxmr`, `hxrm`. Build backend: hatchling.
- Dependencies: `typer`, `pydantic`. Dev: `pytest`, `ruff`.
- src layout:

```
.
├── pyproject.toml
├── README.md
├── src/hx/
│   ├── __init__.py
│   ├── cli.py          # Typer app + the three alias entry points
│   ├── config.py       # pydantic model + load/resolve
│   ├── sandbox.py      # sbx/git subprocess calls, worktree parsing, name sanitizing
│   ├── setup_cmd.py    # hx setup
│   └── kit/spec.yaml.template
└── tests/
```

- All subprocess calls via a thin layer in `sandbox.py` so tests can mock it. Stream
  output to the terminal (no capture) except where parsing is needed.
- Exit codes: 0 success, 1 on any failure, with a one-line human-readable error on
  stderr. No tracebacks for expected failures.

## Code style

- Clean Code: KISS, YAGNI, DRY. Explicit over clever. Comments only where the code
  can't speak for itself.
- Descriptive variable names — no abbreviations (`branch`, not `br`).
- Satisfy ruff without `noqa` suppressions.
- Pydantic `BaseModel` (not dataclasses) for data models.

## Tests (required, not optional)

Pure-logic tests with mocked subprocess — no real `sbx`/`git`/Docker:

- name sanitization (`feat/DIGREM-123-x` → `feat-DIGREM-123-x`)
- `git worktree list --porcelain` parsing: match found, not found, multiple worktrees,
  detached-HEAD blocks (no `branch` line) skipped
- `rm` guard decision table: branch missing / has upstream / no upstream + 0 commits /
  no upstream + N commits (prompt yes/no)
- `mr` builds the exact push command incl. push options and default target
- config: load, defaults, missing repo → helpful error
- kit templating: identity substituted, refuses to overwrite without `--force`

## README

Cover: what the tool is (one paragraph, including the sandbox/worktree/no-credentials
model), install via `uv tool install git+<repo-url>`, `hx setup` first run, the three
commands with examples, and how to update (`uv tool upgrade`).

## Acceptance checklist

- `uv sync && uv run pytest` passes; `uv run ruff check` clean
- `uv tool install .` exposes `hx`, `hxmr`, `hxrm`; `hx --help` and each subcommand's
  `--help` are accurate
- `hx create`/`mr`/`rm` invoke exactly the commands specified above (verify via tests)
