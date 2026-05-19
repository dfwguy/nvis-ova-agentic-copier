---
description: Lock down the GitLab repo (protect main, require MR approval) and write CONTRIBUTING.md. Idempotent.
---

Configures GitLab project-level protections so the default branch cannot be pushed to directly and merge requests require approval by someone other than the author/committer. Writes `CONTRIBUTING.md` so the repo-lock-down gate in `CLAUDE.md` silences itself.

**Run from the main thread. Do not spawn a subagent — every step is a single command.**

All `glab` calls are in-sandbox HTTPS via the proxy; no `dangerouslyDisableSandbox` needed.

## Procedure

### 1. Resolve the project

```bash
glab repo view --output json
```

Extract `id` (numeric project ID) and `default_branch` from the JSON. If `glab` is unauthenticated or errors, stop and tell the user to run `glab auth login`.

### 2. Protect the default branch

Check current state:

```bash
glab api "projects/<id>/protected_branches/<default_branch>"
```

- **200** with `push_access_levels[0].access_level == 0`, `merge_access_levels[0].access_level == 40`, and `allow_force_push == false` → already correct; skip to step 3.
- **200** with wrong levels → DELETE first, then POST below. **This is the common case on a new gitlab-master.nvidia.com project**, which defaults to `push_access_level=40` (Maintainers can push directly) — we tighten to `0` so nobody pushes directly.
- **404** → POST directly (rarer; happens only if someone manually unprotected the branch).

```bash
glab api -X DELETE "projects/<id>/protected_branches/<default_branch>"   # only when wrong-levels case above
glab api -X POST "projects/<id>/protected_branches" \
  -f name=<default_branch> \
  -f push_access_level=0 \
  -f merge_access_level=40 \
  -f allow_force_push=false
```

`push_access_level=0` = no one can push directly. `merge_access_level=40` = only Maintainers can merge. `allow_force_push=false` blocks history rewrites.

### 3. Block self-approval on MRs

These two flags live on the dedicated `/approvals` endpoint, **not** on the bare project endpoint. `PUT /projects/<id>` silently ignores them and returns `null` for both fields — use `POST /projects/<id>/approvals` instead (GitLab's approval-settings endpoint uses POST for update). Idempotent: the response echoes the new state.

```bash
glab api -X POST "projects/<id>/approvals" \
  -f merge_requests_author_approval=false \
  -f merge_requests_disable_committers_approval=true \
  -f reset_approvals_on_push=true
```

- `merge_requests_author_approval=false` → MR authors cannot approve their own MRs.
- `merge_requests_disable_committers_approval=true` → anyone who pushed commits cannot approve.
- `reset_approvals_on_push=true` → existing approvals are cleared when new commits land, so a stale approval cannot follow new changes through to merge.

Together these block the AI (which is always the committer) from approving its own MR.

### 4. Require at least one approver

Check existing rules first:

```bash
glab api "projects/<id>/approval_rules"
```

If no rule has `approvals_required >= 1`, create one:

```bash
glab api -X POST "projects/<id>/approval_rules" \
  -f name="Require approver" \
  -f approvals_required=1
```

### 5. Write `CONTRIBUTING.md`

Use the Write tool to create `CONTRIBUTING.md` at the repo root with this content:

```markdown
# Contributing

## Branch protection

`main` is protected — no direct commits. All changes go through a merge request.

## Merge requests

- Open an MR from a feature branch (e.g. `claude/<slug>` for AI-driven changes).
- At least one approval is required, and the MR author and committers cannot self-approve.
- A Maintainer merges.

## AI contributions

Claude Code drives most of the workflow. See [`.claude/git-flow.md`](.claude/git-flow.md) for the branch / commit / MR / return-to-main lifecycle and [`CLAUDE.md`](CLAUDE.md) for the multi-agent TDD process. AI must never run `glab mr approve` — approval is reserved for human reviewers.
```

If `CONTRIBUTING.md` already exists, leave it alone (a human may have customized it).

### 6. Report

Print a one-block summary:

- **Branch protection on `<default_branch>`**: `created` / `already correct` / `replaced (was: <old levels>)`
- **Self-approval block**: `applied` / `already in place`
- **Approval rule (≥1 approver)**: `created` / `already in place`
- **`CONTRIBUTING.md`**: `written` / `already present`

End with: `Idempotent — re-run anytime to re-verify.`

## Failure modes

- **403 on any API call** → the PAT backing `glab` lacks Maintainer rights on this project. Stop, name the failing call, and tell the user to either escalate their access or have a Maintainer run this command.
- **glab unauthenticated** → tell the user to run `glab auth login` and stop.
- **Any other glab error** → surface verbatim and stop. Do not retry blindly.
