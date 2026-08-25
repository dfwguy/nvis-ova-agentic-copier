# Git Flow

Every repository change must publish a feature branch and open or update a
merge request. A publication failure is a blocker, not completion.

Every change in this repo — full TDD workflow or one-off trivial edit — follows the same lifecycle: branch off `main`, do the work, commit, push, and open or update a merge request via `glab`. Publication hands the work to a human reviewer; waiting for merge is not part of the active run and does not block unrelated work. **There is no direct-to-`main` path.** `/setup-repo` configures GitLab branch protection so pushes to `main` are rejected and merging requires approval; even a one-line typo fix must land through an MR. The main agent owns this lifecycle. **Do not spawn a subagent for any of the steps below** — all of it is short, local-only or single-command-remote, and trivially runs from the main thread. Subagents add latency and an extra approval surface for no benefit here.

**Do not confuse a readiness or policy audit with publication permission.** A
Developer may be unable to read Maintainer-only policy settings and still be
able to push a feature branch and create an MR. After normal work completes,
always attempt the full branch → commit → push → MR handoff. Only an actual
Git push or MR-creation failure blocks publication.

## GitLab target

Every remote `glab` operation uses the numeric project ID. Require
`GITLAB_HOST` and numeric `GITLAB_PROJECT_ID` from the environment; never
derive a project path from the Git remote.

For the MR handoff, use the numeric API endpoint
`projects/$GITLAB_PROJECT_ID/merge_requests`. If the available API client
cannot address a numeric project ID, use its already-configured project path;
either way, attempt MR creation after the push rather than stopping on an
unrelated preflight result.

## Git and GitLab access preflight

Before branching from `main` or publishing, prove the two independent access
paths with read-only commands. This works whether credentials come from
directory-scoped environment variables (for example, `GITLAB_TOKEN`), a
stored `glab` login, an SSH agent, or an existing Git credential helper. Do
not print, inspect, or copy secret environment-variable values.

First require a non-empty `GITLAB_HOST` and a numeric `GITLAB_PROJECT_ID`.
Then, from a clean `main`, run:

```bash
git pull --ff-only
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
```

`git pull --ff-only` verifies the configured Git remote, network route, and
Git credential, while the `glab` request verifies API authentication and that
the supplied host/project pair is accessible. Neither command assumes a
sandbox proxy. Do not change a working remote or credential configuration just
because it differs from another developer's VM.

If either command fails, stop before creating a branch or publishing. Report
which command failed and its non-secret error, then have the user repair the
appropriate Git route/credential or GitLab API configuration. This is a
blocker, never a completed outcome. Do not infer that a successful
`glab` call also authorizes Git, or vice versa.

## Step 1: Start a feature branch from clean `main`

> **This step runs first in *every* session that will make a change — a full TDD feature or a one-line trivial edit alike. It is not part of the TDD workflow; it gates it.** The most common and most damaging mistake is to skip it because "I'm already on a branch that looks right" and append to a branch whose work has already merged — that re-creates the deleted remote branch, re-introduces merged commits, and produces merge conflicts. Always verify the starting point *mechanically* (below); never infer it from the branch name.

### Session boundary

At the start of a session, inspect the current branch before treating it as a
work branch. A task dispatched by the serial work-item runner is explicitly
related to its required `agent/issue-<IID>` branch; stay on that prepared branch
and return control to the runner when the task is done. Do not switch that
agentic checkout to `main` at the end of the worker session.

For an ordinary user session that starts away from the default branch, first
decide whether the request is genuinely follow-up work for that branch and its
open merge request. If it is unrelated, require a clean tree, then switch and
refresh before beginning the new task:

```bash
git status --porcelain
git switch main
git pull --ff-only
```

If it is related, use the mechanical branch and merge-request checks below.
Never use this decision to append unrelated work to a feature branch.

Derive the task's kebab-case slug (≤40 characters) and intended branch before
checking the current checkout:

```bash
intended_branch="claude/<slug>"
```

The `agent/issue-<iid>` form is reserved for the serial `work-item-runner` in
its configured dedicated checkout. Its starting point is the configured
default branch. A runner child must mechanically verify that checkout, then
continue its already-selected workflow without recursively invoking
`agent-work` or creating another branch:

```bash
git rev-parse --abbrev-ref HEAD | grep -E '^agent/issue-[1-9][0-9]*$'
test -d "$(git rev-parse --show-toplevel)/.git"
project_json="$(glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID")"
expected_origin="$(jq -er '.http_url_to_repo' <<<"$project_json")"
test "$(git remote get-url origin)" = "$expected_origin"
configured_default_branch="$(jq -er '.default_branch' <<<"$project_json")"
git merge-base --is-ancestor "origin/$configured_default_branch" HEAD
```

Only the serial runner uses this verified path. An ordinary checkout that
finds an `agent/issue-<iid>` branch must refuse it rather than create, reuse, or
publish that reserved form. The branch and its merge request remain available
at `awaiting_human`; GitLab is the durable handoff record.

Run from the main thread with Bash:

```bash
git rev-parse --abbrev-ref HEAD
```

- **On a mechanically verified runner `agent/issue-<iid>` branch** → use the
  checks above to prove the dedicated `.git` directory, configured project
  origin, and configured default branch. Then continue the runner-selected
  workflow on this branch. `agent-work run-next --apply` is the parent
  invocation that selected it; never invoke `agent-work` recursively.

- **Already on a `claude/<slug>` feature branch** → do **not** trust the branch name; verify *mechanically* that it still holds unfinished work for the current task before appending. Fetch first, then check whether the branch has already landed:
   ```bash
   git fetch origin --prune
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=all&source_branch=$(git rev-parse --abbrev-ref HEAD)"
   ```
   - **A merged MR exists for this branch**, or `git log --oneline origin/main..HEAD` shows only commits whose content is already in `main` → the branch is **stale / already-landed**, even when its name still matches your topic. Switch back with `git checkout main` and start fresh below. **Never re-push a merged branch** — doing so re-creates the deleted remote branch and re-introduces already-merged commits, which is exactly what causes merge conflicts.
   - **Only an open MR — or no MR yet — for genuinely unfinished current-task work** → this is a real follow-up iteration: append commits to the same branch and update the same MR (Step 3b); skip the rest of this step.

   Never pile new or unrelated work onto a stale branch, and never assume a branch is live just because its name fits the task.

- **On `main` / `master`** → this is the expected starting point. Run the pre-flight checks below first; only create the feature branch once all are clean.

- **On any other branch** (detached `HEAD`, or a non-`claude/` branch) → stop and surface it. You are not at a known-good starting point; let the user say whether to switch to `main` first rather than guessing.

### Pre-flight checks (only when starting from `main`)

**First, confirm a clean working tree** — run `git status --porcelain` and require it to be empty. Uncommitted changes mean you are not at a clean starting point, and a dirty tree even aborts the `git pull --ff-only` below. If there are local changes, stop and surface them; let the user decide to commit, stash, or discard. **Never stash or discard silently.** Then run the access preflight above and the remaining checks:

1. **Local tests**:
   ```bash
   uv run pytest
   ```
   Fast smoke test. The full `tox` matrix still runs at `/tdd-finalizer` time.

2. **GitLab pipeline health on `main`**:
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/pipelines?ref=main&per_page=1"
   ```
   Treat `failed` / `canceled` on the latest pipeline as a failing check.

3. **Open merge request awareness**:
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&per_page=100"
   ```
   Unrelated open merge requests are **non-blocking**. Do not stop merely
   because this query returns an MR. Stop only for a concrete dependency on
   unmerged work, the same branch belonging to another run, or a known file or
   resource conflict. A continuation of an existing MR reuses its branch as
   described above.

4. **Branch-specific continuation from `main`**:
   ```bash
   git fetch origin --prune
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&source_branch=$intended_branch"
   git show-ref --verify --quiet "refs/heads/$intended_branch"
   ```
   An open MR for the same task is a follow-up iteration, not new work. Check
   out the existing local branch with `git checkout "$intended_branch"`; if
   only its remote-tracking branch exists, use
   `git checkout --track "origin/$intended_branch"`. Then append the review
   fix and update the existing MR through Step 3b. If that branch or MR belongs
   to another work item, stop for a same-branch conflict. If a local or remote
   branch exists without an open MR, verify mechanically whether it is
   genuinely unfinished current-task work before reusing it; never overwrite
   it or create a duplicate branch.

**All blocking checks clean and no current-task branch exists** → create the intended branch:

```bash
git checkout -b "$intended_branch"
```

**A blocking check fails** → summarize the failure (test names, pipeline ID + URL, dependency, same-branch ownership, or resource conflict) and remediate it before handoff. A failed or canceled latest pipeline is actionable: diagnose it, then either include its fix in the current feature MR when it is compatible with the requested work or open a dedicated pipeline-repair MR. Do not leave a failed pipeline merely reported. Other failures still require user direction when their repair would materially expand scope. An unrelated open MR is not a failed check.

If `glab` is unauthenticated or errors for tooling reasons, surface the error and stop; do not retry blindly.

`git pull` and the `glab` reads need network. Use the environment's normal
working route: the sandbox proxy where it exists, or the VM's configured
network path otherwise. The other commands are local-only and sandbox-safe.

## Step 2: Do the work

- **Non-trivial change** → route through the TDD agents (`/architect` → `/tdd-red` → `/tdd-green` → `/tdd-judge` → parallel judges → `/tdd-finalizer`). See `CLAUDE.md` → "Multi-Agent Workflow" for the routing.
- **Trivial change** (≤ ~10 lines, no behavior surprise, no new dependencies) → the main agent may edit + commit on the feature branch without spawning the TDD subagents. Pre-commit hooks still run at commit time, so quality gates are not bypassed.

Either path lands at a local commit on the feature branch. **Both paths then proceed to Step 3 (push + MR)** — the trivial shortcut skips the TDD subagents, not the merge request.

## Step 3: Publish (push + MR)

Run from the main thread using the same verified Git and GitLab access path as
the preflight. One user approval per remote-touching command.

1. **Sanity-check branch** (in-sandbox):
   ```bash
   git rev-parse --abbrev-ref HEAD
   ```
   If on `main` / `master`, stop — Step 1 should have prevented this. Never push directly to the default branch.

2. **Detect an existing MR for this branch** (in-sandbox; `glab` reads only):
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&source_branch=$(git rev-parse --abbrev-ref HEAD)"
   ```
   - `[]` → no MR yet, go to 3a.
   - Non-empty → MR exists, go to 3b.
   - `glab` unauthenticated or errors for tooling reasons → surface the error and stop. This is a blocker, never a completed outcome. Do not retry blindly.

3a. **No existing MR — push + create by project ID** (one approval per remote command):
   - `title`: `git log -1 --format=%s` (the commit subject)
   - `description`: Summary section (commit body, or 2–3 bullets of what shipped) + Test plan section (commands reviewers can run, e.g. `uv run tox`)
   - `target_branch`: `main` (or the default branch returned by the GitLab target check)

   ```bash
   git push --set-upstream origin HEAD
   glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/merge_requests" \
     -f source_branch="$(git rev-parse --abbrev-ref HEAD)" \
     -f target_branch="main" \
     -f title="<commit subject>" \
     -f description="<built body>" \
     -f remove_source_branch=true \
     -f squash=true
   ```
   `remove_source_branch=true` deletes the feature branch when the MR merges;
   `squash=true` collapses the branch's commits into one on merge.

3b. **MR exists — just push**:
   ```bash
   git push
   ```
   GitLab updates the existing MR with the new commits. Do not create a duplicate MR.

If `git push` or merge-request creation fails, stop and report the non-secret
error and required next action. The failure blocks the work; do not call local
edits or a local commit complete.

4. **Report** to the user: the `web_url` from the API response (or the existing-MR lookup), the current local commit hash, and a one-line summary. Because the MR uses `squash=true`, GitLab collapses the branch into a **single new commit on `main` with a different hash** when it merges — so the hash you report now is the local branch tip, *not* the SHA that will land. Do not present it as the final merged hash; the canonical post-merge SHA is available only after later reconciliation.

## Step 4: Hand off without waiting for merge

After Step 3, report the result as `awaiting_human` and end the active run. AI must never approve or merge the MR itself
(see `CLAUDE.md` → "Repo lock-down gate"), so human review and merge still determine whether the change lands.
They do not prevent another unrelated work item from starting.

1. **Release the active checkout at the correct session boundary.** A serial-runner worker on an
   `agent/issue-<IID>` branch ends immediately after returning its merge-request
   draft; the trusted runner owns publication and preserves that branch for the
   human handoff. In a normal user session, query the branch-specific MR first.
   Only after GitLab confirms that it is `merged` may a clean checkout return to
   an up-to-date `main`:
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=all&source_branch=<feature-branch>"
   git status
   git switch main
   git pull --ff-only
   ```
   If the MR is still open or is not merged, report `awaiting_human` and end the
   session without switching branches. Never infer that publication means merge.
   Never stash or discard changes silently. The serial runner keeps the issue
   branch and MR for review; after a human merge, an operator explicitly returns
   its dedicated checkout to `main` before launching another bounded pass.

2. **Retain the feature branch for feedback.** Do not delete it merely because
   an MR exists. Review fixes return to the same branch and update the same MR
   through Step 3b. Unrelated open merge requests remain non-blocking unless
   there is a same-branch conflict, declared dependency, or concrete resource
   conflict.

3. **Reconcile later without busy-polling.** When the user asks for status, or
   a bounded reconciliation job revisits the item, query its branch-specific
   MR and then the latest default-branch pipeline if it merged:
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=all&source_branch=<feature-branch>"
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/pipelines?ref=main&per_page=1"
   ```
   An open MR remains `awaiting_human`. A merged MR is not declared fully
   landed until the latest `main` pipeline passes. A failed post-merge pipeline
   is reported for remediation, but it does not block unrelated work.

4. **Clean up only after merge confirmation.** Only once the MR is merged and `main` is green may reconciliation update `main` and remove the local branch:
   ```bash
   git checkout main
   git pull --ff-only
   git branch -D claude/<slug>
   git fetch --prune
   ```
   Use `-D`, not `-d`, because squash merge creates a different commit on
   `main`. Never run the destructive branch deletion before the numeric-project
   MR query confirms `merged`; a closed or open MR may still contain work that
   must be preserved.

Local git is sandbox-safe; use the environment's verified network path for
`glab` reads and `git pull`.

## When the lifecycle does not apply

Only strictly read-only inspection (the user is asking questions and no
repository state changes) is outside this lifecycle. Every repository mutation
must use a feature branch and finish Step 3 by opening or updating an MR. A
commit, authentication, push, or MR-creation failure blocks the work; it is
never a completed outcome.
