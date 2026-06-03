# Git Flow

Every change in this repo — full TDD workflow or one-off trivial edit — follows the same lifecycle: branch off `main`, do the work, commit, push, open or update a merge request via `glab`, then stay on the branch and return to `main` only once the MR has merged and the post-merge pipeline is green. **There is no direct-to-`main` path.** `/setup-repo` configures GitLab branch protection so pushes to `main` are rejected and merging requires approval; even a one-line typo fix must land through an MR. The main agent owns this lifecycle. **Do not spawn a subagent for any of the steps below** — all of it is short, local-only or single-command-remote, and trivially runs from the main thread. Subagents add latency and an extra approval surface for no benefit here.

## Step 1: Start a feature branch from clean `main`

> **This step runs first in *every* session that will make a change — a full TDD feature or a one-line trivial edit alike. It is not part of the TDD workflow; it gates it.** The most common and most damaging mistake is to skip it because "I'm already on a branch that looks right" and append to a branch whose work has already merged — that re-creates the deleted remote branch, re-introduces merged commits, and produces merge conflicts. Always verify the starting point *mechanically* (below); never infer it from the branch name.

Run from the main thread with Bash:

```bash
git rev-parse --abbrev-ref HEAD
```

- **Already on a `claude/<slug>` feature branch** → do **not** trust the branch name; verify *mechanically* that it still holds unfinished work for the current task before appending. Fetch first, then check whether the branch has already landed:
   ```bash
   git fetch origin --prune
   glab mr list --source-branch "$(git rev-parse --abbrev-ref HEAD)" --state all --output json
   ```
   - **A merged MR exists for this branch**, or `git log --oneline origin/main..HEAD` shows only commits whose content is already in `main` → the branch is **stale / already-landed**, even when its name still matches your topic. Switch back with `git checkout main` and start fresh below. **Never re-push a merged branch** — doing so re-creates the deleted remote branch and re-introduces already-merged commits, which is exactly what causes merge conflicts.
   - **Only an open MR — or no MR yet — for genuinely unfinished current-task work** → this is a real follow-up iteration: append commits to the same branch and update the same MR (Step 3b); skip the rest of this step.

   Never pile new or unrelated work onto a stale branch, and never assume a branch is live just because its name fits the task.

- **On `main` / `master`** → this is the expected starting point. Run the pre-flight checks below first; only create the feature branch once all are clean.

- **On any other branch** (detached `HEAD`, or a non-`claude/` branch) → stop and surface it. You are not at a known-good starting point; let the user say whether to switch to `main` first rather than guessing.

### Pre-flight checks (only when starting from `main`)

**First, confirm a clean working tree** — run `git status --porcelain` and require it to be empty. Uncommitted changes mean you are not at a clean starting point, and a dirty tree even aborts the `git pull --ff-only` below. If there are local changes, stop and surface them; let the user decide to commit, stash, or discard. **Never stash or discard silently.** Then run the four checks:

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

`git pull` and the `glab` reads need network; they run in-sandbox over HTTPS via the sandbox proxy, so no `dangerouslyDisableSandbox` is needed (see `CLAUDE.md` → "Git Remote"). The other commands are local-only and sandbox-safe.

## Step 2: Do the work

- **Non-trivial change** → route through the TDD agents (`/architect` → `/tdd-red` → `/tdd-green` → `/tdd-judge` → parallel judges → `/tdd-finalizer`). See `CLAUDE.md` → "Multi-Agent Workflow" for the routing.
- **Trivial change** (≤ ~10 lines, no behavior surprise, no new dependencies) → the main agent may edit + commit on the feature branch without spawning the TDD subagents. Pre-commit hooks still run at commit time, so quality gates are not bypassed.

Either path lands at a local commit on the feature branch. **Both paths then proceed to Step 3 (push + MR)** — the trivial shortcut skips the TDD subagents, not the merge request.

## Step 3: Publish (push + MR)

Run from the main thread, **in-sandbox** — the HTTPS remote (see `CLAUDE.md` → "Git Remote") routes through the sandbox's HTTPS proxy, so `dangerouslyDisableSandbox` is not used. One user approval per remote-touching command.

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

3a. **No existing MR — combined push + create** (single approval):
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

3b. **MR exists — just push**:
   ```bash
   git push
   ```
   GitLab updates the existing MR with the new commits. Do not create a duplicate MR.

4. **Report** to the user: the MR URL (from `glab mr create`, or `glab mr view --web` in 3b), the current local commit hash, and a one-line summary. Because the MR uses `--squash-before-merge`, GitLab collapses the branch into a **single new commit on `main` with a different hash** when it merges — so the hash you report now is the local branch tip, *not* the SHA that will land. Don't present it as the final merged hash; the canonical post-merge SHA only exists after Step 4 (read it then with `git log -1 --format=%H` on the updated `main` if the user needs it).

## Step 4: Stay on the branch until the MR merges (then return to `main`)

Do **not** return to `main` as soon as the MR is opened. The work is not done when the MR exists — it is done when a human reviewer has merged it and the resulting pipeline on `main` is green. AI must never approve or merge the MR itself (see `CLAUDE.md` → "Repo lock-down gate"), so "landing" always depends on a human.

After Step 3 reports the MR, **remain on the feature branch** and tell the user the workflow is paused pending (a) human review + merge of the MR, and (b) a green post-merge pipeline. Then:

1. **Do not busy-poll the remote.** Report and wait for the user to come back (e.g. "it's merged" or "continue"). When the user prompts you — or asks you to check — verify the MR state in-sandbox:
   ```bash
   glab mr view --output json    # state "merged" means it landed
   ```
   While it is still `opened`, there is nothing to return to — stay on the branch. Follow-up iterations (review feedback, fixes) append commits to this same branch and update the same MR per Step 1 / Step 3b.

2. **Confirm tests are green after the merge.** Once the MR is `merged`, check the pipeline on the default branch:
   ```bash
   glab ci status --branch main
   ```
   Treat anything other than a passing latest pipeline as "not landed yet" — surface the failing jobs to the user and stay engaged. Do not walk away from a red `main` you just merged into.

3. **Only once the MR is merged AND `main` is green**, switch back so the next piece of work starts from a clean, up-to-date `main`:
   ```bash
   git status            # confirm clean working tree
   git checkout main
   git pull --ff-only    # pick up the just-merged commit (the squashed SHA, not your branch tip)
   ```

4. **Tidy up** so the next start is clean (and Step 1's "clean starting point" checks pass without leftovers):
   ```bash
   git branch -d claude/<slug>   # delete the merged local branch (the remote copy was auto-removed by --remove-source-branch)
   git fetch --prune             # drop stale remote-tracking refs for branches deleted on the remote
   ```
   `git branch -d` is the **safe** delete — it refuses if the branch is not fully merged into `main`. If it complains, the MR did not actually land; investigate rather than forcing it with `-D`.

Local git is sandbox-safe; the `glab` reads and `git pull` run in-sandbox over the HTTPS proxy (see `CLAUDE.md` → "Git Remote").

Skip the wait only if the user explicitly says to — e.g. "open the MR and move on" or "stay on the branch and keep iterating." Honor the override, but state what you're skipping.

## When the lifecycle does not apply

- Read-only sessions (the user is asking questions, not asking for changes).
- Explicit user override to **defer publishing** ("don't push yet", "let me iterate locally first") — honor it, but mention that the change still needs an MR to land on `main` (branch protection makes this non-optional). The user can ask to skip Step 3 *for now*; they cannot ask to bypass the MR entirely.
