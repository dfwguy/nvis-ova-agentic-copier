---
description: Lock down the GitLab repo (protect main, require MR approval) and write CONTRIBUTING.md. Idempotent.
---

Configures GitLab project-level protections so the default branch cannot be pushed to directly and merge requests require approval. Interviews the user on whether to block self-approval by authors and committers (recommended for multi-user repos; opt out for single-user repos where there is no second reviewer). Writes `CONTRIBUTING.md` so the repo-lock-down gate in `CLAUDE.md` silences itself.

**Run from the main thread. Do not spawn a subagent — every step is a single command.**

All `glab` calls are in-sandbox HTTPS via the proxy; no `dangerouslyDisableSandbox` needed.

## Procedure

### 1. Resolve the project

```bash
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
```

Require `GITLAB_HOST` and a numeric `GITLAB_PROJECT_ID` from the environment;
never derive a project path from the Git remote. Confirm the returned `id`
matches `GITLAB_PROJECT_ID` and extract `default_branch` from the JSON. If the
call errors, stop and surface the error.

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

### 3. Configure self-approval policy

**Always interview the user before applying** — never infer. Single-user repos break with self-approval blocked (no one else can approve, so MRs cannot merge); multi-user repos benefit from blocking it as best practice.

Use `AskUserQuestion` with a single question:

- **Question**: "Block self-approval on merge requests? Recommended for multi-user repos as a security best practice; opt out for single-user repos where the owner is the only approver."
- **Header**: `Self-approval`
- **Options**:
  1. `Block self-approval (Recommended)` — Authors and committers cannot approve their own MRs. Best practice for multi-user / team repos.
  2. `Allow self-approval` — Needed for single-user repos where the owner is the only available approver.

Regardless of the answer, AI never approves MRs — the `glab mr approve` / `glab mr revoke` deny rules in `.claude/settings.json` enforce this independently.

These approval-policy flags live on the dedicated `/approvals` endpoint, **not** on the bare project endpoint. `PUT /projects/<id>` silently ignores them and returns `null` for both fields — use `POST /projects/<id>/approvals` instead (GitLab's approval-settings endpoint uses POST for update). Idempotent: the response echoes the new state.

#### 3a. If "Block self-approval"

```bash
glab api -X POST "projects/<id>/approvals" \
  -f merge_requests_author_approval=false \
  -f merge_requests_disable_committers_approval=true \
  -f reset_approvals_on_push=true
```

- `merge_requests_author_approval=false` → MR authors cannot approve their own MRs.
- `merge_requests_disable_committers_approval=true` → anyone who pushed commits cannot approve.
- `reset_approvals_on_push=true` → existing approvals are cleared when new commits land, so a stale approval cannot follow new changes through to merge.

#### 3b. If "Allow self-approval"

```bash
glab api -X POST "projects/<id>/approvals" \
  -f merge_requests_author_approval=true \
  -f merge_requests_disable_committers_approval=false \
  -f reset_approvals_on_push=true
```

- Authors and committers may approve their own MRs (required so a single-user repo can actually merge).
- `reset_approvals_on_push=true` is still applied unconditionally — stale approvals are always a risk.

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

Use the Write tool to create `CONTRIBUTING.md` at the repo root. Pick the merge-requests bullet to match the step 3 choice:

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

Claude Code drives most of the workflow. See [`.claude/git-flow.md`](.claude/git-flow.md) for the branch / commit / MR / return-to-main lifecycle and [`CLAUDE.md`](CLAUDE.md) for the multi-agent TDD process. AI must never run `glab mr approve` — approval is reserved for human reviewers.
```

If `CONTRIBUTING.md` already exists, leave it alone (a human may have customized it).

### 6. Report

Print a one-block summary:

- **Branch protection on `<default_branch>`**: `created` / `already correct` / `replaced (was: <old levels>)`
- **Self-approval policy**: `blocked` / `allowed (single-user)` — and `applied` / `already in place`
- **Approval rule (≥1 approver)**: `created` / `already in place`
- **`CONTRIBUTING.md`**: `written` / `already present`

End with: `Idempotent — re-run anytime to re-verify.`

## Failure modes

- **403 on any API call** → the PAT backing `glab` lacks Maintainer rights on this project. Stop, name the failing call, and tell the user to either escalate their access or have a Maintainer run this command.
- **glab unauthenticated** → tell the user to run `glab auth login` and stop.
- **Any other glab error** → surface verbatim and stop. Do not retry blindly.
