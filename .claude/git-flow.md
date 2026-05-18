# Git Flow

Every change in this repo — full TDD workflow or one-off trivial edit — follows the same lifecycle: branch off `main`, do the work, commit, push, open or update a merge request via `glab`, return to `main`. The main agent owns this lifecycle. **Do not spawn a subagent for any of the steps below** — all of it is short, local-only or single-command-remote, and trivially runs from the main thread. Subagents add latency and an extra approval surface for no benefit here.

## Step 1: Feature branch (before any code / test / doc change)

Run from the main thread with Bash:

```bash
git rev-parse --abbrev-ref HEAD
```

- If the result is `main` or `master`, derive a kebab-case slug (≤40 chars) from the task description, then:
  ```bash
  git checkout -b claude/<slug>
  ```
- Otherwise (already on a feature branch), do **not** create another branch. Follow-up iterations append commits to the same branch and update the same MR.

Both commands are local-only and sandbox-safe. If the sandbox ever blocks them, retry with sandbox disabled — they only touch `.git/`.

## Step 2: Do the work

- **Non-trivial change** → route through the TDD agents (`/architect` → `/tdd-red` → `/tdd-green` → `/tdd-judge` → parallel judges → `/tdd-finalizer`). See `CLAUDE.md` → "Multi-Agent Workflow" for the routing.
- **Trivial change** (≤ ~10 lines, no behavior surprise, no new dependencies) → the main agent may edit + commit directly without spawning the TDD agents. Pre-commit hooks still run at commit time, so quality gates are not bypassed.

Either path lands at a local commit on the feature branch.

## Step 3: Publish (push + MR)

Run from the main thread, **outside the sandbox** (`dangerouslyDisableSandbox: true`). One user approval per remote-touching command.

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

3a. **No existing MR — combined push + create** (sandbox disabled, single approval):
   - `--title`: `git log -1 --format=%s` (the commit subject)
   - `--description`: Summary section (commit body, or 2–3 bullets of what shipped) + Test plan section (commands reviewers can run, e.g. `uv run tox`)
   - `--target-branch`: `main` (or the default branch from `git symbolic-ref refs/remotes/origin/HEAD` if different)

   ```bash
   glab mr create \
     --push \
     --target-branch main \
     --title "<commit subject>" \
     --description "<built body>" \
     --yes
   ```
   `--push` pushes the branch first, then opens the MR. `--yes` skips glab's own confirmation, so the Claude permission prompt is the only approval.

3b. **MR exists — just push** (sandbox disabled):
   ```bash
   git push
   ```
   GitLab updates the existing MR with the new commits. Do not create a duplicate MR.

4. **Report** to the user: the MR URL (from `glab mr create`, or `glab mr view --web` in 3b), the final commit hash, and a one-line summary.

## Step 4: Return to `main`

After the MR is reported, switch back so the next piece of work starts clean:

```bash
git status            # confirm clean working tree
git checkout main
```

Local-only, sandbox-safe. Skip this only if the user explicitly says they want to stay on the feature branch (e.g., to keep iterating without re-checking out).

## When the lifecycle does not apply

- Read-only sessions (the user is asking questions, not asking for changes).
- Explicit user override ("don't branch", "don't open an MR", etc.) — honor it but mention what you're skipping.
