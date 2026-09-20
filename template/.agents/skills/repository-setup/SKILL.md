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

Use this skill only if
`grep -Fq 'All changes go through a merge request.' CONTRIBUTING.md` fails.
Until its bootstrap MR is published, this is the only permitted repository
mutation. Make no unrelated change. Never approve or merge.

Set project protections. Direct default-branch pushes must fail. MRs need an
approval. Ask whether to block approval by authors and committers. Recommend
that setting for a multi-user repository. Allow an opt-out for a single-user
repository with no second reviewer. Write the `CONTRIBUTING.md` marker that
ends this flow.

Use only the dedicated feature branch. Configure protections and approval
policy. Publish the `CONTRIBUTING.md` change in an MR. A changed bootstrap run
returns `awaiting_human` after opening or updating that MR. Do not wait for a
merge. A no-diff, idempotent check makes no repository mutation. Report that no
MR action is needed.

**Run in the main thread. Do not delegate; every step is one command.**

All `glab` calls use in-sandbox HTTPS through the proxy. No
`dangerouslyDisableSandbox` is needed.

## Procedure

### 1. Verify the GitLab user and resolve the project

Before changing GitLab settings, verify that the current directory credentials
identify a GitLab user:

```bash
direnv exec . /usr/bin/env bash -c 'exec glab api user' | jq '{name, email, public_email, username}'
```

Require a successful response with a non-empty `username`. On an error, null,
or empty username, stop. Report that no authenticated GitLab user was verified.
Do not create a branch, change settings, or write `CONTRIBUTING.md`.

```bash
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
```

Require environment `GITLAB_HOST` and numeric `GITLAB_PROJECT_ID`. Never get a
project path from the Git remote. Confirm that returned `id` matches the ID.
Read `default_branch` from JSON. On an error, stop and show it.

### 2. Prepare the dedicated bootstrap branch

Require a clean, current default branch before changing GitLab settings or
writing `CONTRIBUTING.md`:

```bash
git status --porcelain
git rev-parse --abbrev-ref HEAD
git pull --ff-only
git fetch origin --prune
setup_branch="clanker/setup-repo"
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=all&source_branch=clanker/setup-repo"
git show-ref --verify --quiet "refs/heads/$setup_branch"
git show-ref --verify --quiet "refs/remotes/origin/$setup_branch"
```

Before `git pull`, the branch must be `<default_branch>`. Otherwise, with a
dirty tree, or after any failed preflight, stop and name the blocker. The two
`git show-ref` calls are separate presence checks. Exit 1 means no ref, not a
failed preflight. Never silently check out, stash, discard, or add setup work
to another branch.

Resolve state before changing GitLab settings:

- If `CONTRIBUTING.md` is on the default branch and no setup MR is open, stay
  there. Only verify or repair settings in steps 3–5. Then report the
  settings-only result. Historical closed or merged setup MRs and abandoned
  setup refs do not block this no-repository-change route.
- If a setup MR is open, require its source branch to match the local or remote
  ref. Check out the local branch or track the remote one. Create or reuse its
  worktree. Confirm it has only expected setup work. Update that same MR.
- If the file is missing and no setup MR is open, an existing local or remote
  setup ref, or a past closed or merged setup MR, is ambiguous. Stop before
  setting GitLab policy. Ask a human. Never recreate, overwrite, or diverge it.
- If the file is missing and no setup branch or MR has existed, create the
  dedicated branch from clean default:

  ```bash
  git checkout -b "$setup_branch"
  ```

### 3. Protect the default branch

Check current state:

```bash
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/protected_branches/<default_branch>"
```

- **200** with `push_access_levels[0].access_level == 0`,
  `merge_access_levels[0].access_level == 40`, and
  `allow_force_push == false` means correct. Go to step 4.
- **200** with other values requires DELETE, then the POST below. New
  gitlab-master.nvidia.com projects commonly use `push_access_level=40`.
  Maintainers could then push directly. Set it to `0` so no one can.
- **404** needs the POST directly. This is rare. It means someone unprotected
  the branch.

```bash
glab api --hostname "$GITLAB_HOST" -X DELETE "projects/$GITLAB_PROJECT_ID/protected_branches/<default_branch>"   # only when wrong-levels case above
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/protected_branches" \
  -f name=<default_branch> \
  -f push_access_level=0 \
  -f merge_access_level=40 \
  -f allow_force_push=false
```

`push_access_level=0` blocks all direct pushes. `merge_access_level=40` allows
only Maintainer merges. `allow_force_push=false` blocks history rewrites.

### 4. Configure self-approval policy

**Always ask the user before applying. Never infer.** Blocking self-approval
breaks a single-user repo: nobody else can approve. It is best practice for a
multi-user repo.

Use `AskUserQuestion` with a single question:

- **Question**: "Block self-approval on merge requests? Recommended for multi-user repos as a security best practice; opt out for single-user repos where the owner is the only approver."
- **Header**: `Self-approval`
- **Options**:
  1. `Block self-approval (Recommended)` — Authors and committers cannot approve their own MRs. Best practice for multi-user / team repos.
  2. `Allow self-approval` — Needed for single-user repos where the owner is the only available approver.

Whatever the answer, AI never approves or merges MRs. Shared workflow and
GitLab rules keep the human review boundary.

These flags are on `/approvals`, **not** the bare project endpoint.
`PUT /projects/<id>` silently ignores them and returns `null` for both. Use
`POST /projects/<id>/approvals`; GitLab uses POST to update approval settings.
The response echoes the new state, so this is idempotent.

#### 4a. If "Block self-approval"

```bash
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/approvals" \
  -f merge_requests_author_approval=false \
  -f merge_requests_disable_committers_approval=true \
  -f reset_approvals_on_push=true
```

- `merge_requests_author_approval=false`: authors cannot approve their MRs.
- `merge_requests_disable_committers_approval=true`: commit pushers cannot
  approve.
- `reset_approvals_on_push=true`: new commits clear approvals. A stale approval
  cannot pass new changes to merge.

#### 4b. If "Allow self-approval"

```bash
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/approvals" \
  -f merge_requests_author_approval=true \
  -f merge_requests_disable_committers_approval=false \
  -f reset_approvals_on_push=true
```

- Authors and committers may approve their MRs. A single-user repo needs this
  to merge.
- Always apply `reset_approvals_on_push=true`. Stale approvals are still a
  risk.

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

- Open an MR from a feature branch (e.g. `clanker/<slug>` for AI-driven changes).
- <self-approval bullet from above>
- A Maintainer merges.

## AI contributions

The shared agent workflow is defined by [`AGENTS.md`](AGENTS.md) and
the [`git-flow` skill](../git-flow/SKILL.md). AI must never run `glab mr
approve` — approval is reserved for human reviewers.
```

The settings-only route from step 2 skips this. If the file is already on the
setup branch, leave it alone. A human may have customized it.

Validate that file. Stage only it. Commit on `clanker/setup-repo`:

```bash
git diff --check -- CONTRIBUTING.md
git add CONTRIBUTING.md
git commit -m "chore: configure repository protections"
```

On an idempotent open-MR rerun with no file change, do not make an empty
commit. Before publishing, confirm the branch has its setup commit.

### 7. Publish and hand off

Push the branch. Then look up or create its MR with the numeric project ID:

```bash
git push --set-upstream origin HEAD
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&source_branch=clanker/setup-repo"
glab api --hostname "$GITLAB_HOST" -X POST "projects/$GITLAB_PROJECT_ID/merge_requests" \
  -f source_branch="clanker/setup-repo" \
  -f target_branch="<default_branch>" \
  -f title="chore: configure repository protections" \
  -f description="Configures branch protection and merge-request approval policy."
```

Create only if lookup finds no open MR. Otherwise, update it with the push.
Record `web_url` and `git rev-parse HEAD` as its tip. Release a clean checkout.
Do not wait for merge:

```bash
git status --porcelain
git checkout <default_branch>
git pull --ff-only
```

If the tree is dirty, stop. Do not stash or discard. Return the recorded URL
and tip as `awaiting_human`. The missing-file gate remains on the default
branch until a human merges the setup MR.

### 8. Report

Print a one-block summary:

- **Branch protection on `<default_branch>`**: `created` / `already correct` / `replaced (was: <old levels>)`
- **Self-approval policy**: `blocked` / `allowed (single-user)` — and `applied` / `already in place`
- **Approval rule (≥1 approver)**: `created` / `already in place`
- **`CONTRIBUTING.md`**: `written` / `already present`

For a published bootstrap, include MR URL, `clanker/setup-repo`, branch tip,
and `awaiting_human`. For a settings-only check or repair with no diff, say no
commit or MR was needed. End with: `Idempotent — re-run anytime to re-verify.`

## Failure modes

- **403 on an API call**: `glab`'s PAT lacks Maintainer rights. Stop. Name the
  call. Ask the user to escalate access or have a Maintainer run it.
- **glab unauthenticated**: tell the user to run `glab auth login`, then stop.
- **Git preflight, commit, push, or MR creation failure**: report the
  non-secret error and next action. This blocks setup. It is not completion.
- **Any other glab error**: show it verbatim and stop. Do not retry blindly.
