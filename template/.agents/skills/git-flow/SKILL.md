---
name: git-flow
description: Publish repository changes through a feature branch and human-reviewed GitLab merge request.
metadata:
  model-tier: balanced
---
<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# Git Flow

Every change must use a feature branch. Open or update an MR. A publication
failure blocks completion.

All work uses one flow. This includes full TDD work and trivial edits. Branch
from `main`, do the work, commit, push, then use `glab` to open or update an
MR. Publication hands work to a human reviewer. Waiting for a merge is not
part of the active run. It does not block unrelated work. **There is no
direct-to-`main` path.** Setup protects `main`. GitLab rejects pushes to it and
requires approval to merge. Even a one-line typo fix needs an MR. For coding
work, Phase 6 Finalizer owns the verified local commit. This main-thread skill
owns only the push and MR update. Content or trivial work keeps its normal
main-thread commit. **Do not spawn a subagent for the main-thread publication
steps below.** They are short, local-only, or one remote command.

For coding work, Phase-6 Finalizer may use `git commit --no-verify`. First, it
must mechanically match the staged tree and supported-version JUnit
digests/counts to the Phase-4 Judge receipt. This avoids rerunning the recorded
hook. The serial runner still uses ordinary `git commit -m`. It has no Judge
identity shortcut. Phase-8 post-push pipeline observation is another defense.
It never replaces Judge.

**A readiness or policy audit is not publication permission.** A Developer may
not be able to read Maintainer-only policy settings. That Developer may still
push a feature branch and create an MR. After normal work, always try the full
branch → commit → push → MR handoff. Only a Git push or MR-creation failure
blocks publication.

## GitLab target

Use the numeric project ID for every remote `glab` operation. Require
`GITLAB_HOST` and numeric `GITLAB_PROJECT_ID` from the environment. Never
derive a project path from the Git remote.

For the MR handoff, use `projects/$GITLAB_PROJECT_ID/merge_requests`. If the
API client cannot use a numeric project ID, use its configured project path.
Either way, try to create the MR after the push. Do not stop for an unrelated
preflight result.

## Git and GitLab access preflight

Before branching from `main` or publishing, prove both access paths with
read-only commands. For HTTPS Git, load the directory-scoped `GITLAB_TOKEN`
only at runtime. Configure Git with this checkout's
`src/agent-work-git-credential.sh`. It is the only credential helper for that
command. Do not print, inspect, or copy secret environment values. Do not use
a stored helper or SSH agent for this route.

Require a non-empty `GITLAB_HOST` and numeric `GITLAB_PROJECT_ID`. Then run
this from clean `main`:

```bash
credential_helper="$PWD/src/agent-work-git-credential.sh"
test -x "$credential_helper"
direnv exec . /usr/bin/env -u GIT_CONFIG_PARAMETERS \
  AGENT_WORK_GIT_CREDENTIAL_HELPER="$credential_helper" \
  GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false \
  GIT_CONFIG_COUNT=4 GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0= \
  GIT_CONFIG_KEY_1=credential.helper 'GIT_CONFIG_VALUE_1=!exec "$AGENT_WORK_GIT_CREDENTIAL_HELPER"' \
  GIT_CONFIG_KEY_2=credential.useHttpPath GIT_CONFIG_VALUE_2=true \
  GIT_CONFIG_KEY_3=core.askPass GIT_CONFIG_VALUE_3=/bin/false \
  git pull --ff-only
direnv exec . /usr/bin/env bash -c 'exec glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"'
```

`git pull --ff-only` checks the configured credential-free HTTPS origin,
network route, and Git credential. The `glab` request separately checks API
auth and access to the supplied host/project pair. Neither command assumes a
sandbox proxy. The helper emits `oauth2` and the token only through Git's
credential protocol. Never put either in a remote URL, saved Git config,
command report, or diagnostic.

When `.envrc` supplies `GITLAB_TOKEN`, run each API command through `direnv
exec . /usr/bin/env bash -c`. Variables then expand after the environment
loads. For example, use `direnv exec . /usr/bin/env bash -c 'exec glab api
--hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"'`. Use that numeric
endpoint to diagnose access. A path-based `404` does not prove that the
directory-scoped token lacks project access. `glab auth status` checks stored
logins only. It cannot check this route.

If either command fails, stop before branching or publishing. Report the
failed command and its non-secret error. Have the user repair the Git
route/credential or GitLab API config. This blocks completion. A working
`glab` call does not authorize Git, and the reverse is also true.

## Step 1: Start a feature branch from clean `main`

> **Run this step first in every session that will change work.** This applies
> to full TDD features and one-line trivial edits. It gates TDD; it is not part
> of TDD. Do not skip it because a branch looks right. Adding work to a merged
> branch can recreate a deleted remote branch, restore merged commits, and
> cause conflicts. Always check the start point *mechanically*. Never trust the
> branch name.

### Session boundary

At session start, inspect the current branch before using it for work. A serial
work-item runner task belongs to its required `agent/issue-<IID>` branch. Stay
on that prepared branch. Return control to the runner when done. Do not switch
that agentic checkout to `main` at the end of the worker session.

For a normal user session away from the default branch, decide first whether
the request is follow-up work for that branch and its open MR. If not, require
a clean tree. Then switch and refresh before the new task:

```bash
git status --porcelain
git switch main
git pull --ff-only
```

If it is related, use the mechanical branch and MR checks below. Never use
this decision to add unrelated work to a feature branch.

Derive the task's kebab-case slug (≤40 characters) and intended branch before
you check the current checkout:

```bash
intended_branch="clanker/<slug>"
```

Only the serial `work-item-runner` may use `agent/issue-<iid>`. It uses that
form in its configured dedicated checkout. It starts from the configured
default branch. A runner child must mechanically check that checkout. It then
continues its selected flow. Do not call `agent-work` again or create a branch.

The coding agent in that runner is not the main agent for this lifecycle. Its
runner-owned worker exception overrides this document's publication rules. It
may edit and validate uncommitted work-tree changes only. Then it returns one
delimited MR draft. Do not commit, push, create an MR, approve, or merge. The
trusted runner checks that draft. It performs publication.

```bash
git rev-parse --abbrev-ref HEAD | grep -E '^agent/issue-[1-9][0-9]*$'
git rev-parse --git-dir >/dev/null
git rev-parse --git-common-dir >/dev/null
project_json="$(glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID")"
expected_origin="$(jq -er '.http_url_to_repo' <<<"$project_json")"
test "$(git remote get-url origin)" = "$expected_origin"
configured_default_branch="$(jq -er '.default_branch' <<<"$project_json")"
git merge-base --is-ancestor "origin/$configured_default_branch" HEAD
```

Only the serial runner uses this checked path. A normal checkout that finds an
`agent/issue-<iid>` branch must refuse it. Do not create, reuse, or publish that
reserved form. The branch and its MR stay available at `awaiting_human`. GitLab
is the durable handoff record.

Run from the main thread with Bash:

```bash
git rev-parse --abbrev-ref HEAD
```

- **On a mechanically verified runner `agent/issue-<iid>` branch** → use the
  checks above. They prove the durable Git worktree, configured project origin,
  and configured default branch. Then continue the runner-selected phase on
  this branch. `agent-work loop run --apply` selected it. Never call
  `agent-work` recursively.

- **Already on a `clanker/<slug>` feature branch** → do **not** trust its name.
  Mechanically check that it still has unfinished work for this task before you
  add work. Fetch first. Then check whether the branch has landed:
   ```bash
   git fetch origin --prune
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=all&source_branch=$(git rev-parse --abbrev-ref HEAD)"
   ```
   - **A merged MR exists for this branch**, or `git log --oneline origin/main..HEAD` shows only commits whose content is already in `main` → the branch is **stale / already-landed**. This is true even if its name fits the topic. Switch back with `git checkout main`. Then start fresh below. **Never re-push a merged branch.** That recreates the deleted remote branch. It restores merged commits. It causes merge conflicts.
   - **Only an open MR — or no MR yet — for unfinished current-task work** → this is a real follow-up. Add commits to the same branch. Update the same MR through Step 3b. Skip the rest of this step.

   Never add new or unrelated work to a stale branch. Never assume a branch is
   live just because its name fits the task.

- **On `main` / `master`** → this is the expected start. Run the pre-flight
  checks below. Create the feature branch only after all pass.

- **On any other branch** (detached `HEAD`, or a branch outside the normal
  `clanker/` form) → stop and report it. The only exception is the mechanically
  checked `agent/issue-<iid>` runner branch above. Do not widen normal branch
  authority. This is not a known-good start. Let the user decide whether to
  switch to `main`. Do not guess.

### Pre-flight checks (only when starting from `main`)

**First, require a clean work tree.** Run `git status --porcelain`. Its output
must be empty. Uncommitted changes mean this is not a clean start. A dirty tree
also stops the `git pull --ff-only` below. If there are local changes, stop and
report them. Let the user choose to commit, stash, or discard them. **Never
stash or discard silently.** Then run the access preflight and these checks:

1. **Coding-lane baseline tests** — after classifying the whole intended
   change through the `$architect` router, run this only for the coding workflow:
   ```bash
   uv run pytest
   ```
   This is the fast coding baseline. The full matrix still runs at coding
   finalization. Documentation and agent-policy work uses content checks. It
   needs no test baseline.

2. **Open merge request awareness**:
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&per_page=100"
   ```
   Unrelated open MRs are **non-blocking**. Do not stop only because this query
   returns an MR. Stop for a real dependency on unmerged work, the same branch
   owned by another run, or a known file or resource conflict. A follow-up to
   an existing MR reuses its branch as described above.

3. **Branch-specific continuation from `main`**:
   ```bash
   git fetch origin --prune
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&source_branch=$intended_branch"
   git show-ref --verify --quiet "refs/heads/$intended_branch"
   ```
   An open MR for this task is a follow-up, not new work. Check out the local
   branch with `git checkout "$intended_branch"`. If only its remote-tracking
   branch exists, use `git checkout --track "origin/$intended_branch"`. Add the
   review fix. Update the existing MR through Step 3b. If that branch or MR
   belongs to another work item, stop for a same-branch conflict. If a local or
   remote branch exists without an open MR, mechanically check that it has
   unfinished work for this task before reuse. Never overwrite it or make a
   duplicate branch.

**All blocking checks pass and no branch exists for this task** → create the
intended branch:

```bash
git checkout -b "$intended_branch"
```

**A blocking check fails** → summarize the failure. Include test names,
dependencies, same-branch ownership, or resource conflicts. Fix it before
handoff. Other repairs need user direction when they would materially expand
scope. An unrelated open MR is not a failed check.

If `glab` lacks auth or has a tooling error, report it and stop. Do not retry
blindly.

`git pull` and `glab` reads need network. Use the normal environment route:
the sandbox proxy when present, or the VM's configured network route. Other
commands are local-only and sandbox-safe.

## Step 2: Do the work

- **Non-trivial change** → activate `$architect` and follow the live router.
- **Trivial change** (≤ ~10 lines, no behavior surprise, no new dependencies) →
  the main agent may edit and commit on the feature branch without TDD
  subagents. Pre-commit hooks still run at commit time. Quality gates remain.

Both paths make a local feature-branch commit. **Both then go to Step 3 (push +
MR).** The trivial path skips TDD subagents, not the MR.

## Step 3: Publish (push + MR)

Run this from the main thread. Use the same checked Git and GitLab path as the
preflight. Get one user approval for each remote-touching command.

1. **Sanity-check branch** (in-sandbox):
   ```bash
   git rev-parse --abbrev-ref HEAD
   ```
   If on `main` / `master`, stop. Step 1 should have prevented this. Never push
   directly to the default branch.

2. **Detect an existing MR for this branch** (in-sandbox; `glab` reads only):
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&source_branch=$(git rev-parse --abbrev-ref HEAD)"
   ```
   - `[]` → no MR yet, go to 3a.
   - Non-empty → MR exists, go to 3b.
   - `glab` unauthenticated or errors for tooling reasons → surface the error and stop. This is a blocker, never a completed outcome. Do not retry blindly.

3a. **No existing MR — push + create by project ID** (one approval per remote command):
   - `title`: `git log -1 --format=%s` (the commit subject)
   - `description`: concise problem, impact, requested action or intended
     outcome, and validation; state each fact once
   - `target_branch`: `main` (or the default branch returned by the GitLab target check)

   After the push, use the publisher in the active feature worktree. Set
   `active_worktree="$(git rev-parse --show-toplevel)"`. Then run the
   checkout-owned publisher boundary at
   `"$active_worktree/bin/agent-work-from-checkout"`. It renders real
   newlines from scalar fields. It selects the numeric project, creates or
   updates the MR, and reads it back exactly. Do not construct a multiline
   shell variable or call GitLab's create/update endpoint directly.
   For ordinary issue-backed work, pass the already validated positive issue
   IID with `--issue-iid`. The boundary renders exactly one `Closes #IID` and
   read-back verifies that exact issue in the configured project. Never derive
   the IID from issue or MR text. Issue-less publication may omit this option.

   ```bash
   source_branch="$(git rev-parse --abbrev-ref HEAD)"
   target_branch="main"
   title="$(git log -1 --format=%s)"
   active_worktree="$(git rev-parse --show-toplevel)"
   issue_iid="<validated-positive-issue-IID>"
   [[ "$issue_iid" =~ ^[1-9][0-9]*$ ]] || {
     printf '%s\n' "A validated positive issue IID is required" >&2
     exit 1
   }
   git push --set-upstream origin HEAD
   "$active_worktree/bin/agent-work-from-checkout" merge-request publish \
     --source-branch "$source_branch" --target-branch "$target_branch" --title "$title" \
     --problem "Review handoffs were hard to scan." \
     --impact "Reviewers could miss the action needed to move work forward." \
     --outcome "Make the MR description concise and nonduplicative." \
     --validation "git diff --check" --issue-iid "$issue_iid" --apply
   ```
   Publication checks the configured-project issue, source and target branches,
   title, and Markdown returned by GitLab. A failure blocks completion. Use
   free-form fields and `--tracking` only for non-closing relationships. They
   must not contain a GitLab closing pattern. This includes `Closes`, `Fixes`,
   `Resolves`, Implement variants, and issue/work-item URLs. `--issue-iid` is
   the only closing association.

   Keep role receipts, result envelopes, full correction ledgers, and duplicated
   summaries out of the human-facing description; retain them in durable audit
   evidence.

   A Finalizer draft-safety verdict is the only exception to the normal title
   shape: publish a mechanically safe but unresolved proposal with a title that
   begins `Draft:`. Its concise description identifies the specific human
   decision and requested action, its impact, and enough validation to review
   it. Keep detailed findings, receipts, and correction-cycle evidence in the
   durable audit. Use Draft only when a real human decision remains. If no
   actionable human decision remains and an approved-scope correction exists,
   correct the work and publish a normal MR. This is a handoff for human
   judgment, never approval to merge. Do not publish when the verdict reports
   unsafe/unvalidated content or ambiguous publication authority.

3b. **MR exists — push, then preserve or update its draft handoff**:
   ```bash
   source_branch="$(git rev-parse --abbrev-ref HEAD)"
   selected_mr_json="$(glab api --hostname "$GITLAB_HOST" \
     "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&source_branch=$source_branch" \
     | jq -cer 'if length == 1 and (.[0].iid | type == "number") then .[0] else error("expected exactly one open MR with a numeric IID") end')" || {
     printf '%s\n' "Could not select exactly one open MR with a numeric IID" >&2
     exit 1
   }
   mr_iid="$(jq -er 'if (.iid | type) == "number" then .iid else error("selected MR IID must be a number") end' <<<"$selected_mr_json")" || {
     printf '%s\n' "Selected MR did not return a numeric IID" >&2
     exit 1
   }
   git push
   read_back_mr_json="$(glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests/$mr_iid")"
   if ! jq -e --arg iid "$mr_iid" --argjson intended "$selected_mr_json" \
     '(.iid | type) == "number" and (.source_branch | type) == "string" and (.target_branch | type) == "string" and (.title | type) == "string" and (.description | type) == "string" and (.iid | tostring) == $iid and .source_branch == $intended.source_branch and .target_branch == $intended.target_branch and .title == $intended.title and .description == $intended.description' \
     <<<"$read_back_mr_json" >/dev/null; then
     printf '%s\n' "MR read-back did not match the intended IID, branches, title, or Markdown" >&2
     exit 1
   fi
   ```
   GitLab updates the existing MR with new commits. To replace its title or
   description, set `active_worktree="$(git rev-parse --show-toplevel)"` in
   this active feature worktree. Then use
   `"$active_worktree/bin/agent-work-from-checkout" merge-request publish`
   with scalar Problem, Outcome, and Validation fields after this push. It
   updates that source branch and verifies the returned metadata. For
   issue-backed updates,
   pass the validated `--issue-iid`; free-form fields and `--tracking` must
   contain no GitLab closing pattern.
   For normal approval,
   preserve its existing title and description only when they remain current
   and comply with the concise contract; after the shown push and read-back,
   stop when they do. Otherwise, update that same MR once after that verified
   push, capture the intended replacement title and description before the
   update, then read it back and verify those exact values, its identity, and
   its branches. This preserves trailing newlines in the description. For an
   accepted Finalizer draft-safety verdict, first use the verified
   numeric-project MR identity,
   preserve required existing content and closing references, and update that
   same MR so its title begins `Draft:` and its description states
   the specific human decision and requested action, impact, and enough
   validation to review it. Keep detailed findings, receipts, and
   correction-cycle evidence in the durable audit. Read the same MR back and
   verify its identity, explicitly intended title, explicitly intended
   description, source branch, and target branch.
   Capture those intended values before any separately authorized draft update
   and compare them to the exact read-back. If it cannot
   be safely relabeled, described, or verified, return `needs_human`; never
   create a duplicate MR. A Draft: handoff is never approval to merge.

If `git push` or merge-request creation fails, stop and report the non-secret
error and required next action. The failure blocks the work; do not call local
edits or a local commit complete.

4. **Report** to the user: the `web_url` from the API response (or the
   existing-MR lookup), the current local commit hash, and a one-line summary.
   Because the MR uses `squash=true`, GitLab collapses the branch into a
   **single new commit on `main` with a different hash** when it merges. The
   reported hash is the branch tip, not the future merged SHA.

## Step 4: Hand off without waiting for merge

After publication, ordinary interactive work delegates and completes the
Fast `$pipeline-performance-judge` before reporting `awaiting_human`; serial
work invokes that judge in a fresh context after its cheap exact-SHA gate.
AI must never approve or merge the MR itself, so human review and merge
determine whether the change lands. They do not prevent unrelated work.

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

3. **Continue a branch-specific review fix.** Re-fetch the open MR by its
   source branch, verify that the remote branch tip matches the intended work,
   make the requested correction on that same branch, and update the existing
   MR through Step 3b. Never create a second MR for the same review iteration.

4. **Clean up only after merge confirmation.** Only once the MR is merged may
   the normal checkout update `main` and remove the local branch:
   ```bash
   git checkout main
   git pull --ff-only
   git branch -D clanker/<slug>
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
