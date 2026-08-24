---
name: setup-agent-loop
description: Configure one guarded, serial GitLab polling loop; not webhooks, queues, or automatic merge.
---

# Set up agent loop

Use this skill to configure one GitLab project for the serial `run-next` loop.
It neither installs a webhook nor implicitly authorizes unattended publication.

1. Establish and validate a bare `GITLAB_HOST` and numeric
   `GITLAB_PROJECT_ID` first. Then check only the preinstalled Git, `glab`,
   and Codex tools and their existing noninteractive sessions. Do not print or
   place tokens in commands, environment files, or logs:

   ```bash
   case "$GITLAB_HOST" in ''|*://*|*/*) exit 2;; esac
   case "$GITLAB_PROJECT_ID" in ''|*[!0-9]*) exit 2;; esac
   command -v git glab codex
   glab auth status --hostname "$GITLAB_HOST"
   codex login status
   glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
   ```
2. Require absolute `AGENT_WORK_CHECKOUT` and `AGENT_WORK_LOCK_FILE` in
   addition to the validated host/project settings. Use only numeric-project
   endpoints, never `origin` inference.
3. Create a clean dedicated checkout that is not an operator's active checkout.
   Its `origin` must match the verified project, and `git -C
   "$AGENT_WORK_CHECKOUT" ls-remote origin HEAD` must succeed without prompts.
4. Check `uv`, install `agent-work` with `uv tool install --force .`, then
   verify it with `command -v agent-work`. Install the one
   `agent-work@.service` and one `agent-work@.timer` user unit; write the four
   non-secret values to a mode-`0600` instance environment file; then run
   `agent-work run-next --json` as a preview smoke test.
5. Before enabling `--apply`, obtain explicit unattended-publication approval,
   use a constrained service identity, and verify independent MR safeguards:
   one approval, no author self-approval, and no per-MR rule overrides. A
   Developer `403` for Maintainer-only settings is inconclusive; require a
   human Maintainer attestation.
6. Enable one instance timer and check its status and journal. To repair or
   pause, disable the timer first; no legacy SQLite state migration is needed.

The loop projects `agent::ready` to `agent::running`, then to
`agent::awaiting-human` only after exact same-project MR evidence, matching
local/remote branch tips, default target, and `Closes #IID`; otherwise it uses
`agent::failed`. Branches are deterministic: `agent/issue-<IID>`. The local
lock returns exit `75` on contention. This is intentionally single-host and
does not provide a distributed GitLab compare-and-set claim. Never approve or
merge a resulting MR.
