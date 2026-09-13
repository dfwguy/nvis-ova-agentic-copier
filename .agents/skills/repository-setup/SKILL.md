---
name: repository-setup
description: Lock down an unprotected GitLab repository by verifying the user, protecting its default branch, requiring approval, and publishing the bootstrap merge request.
metadata:
  model-tier: most capable
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

# Repository setup

Use this skill only when
`grep -Fq 'All changes go through a merge request.' CONTRIBUTING.md` fails.
It is the sole permitted repository mutation until its bootstrap merge request
is published. Do not make unrelated changes, approve, or merge.

Configures GitLab project-level protections so the default branch cannot be pushed to directly and merge requests require approval. Interviews the user on whether to block self-approval by authors and committers (recommended for multi-user repos; opt out for single-user repos where there is no second reviewer). Writes `CONTRIBUTING.md` with the marker that ends this setup flow.

It must create or reuse only its dedicated
feature branch, configure project protections and approval policy, and publish
the `CONTRIBUTING.md` change through an MR. It must not make unrelated changes,
approve, or merge. A run that makes the bootstrap change returns
`awaiting_human` after the MR is opened or updated, then ends without waiting
for merge. A no-diff idempotent verification makes no repository mutation and
reports that no MR action is required.

**Run from the main thread. Do not spawn a subagent — every step is a single command.**

All `glab` calls are in-sandbox HTTPS via the proxy; no `dangerouslyDisableSandbox` needed.

## Procedure

### 1. Verify the GitLab user and resolve the project

Before changing GitLab settings, verify that the current directory's
credentials identify a GitLab user:

```bash
direnv exec . /usr/bin/env bash -c 'exec glab api user' | jq '{name, email, public_email, username}'
```

Require a successful response with a non-empty `username`. If the command
fails or `username` is null or empty, stop and report that no authenticated
GitLab user was verified. Do not create a branch, change project settings, or
write `CONTRIBUTING.md`.

```bash
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
```

Require `GITLAB_HOST` and a numeric `GITLAB_PROJECT_ID` from the environment;
never derive a project path from the Git remote. Confirm the returned `id`
matches `GITLAB_PROJECT_ID` and extract `default_branch` from the JSON. If the
call errors, stop and surface the error.

### 2. Prepare the dedicated bootstrap branch

Require a clean default branch: a clean working tree and an up-to-date default
branch before changing GitLab settings or writing `CONTRIBUTING.md`:

```bash
git status --porcelain
git rev-parse --abbrev-ref HEAD
git pull --ff-only
git fetch origin --prune
setup_branch="claude/setup-repo"
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=all&source_branch=claude/setup-repo"
git show-ref --verify --quiet "refs/heads/$setup_branch"
git show-ref --verify --quiet "refs/remotes/origin/$setup_branch"
```

The current branch must already be `<default_branch>` before `git pull`; if it
is not, or if the tree is dirty or a preflight fails, stop and report the exact
blocker. The two `git show-ref` commands are independent presence checks; exit
status 1 means that ref is absent, not that the preflight failed. Never silently
check out a branch, stash, discard, or append setup work to another branch.

Resolve the state before changing GitLab settings:

- If `CONTRIBUTING.md` exists on the default branch and no setup MR is open,
  remain on the default branch. Proceed only with idempotent settings
  verification or repair in steps 3–5, then report the settings-only result.
  Historical merged or closed setup MRs and abandoned setup refs do not block
  this no-repository-change path.
- If an open setup MR exists, require its source branch to match the resolved
  local or remote ref. Then create or reuse its worktree by checking out the
  local branch or tracking the remote branch, verify that it contains only the
  expected setup work, and update the same MR.
- If `CONTRIBUTING.md` is missing and no setup MR is open, any existing local
  or remote setup ref or any historical merged or closed setup MR is ambiguous.
  Stop before changing GitLab settings and ask for a human decision; never
  recreate, overwrite, or diverge it.
- If `CONTRIBUTING.md` is missing and no setup branch or MR has ever existed,
  create the dedicated branch from the clean default branch:

  ```bash
  git checkout -b "$setup_branch"
  ```

### 3. Protect the default branch

Check current state:

```bash
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/protected_branches/<default_branch>"
```

- **200** with `push_access_levels[0].access_level == 0`, `merge_access_levels[0].access_level == 40`, and `allow_force_push == false` → already correct; skip to step 4.
- **200** with wrong levels → DELETE first, then POST below. **This is the common case on a new gitlab-master.nvidia.com project**, which defaults to `push_access_level=40` (Maintainers can push directly) — we tighten to `0` so nobody pushes directly.
- **404** → POST directly (rarer; happens only if someone manually unprotected the branch).

```bash
glab api --hostname "$GITLAB_HOST" -X DELETE "projects/$GITLAB_PROJECT_ID/protected_branches/<default_branch>"   # only when wrong-levels case above
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/protected_branches" \
  -f name=<default_branch> \
  -f push_access_level=0 \
  -f merge_access_level=40 \
  -f allow_force_push=false
```

`push_access_level=0` = no one can push directly. `merge_access_level=40` = only Maintainers can merge. `allow_force_push=false` blocks history rewrites.

### 4. Configure self-approval policy

**Always interview the user before applying** — never infer. Single-user repos break with self-approval blocked (no one else can approve, so MRs cannot merge); multi-user repos benefit from blocking it as best practice.

Use `AskUserQuestion` with a single question:

- **Question**: "Block self-approval on merge requests? Recommended for multi-user repos as a security best practice; opt out for single-user repos where the owner is the only approver."
- **Header**: `Self-approval`
- **Options**:
  1. `Block self-approval (Recommended)` — Authors and committers cannot approve their own MRs. Best practice for multi-user / team repos.
  2. `Allow self-approval` — Needed for single-user repos where the owner is the only available approver.

Regardless of the answer, AI never approves or merges MRs; the shared workflow
and GitLab approval rules preserve the human review boundary.

These approval-policy flags live on the dedicated `/approvals` endpoint, **not** on the bare project endpoint. `PUT /projects/<id>` silently ignores them and returns `null` for both fields — use `POST /projects/<id>/approvals` instead (GitLab's approval-settings endpoint uses POST for update). Idempotent: the response echoes the new state.

#### 4a. If "Block self-approval"

```bash
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/approvals" \
  -f merge_requests_author_approval=false \
  -f merge_requests_disable_committers_approval=true \
  -f reset_approvals_on_push=true
```

- `merge_requests_author_approval=false` → MR authors cannot approve their own MRs.
- `merge_requests_disable_committers_approval=true` → anyone who pushed commits cannot approve.
- `reset_approvals_on_push=true` → existing approvals are cleared when new commits land, so a stale approval cannot follow new changes through to merge.

#### 4b. If "Allow self-approval"

```bash
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/approvals" \
  -f merge_requests_author_approval=true \
  -f merge_requests_disable_committers_approval=false \
  -f reset_approvals_on_push=true
```

- Authors and committers may approve their own MRs (required so a single-user repo can actually merge).
- `reset_approvals_on_push=true` is still applied unconditionally — stale approvals are always a risk.

### 5. Require at least one approver

Check existing rules first:

```bash
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/approval_rules"
```

If no rule has `approvals_required >= 1`, create one:

```bash
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/approval_rules" \
  -f name="Require approver" \
  -f approvals_required=1
```

### 6. Write `CONTRIBUTING.md`, validate, and commit

Use the Write tool to create `CONTRIBUTING.md` at the repo root. Pick the merge-requests bullet to match the step 4 choice:

- **If "Block self-approval"** → use: `At least one approval is required, and the MR author and committers cannot self-approve.`
- **If "Allow self-approval"** → use: `At least one approval is required. Self-approval is enabled (single-user mode), so the owner may approve their own MR — AI agents must not.`

Full file content (substitute the self-approval bullet above into the **Merge requests** section):

```markdown
# Contributing

## Branch protection

`main` is protected — no direct commits. All changes go through a merge request.

## Merge requests

- Open an MR from a feature branch (e.g. `claude/<slug>` for AI-driven changes).
- <self-approval bullet from above>
- A Maintainer merges.

## AI contributions

The shared agent workflow is defined by [`AGENTS.md`](AGENTS.md) and
the [`git-flow` skill](../git-flow/SKILL.md). AI must never run `glab mr
approve` — approval is reserved for human reviewers.
```

The settings-only path from step 2 skips this step. Otherwise, if
`CONTRIBUTING.md` already exists on the setup branch, leave it alone because a
human may have customized it.

Validate the exact file, stage only it, and commit it on `claude/setup-repo`:

```bash
git diff --check -- CONTRIBUTING.md
git add CONTRIBUTING.md
git commit -m "chore: configure repository protections"
```

On an idempotent open-MR rerun with no file change, do not create an empty
commit. Verify the existing branch contains its setup commit before publication.

### 7. Publish and hand off

Push the branch, then look up or create its MR through the numeric project ID:

```bash
git push --set-upstream origin HEAD
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&source_branch=claude/setup-repo"
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/merge_requests" \
  -f source_branch="claude/setup-repo" \
  -f target_branch="<default_branch>" \
  -f title="chore: configure repository protections" \
  -f description="Configures branch protection and merge-request approval policy."
```

Create only when the lookup has no open MR; otherwise update the existing MR
with the push. Record its `web_url` and `git rev-parse HEAD` as the branch tip,
then release the clean checkout without waiting for merge:

```bash
git status --porcelain
git checkout <default_branch>
git pull --ff-only
```

If the tree is not clean, stop rather than stashing or discarding anything.
Return the recorded MR URL and branch tip as `awaiting_human`; the missing-file
gate remains active on the default branch until a human merges the setup MR.

### 8. Report

Print a one-block summary:

- **Branch protection on `<default_branch>`**: `created` / `already correct` / `replaced (was: <old levels>)`
- **Self-approval policy**: `blocked` / `allowed (single-user)` — and `applied` / `already in place`
- **Approval rule (≥1 approver)**: `created` / `already in place`
- **`CONTRIBUTING.md`**: `written` / `already present`

For a published bootstrap change, include the MR URL, branch
(`claude/setup-repo`), branch tip, and `awaiting_human`. For a settings-only
verification or repair with no repository diff, explicitly report that no
commit or MR was needed. End with: `Idempotent — re-run anytime to re-verify.`

## Failure modes

- **403 on any API call** → the PAT backing `glab` lacks Maintainer rights on this project. Stop, name the failing call, and tell the user to either escalate their access or have a Maintainer run this command.
- **glab unauthenticated** → tell the user to run `glab auth login` and stop.
- **Git preflight, commit, push, or MR-creation failure** → report the
  non-secret error and required next action. It is a blocker, never a completed
  setup.
- **Any other glab error** → surface verbatim and stop. Do not retry blindly.
