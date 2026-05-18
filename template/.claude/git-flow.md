# Git Flow

Every change in this repo — full TDD workflow or one-off trivial edit — follows the same lifecycle: branch off `main`, do the work, commit, push, open or update a merge request via `glab`, return to `main`. The main agent owns this lifecycle. **Do not spawn a subagent for any of the steps below** — all of it is short, local-only or single-command-remote, and trivially runs from the main thread. Subagents add latency and an extra approval surface for no benefit here.

## Step 1: Start a feature branch from clean `main`

Run from the main thread with Bash:

```bash
git rev-parse --abbrev-ref HEAD
```

- **Already on a `claude/<slug>` feature branch** → do nothing. Follow-up iterations append commits to the same branch and update the same MR. Skip the rest of this step.

- **On `main` / `master`** → run the four pre-flight checks below first; only create the feature branch once all are clean.

### Pre-flight checks (only when starting from `main`)

1. **Pull latest `main`**:
   ```bash
   git pull --ff-only
   ```
   Non-fast-forward failure means local `main` has diverged — stop and surface the state to the user. Do not force-update or rebase silently.

2. **Local tests**:
   ```bash
   uv run pytest
   ```
   Fast smoke test. The full `tox` matrix still runs at `/tdd-finalizer` time.

3. **GitLab pipeline health on `main`**:
   ```bash
   glab ci status --branch main
   ```
   Treat `failed` / `canceled` on the latest pipeline as a failing check.

4. **Outstanding merge requests**:
   ```bash
   glab mr list --output json
   ```
   Any open MR in the repo (regardless of author) counts as outstanding.

**All four clean** → derive a kebab-case slug (≤40 chars) from the task description and create the branch:

```bash
git checkout -b claude/<slug>
```

**Any check fails or MRs are outstanding** → stop and ask the user to approve before continuing. Summarize what failed (test names, pipeline ID + URL, MR titles + URLs) so the decision is informed. Do not attempt to fix failures or close MRs as part of pre-flight — that's a separate task the user must direct.

If `glab` is unauthenticated or errors for tooling reasons, surface the error and stop; do not retry blindly.

`git pull` and the `glab` reads need network; run them in-sandbox first and only disable the sandbox on actual failure (per `CLAUDE.md` → "Sandbox"). The other commands are local-only and sandbox-safe.

## Step 2: Do the work

- **Non-trivial change** → route through the TDD agents (`/architect` → `/tdd-red` → `/tdd-green` → `/tdd-judge` → parallel judges → `/tdd-finalizer`). See `CLAUDE.md` → "Multi-Agent Workflow" for the routing.
- **Trivial change** (≤ ~10 lines, no behavior surprise, no new dependencies) → the main agent may edit + commit directly without spawning the TDD agents. Pre-commit hooks still run at commit time, so quality gates are not bypassed.

Either path lands at a local commit on the feature branch.

## Step 3: Publish (push + MR)

Run from the main thread, **outside the sandbox** (`dangerouslyDisableSandbox: true`). One user approval per remote-touching command.

1. **Sanity-check branch** (in-sandbox):
   ```bash
   git rev-parse --abbrev-ref HEAD
   ```
   If on `main` / `master`, stop — Step 1 should have prevented this. Never push directly to the default branch.

2. **Detect an existing MR for this branch** (in-sandbox; `glab` reads only):
   ```bash
   glab mr list --source-branch "$(git rev-parse --abbrev-ref HEAD)" --output json
   ```
   - `[]` → no MR yet, go to 3a.
   - Non-empty → MR exists, go to 3b.
   - `glab` unauthenticated or errors for tooling reasons → surface the error and stop. Do not retry blindly.

3a. **No existing MR — combined push + create** (sandbox disabled, single approval):
   - `--title`: `git log -1 --format=%s` (the commit subject)
   - `--description`: Summary section (commit body, or 2–3 bullets of what shipped) + Test plan section (commands reviewers can run, e.g. `uv run tox`)
   - `--target-branch`: `main` (or the default branch from `git symbolic-ref refs/remotes/origin/HEAD` if different)

   ```bash
   glab mr create \
     --push \
     --target-branch main \
     --title "<commit subject>" \
     --description "<built body>" \
     --remove-source-branch \
     --squash-before-merge \
     --yes
   ```
   `--push` pushes the branch first, then opens the MR. `--remove-source-branch` deletes the feature branch when the MR merges; `--squash-before-merge` collapses the branch's commits into one on merge. `--yes` skips glab's own confirmation, so the Claude permission prompt is the only approval.

3b. **MR exists — just push** (sandbox disabled):
   ```bash
   git push
   ```
   GitLab updates the existing MR with the new commits. Do not create a duplicate MR.

4. **Report** to the user: the MR URL (from `glab mr create`, or `glab mr view --web` in 3b), the final commit hash, and a one-line summary.

## Step 4: Return to `main`

After the MR is reported, switch back so the next piece of work starts clean:

```bash
git status            # confirm clean working tree
git checkout main
```

Local-only, sandbox-safe. Skip this only if the user explicitly says they want to stay on the feature branch (e.g., to keep iterating without re-checking out).

## When the lifecycle does not apply

- Read-only sessions (the user is asking questions, not asking for changes).
- Explicit user override ("don't branch", "don't open an MR", etc.) — honor it but mention what you're skipping.
