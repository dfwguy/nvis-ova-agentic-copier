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

Every repository change must publish a feature branch and open or update a
merge request. A publication failure is a blocker, not completion.

Every change in this repo — full TDD workflow or one-off trivial edit — follows the same lifecycle: branch off `main`, do the work, commit, push, and open or update a merge request via `glab`. Publication hands the work to a human reviewer; waiting for merge is not part of the active run and does not block unrelated work. **There is no direct-to-`main` path.** The repository setup procedure configures GitLab branch protection so pushes to `main` are rejected and merging requires approval; even a one-line typo fix must land through an MR. For coding work, Phase 6 Finalizer owns the verified local commit and this main-thread skill owns only push and MR update. Content/trivial work retains its ordinary main-thread commit. **Do not spawn a subagent for the main-thread publication steps below** — they are short, local-only or single-command-remote.

For a coding workflow, Phase-6 Finalizer commits with `git commit --no-verify`
only after mechanically matching the staged tree and supported-version JUnit
digests/counts to the Phase-4 Judge receipt. This avoids rerunning the
already-recorded hook. The serial runner's repository commit remains ordinary
`git commit -m`; it has no Judge identity shortcut. Phase-8 post-push pipeline
observation is an independent defense, never a substitute for Judge.

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
paths with read-only commands. For HTTPS Git, load the directory-scoped
`GITLAB_TOKEN` only at runtime and configure Git with this checkout's
`src/agent-work-git-credential.sh`; it is the sole credential helper for
that command. Do not print, inspect, or copy secret environment-variable
values, and do not use a stored helper or SSH agent for this route.

First require a non-empty `GITLAB_HOST` and a numeric `GITLAB_PROJECT_ID`.
Then, from a clean `main`, run:

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

`git pull --ff-only` verifies the configured credential-free HTTPS origin,
network route, and Git credential, while the `glab` request independently
verifies API authentication and that the supplied host/project pair is
accessible. Neither command assumes a sandbox proxy. The helper emits `oauth2`
and the token only through Git's credential protocol; never put either in a
remote URL, persisted Git configuration, command report, or diagnostic.

When a checkout supplies `GITLAB_TOKEN` through `.envrc`, run every API
command through `direnv exec . /usr/bin/env bash -c` so variables expand after
the environment loads (for example, `direnv exec . /usr/bin/env bash -c 'exec
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"'`). Use that numeric
endpoint to diagnose access: a path-based `404` is not evidence that the
directory-scoped token lacks project access. `glab auth status` checks only
stored logins and cannot verify this route.

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
intended_branch="clanker/<slug>"
```

The `agent/issue-<iid>` form is reserved for the serial `work-item-runner` in
its configured dedicated checkout. Its starting point is the configured
default branch. A runner child must mechanically verify that checkout, then
continue its already-selected workflow without recursively invoking
`agent-work` or creating another branch:

The coding agent inside that runner is not the main agent for this lifecycle.
Its runner-owned worker exception takes precedence over the publication
requirements in this document: edit and validate only uncommitted working-tree
changes, then return exactly one delimited merge-request draft. Do not commit,
push, create a merge request, approve, or merge. The trusted runner alone
performs the publication handoff after it validates that draft.

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

Only the serial runner uses this verified path. An ordinary checkout that
finds an `agent/issue-<iid>` branch must refuse it rather than create, reuse, or
publish that reserved form. The branch and its merge request remain available
at `awaiting_human`; GitLab is the durable handoff record.

Run from the main thread with Bash:

```bash
git rev-parse --abbrev-ref HEAD
```

- **On a mechanically verified runner `agent/issue-<iid>` branch** → use the
  checks above to prove the durable Git worktree, configured project
  origin, and configured default branch. Then continue the runner-selected
  workflow phase on this branch. `agent-work loop run --apply` is the parent
  invocation that selected it; never invoke `agent-work` recursively.

- **Already on a `clanker/<slug>` feature branch** → do **not** trust the branch name; verify *mechanically* that it still holds unfinished work for the current task before appending. Fetch first, then check whether the branch has already landed:
   ```bash
   git fetch origin --prune
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=all&source_branch=$(git rev-parse --abbrev-ref HEAD)"
   ```
   - **A merged MR exists for this branch**, or `git log --oneline origin/main..HEAD` shows only commits whose content is already in `main` → the branch is **stale / already-landed**, even when its name still matches your topic. Switch back with `git checkout main` and start fresh below. **Never re-push a merged branch** — doing so re-creates the deleted remote branch and re-introduces already-merged commits, which is exactly what causes merge conflicts.
   - **Only an open MR — or no MR yet — for genuinely unfinished current-task work** → this is a real follow-up iteration: append commits to the same branch and update the same MR (Step 3b); skip the rest of this step.

   Never pile new or unrelated work onto a stale branch, and never assume a branch is live just because its name fits the task.

- **On `main` / `master`** → this is the expected starting point. Run the pre-flight checks below first; only create the feature branch once all are clean.

- **On any other branch** (detached `HEAD`, or a branch outside the ordinary
  `clanker/` convention) → stop and surface it. The sole exception is the
  mechanically verified reserved `agent/issue-<iid>` runner branch above; do
  not widen ordinary branch authority. You are not at a known-good starting
  point; let the user say whether to switch to `main` first rather than guessing.

### Pre-flight checks (only when starting from `main`)

**First, confirm a clean working tree** — run `git status --porcelain` and require it to be empty. Uncommitted changes mean you are not at a clean starting point, and a dirty tree even aborts the `git pull --ff-only` below. If there are local changes, stop and surface them; let the user decide to commit, stash, or discard. **Never stash or discard silently.** Then run the access preflight above and the remaining checks:

1. **Coding-lane baseline tests** — after classifying the whole intended
   change through the `$architect` router, run this only for the coding workflow:
   ```bash
   uv run pytest
   ```
   This is the fast coding baseline; the full matrix still runs at coding
   finalization. Documentation and agent-policy work uses its content checks
   and does not require a test baseline.

2. **Open merge request awareness**:
   ```bash
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&per_page=100"
   ```
   Unrelated open merge requests are **non-blocking**. Do not stop merely
   because this query returns an MR. Stop only for a concrete dependency on
   unmerged work, the same branch belonging to another run, or a known file or
   resource conflict. A continuation of an existing MR reuses its branch as
   described above.

3. **Branch-specific continuation from `main`**:
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

**A blocking check fails** → summarize the failure (test names, dependency,
same-branch ownership, or resource conflict) and remediate it before handoff.
Other failures still require user direction when their repair would materially
expand scope. An unrelated open MR is not a failed check.

If `glab` is unauthenticated or errors for tooling reasons, surface the error and stop; do not retry blindly.

`git pull` and the `glab` reads need network. Use the environment's normal
working route: the sandbox proxy where it exists, or the VM's configured
network path otherwise. The other commands are local-only and sandbox-safe.

## Step 2: Do the work

- **Non-trivial change** → activate `$architect` and follow the live router.
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
   - `description`: concise problem, impact, requested action or intended
     outcome, and validation; state each fact once
   - `target_branch`: `main` (or the default branch returned by the GitLab target check)

   Use the executable publication boundary after the push. It renders real
   newlines from scalar fields and performs the numeric-project selection,
   create-or-update, and exact authoritative read-back. Do not construct a
   multiline shell variable or call GitLab's create/update endpoint directly.
   For ordinary issue-backed work, pass the already validated positive issue
   IID with `--issue-iid`. The boundary renders exactly one `Closes #IID` and
   read-back verifies that exact issue in the configured project. Never derive
   the IID from issue or MR text. Issue-less publication may omit this option.

   ```bash
   source_branch="$(git rev-parse --abbrev-ref HEAD)"
   target_branch="main"
   title="$(git log -1 --format=%s)"
   issue_iid="<validated-positive-issue-IID>"
   [[ "$issue_iid" =~ ^[1-9][0-9]*$ ]] || {
     printf '%s\n' "A validated positive issue IID is required" >&2
     exit 1
   }
   git push --set-upstream origin HEAD
   agent-work merge-request publish \
     --source-branch "$source_branch" --target-branch "$target_branch" --title "$title" \
     --problem "Review handoffs were hard to scan." \
     --impact "Reviewers could miss the action needed to move work forward." \
     --outcome "Make the MR description concise and nonduplicative." \
     --validation "git diff --check" --issue-iid "$issue_iid" --apply
   ```
   Publication verifies the configured-project issue, source and target
   branches, title, and Markdown returned by GitLab. A failure is a blocker.
   Use free-form structured fields and `--tracking` only for non-closing
   relationships. They must contain no GitLab closing pattern, including
   `Closes`, `Fixes`, `Resolves`, Implement variants, or issue/work-item URLs.
   `--issue-iid` is the sole closing association.

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
   description, use `agent-work merge-request publish` with scalar Problem,
   Outcome, and Validation fields after this push; it updates exactly that
   source branch and verifies the returned metadata. For issue-backed updates,
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
