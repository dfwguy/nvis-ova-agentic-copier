# AGENTS.md

This repository uses Claude Code as its primary AI coding assistant. If you are another AI coding agent (Codex, Cursor, etc.), this file is your entry point.

@CLAUDE.md

@.claude/lang/python.md

@.claude/lang/bash.md

## Codex workflow

The `.claude/` agents and slash commands are Claude Code configuration. Codex
does not execute their front matter; it follows the shared workflow and
conventions they describe, then uses native Codex subagents when delegation is
useful.

Use this model routing for Codex subagents. These are capability/cost tiers,
not parameter-count equivalences:

| Work | Model | Reasoning effort |
|---|---|---|
| Architecture and code-quality review | `gpt-5.6-sol` | `high` |
| Implementation, tests, documentation, CI review, and test review | `gpt-5.6-terra` | `medium` |
| Focused validation and documentation checks | `gpt-5.6-luna` | `low` |

For a non-trivial change, preserve the same sequence described in `CLAUDE.md`:
design; tests and documentation in parallel; minimum implementation;
validation; independent review; final validation and commit. Keep the
main-agent model for tasks where continuity matters, such as the red/green
handoff. Do not create a second `.claude/` workflow with Codex model names.

## GitLab project IDs

All `glab` API operations after target discovery must address the numeric
project ID. `agent-work` derives its checkout and GitLab project from `origin`,
then confirms the numeric endpoint; it must not require `GITLAB_HOST`,
`GITLAB_PROJECT_ID`, or `AGENT_WORK_CHECKOUT` to be present. Before remote work
outside the runner, confirm Git and the GitLab endpoint:

```bash
git pull --ff-only
glab api --hostname gitlab.example.com "projects/12345"
```

Use endpoints under `projects/$GITLAB_PROJECT_ID` for merge requests,
pipelines, branch protection, and approval rules. For MR creation, push with
Git first, then call `POST projects/$GITLAB_PROJECT_ID/merge_requests`.

## Developer-readable loop readiness exception

For a constrained identity with effective API access exactly `30`, the
Developer-readable preflight is the narrow exception to Maintainer-only
approval nagging. It must verify default branch protection (no direct push,
Developer merge, and no push) through the authoritative effective default
branch response, plus an `approval_rules` rule requiring at least one approval
that is unambiguously global. This exception skips only the Maintainer-only
`/approvals` audit and recurring Maintainer attestation; it does not waive
one-time authorization, constrained identity, checkout/origin, preview,
publication identity, or the prohibition on approving or merging.

Developer role alone does not prove these controls, self-approval prevention,
or per-MR approval-rule overrides. This readiness selection intentionally
skips those Maintainer-only checks and relies on workflow, settings, and
runtime never-approve/merge guardrails; it is not a hard same-user security
boundary. An inaccessible Maintainer-only audit blocks only the unattended
agent-loop `--apply` readiness decision. It does **not** mean a Developer
cannot publish a feature branch or create a merge request. Do not mistake that
audit boundary for Git push or MR-create permission.

When normal repository work is complete, a Developer must still continue with
the publication handoff: create or reuse the task feature branch, commit,
push, and attempt to open or update the merge request. Use the numeric project
ID for every `glab api` MR operation; use the configured Git remote only for
`git push`. A real push or MR API authentication failure is the publication
blocker—not the unavailable Maintainer-only loop-readiness audit.

## Mandatory publication handoff

Every repository change must publish a feature branch and open or update a
merge request. A publication failure is a blocker, not completion.

For every user-requested repository mutation, do not stop at local edits,
validation, or a local commit. The main agent must always create or reuse a
feature branch, commit, push, and attempt to open or update its MR through
`.claude/git-flow.md` Step 3. Create or update the MR with
`projects/$GITLAB_PROJECT_ID` (or the established project path only when an
API client does not support numeric IDs); never skip the attempt because an
unrelated readiness audit is unavailable. The final response must include the
direct MR URL and branch tip. A real authentication, push, or MR-creation
failure is a blocker, never completion: state the exact blocker and next
action without implying that unpublished work is complete. Read-only
inspection is the only work that does not require publication.
