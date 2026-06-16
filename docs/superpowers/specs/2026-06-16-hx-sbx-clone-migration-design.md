# hx migration to `sbx --clone` — design

Date: 2026-06-16

## Background

`sbx` (Docker Sandboxes) v0.32 removed the `--branch` flag that hx was built
around and replaced it with `--clone`, which is a fundamentally different
isolation model — not a renamed flag.

### Old model (`sbx create --branch BRANCH`)

- sbx created a **git worktree on the host** for `BRANCH` and bind-mounted it
  into the sandbox.
- hx created the branch on the host first (`ensure_branch`, based on
  `origin/<target>`), found the worktree path on the host (`find_worktree`),
  copied files into it, ran `post_create` against it, and `hx mr` pushed
  directly from that worktree.
- Unit of isolation: **1 sandbox = 1 branch = 1 host worktree.**

### New model (`sbx create --clone`)

Verified empirically against sbx v0.32.0:

- The host repo is mounted **read-only** at `/run/sandbox/source`. A git-daemon
  serves it, and the host gets a remote `sandbox-<name>` pointing at
  `git://127.0.0.1:<port>/<repo>`.
- On the **first `sbx run`**, sbx creates a writable **clone inside the
  container** at the same path as the host workspace (`$WORKSPACE_DIR`, which
  equals the path passed to `sbx create`). The clone starts on the host's HEAD
  (`main`) with `origin` = `/run/sandbox/source` and all host branches available
  as `origin/*`.
- The clone does **not** exist after `sbx create` or `sbx exec` — only after the
  agent is launched via `sbx run`. A cheap `sbx run <name> -- --version`
  (claude prints its version, no API call, exits 0) materializes the clone.
- The agent commits inside the clone. The host retrieves commits with
  `git fetch sandbox-<name>`, which lands them in `refs/remotes/sandbox-<name>/*`
  **and** `refs/sandboxes/<name>/*`. The latter survive `sbx rm`.
- On `sbx rm`, in-container commits are **lost unless fetched first** (sbx warns
  and points to `git fetch sandbox-<name>` + recovery from
  `refs/sandboxes/<name>/*`).
- The clone has **no git credentials** — `git commit` works inside, `git push`
  does not. (The no-creds / push-from-host design intent is unchanged.)

## Model decision

Keep hx's existing UX and isolation guarantee: **1 sandbox : 1 branch : 1
feature.** hx owns the branch name; `hx create BRANCH`, `hxmr BRANCH`,
`hxrm BRANCH` stay. What changes is *where* the branch lives (in-container clone,
not a host worktree) and *how* commits reach the host (fetch via the
`sandbox-<name>` remote, not a shared worktree).

## Redesign

The in-container clone path equals `config.repo` (the workspace path passed to
`sbx create` is mirrored inside the container), so hx always knows it without
parsing.

### `hx create BRANCH [EXTRA_SBX_FLAGS...]`

1. `git -C <repo> fetch origin <target>` on the **host** — keeps the clone's
   base branch fresh (the clone itself has no remote creds). Replaces today's
   `ensure_branch` fetch. Best-effort: warn and continue on failure.
2. `sbx create --clone --name <name> --cpus <n> --memory <m> claude <repo>
   <extra args>` — `--branch` dropped.
3. Provision globals via `sbx exec` (unchanged): git identity, `pre-commit`,
   `~/.claude/CLAUDE.md`, merged settings, superpowers plugin.
4. **Materialize the clone:** `sbx run <name> -- --version` (cheap, no API call).
5. Create the feature branch inside the clone via `sbx exec`
   (cwd = `<repo>`): `git checkout -b BRANCH origin/<target>`, or resume
   `origin/BRANCH` when the branch already exists on the host.
6. `copy_files`: `sbx cp <repo>/<rel> <name>:<repo>/<rel>` for each configured
   path that exists (was a host filesystem copy).
7. `post_create`: `sbx exec <name> -- sh -c 'cd <repo> && <cmd>'` (path is now
   `<repo>`, not a worktree). Warn and still attach on failure.
8. `sbx run <name>` — attach interactively.

### `hx mr BRANCH [TARGET]` (alias `hxmr`)

1. `git -C <repo> fetch sandbox-<name>` → `refs/sandboxes/<name>/BRANCH`.
2. Dirty check via `sbx exec <name> -- git -C <repo> status --porcelain`; prompt
   if there are uncommitted changes.
3. `git -C <repo> push origin refs/sandboxes/<name>/BRANCH:refs/heads/BRANCH
   -o merge_request.create -o merge_request.target=<target>
   -o merge_request.remove_source_branch`. No host branch or worktree involved.

### `hx rm BRANCH` (alias `hxrm`)

1. `git -C <repo> fetch sandbox-<name>` first — preserves commits into
   `refs/sandboxes/<name>/*` (survive removal; what sbx's own warning
   recommends). Best-effort.
2. Unpushed check: compare `refs/sandboxes/<name>/BRANCH` against `origin/BRANCH`
   (if it exists) else `origin/<target>`; safe (0) when the branch tip is an
   ancestor of `origin/<branch>` (already pushed); otherwise count commits not
   on the base. Prompt before deleting if work would be lost.
3. `sbx rm --force <name>`, then `git -C <repo> remote remove sandbox-<name>`.
   Keep `refs/sandboxes/<name>/*` as a recovery safety net (harmless).

## Code & test impact

- **`sandbox.py`:** remove `parse_worktrees`, `find_worktree`,
  `worktree_is_dirty`, `ensure_branch`. Add: `sandbox_remote(name)` helper,
  `materialize_clone(name)`, `create_branch_in_clone(name, repo, branch,
  target)`, `fetch_sandbox(repo, name)`, `clone_is_dirty(name, repo)`. Rework
  `mr_push_command` to push a `refs/sandboxes/<name>/<branch>` ref and
  `unpushed_commit_count` to operate on the fetched sandbox ref. `copy_files`
  becomes `sbx cp`-based.
- **`SANDBOX_CLAUDE_MD`:** rewrite — "you work in a clone at `<repo>` on branch
  BRANCH; stay on this branch; `git commit` works, `git push` does not; ask the
  host to run `hxmr BRANCH` to push and open the MR." Drop the "never touch the
  sibling checkout" line (no sibling exists).
- **`cli.py`:** reorder `create` per the steps above; rewire `mr` and `rm` to
  the fetch-based helpers.
- **Tests:** `test_sandbox.py` and `test_cli.py` are entirely worktree-based and
  get rewritten around the clone/fetch model.
- **`README.md`:** rewrite the model description and the three command sections.

## Trade-offs

- The extra `sbx run -- --version` materialization step adds a few seconds to
  `hx create`. The alternative — letting the agent run `copy_files`/`post_create`
  itself — defeats the "prime caches before the agent works" purpose, so the
  cheap trigger is preferred.
- The clone's base is only as fresh as the host's `origin/<target>` (no remote
  creds in the clone), which is why `hx create` fetches on the host first.
- `refs/sandboxes/<name>/*` are intentionally **not** cleaned up on `hx rm`, to
  preserve a recovery path for commits.
