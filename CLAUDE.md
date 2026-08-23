# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@.claude/project.md

@.claude/git-flow.md

## Repo lock-down gate

**Check before every response**: does `CONTRIBUTING.md` exist at the repo root?

- **Missing** → the GitLab repo has not been locked down (no branch protection, no MR approval rules). Until that file exists:
  1. Open the response with: `⚠ Repo not locked down — run /setup-repo to configure branch protection.`
  2. Close the response with the same line.
  3. Do not commit, push, or open an MR. Reading code and editing files locally is fine. The first action that touches `git commit`, `git push`, or `glab mr create` must be `/setup-repo`.
- **Present** → the gate is silent; proceed normally.

`/setup-repo` writes `CONTRIBUTING.md` and applies GitLab branch protection + MR approval rules in one shot. It is idempotent — re-running is a no-op.

**Never run `glab mr approve` or `glab mr revoke`.** AI must not approve or unapprove merge requests; approval is reserved for human reviewers. `.claude/settings.json` denies these commands as a backstop.

## Sandbox

Run commands in the sandbox first — never preemptively disable it. On a sandbox failure, try to fix the command (or sandbox config) before falling back to disabling the sandbox.

## Links in responses

This terminal makes a URL clickable **only when it sits on its own line inside a fenced code block**. Every other form renders as styled, unclickable text: bare URLs in prose, inline-code URLs (single backticks), Markdown `[text](url)` links, and `<url>` angle brackets all fail. When surfacing a URL the user may want to click (MR/pipeline links, docs, dashboards), put it alone in a fenced block:

````
```
https://example.com/path
```
````

Add any label as plain prose *before* the block, not inside it.

## Git Remote (HTTPS over the sandbox proxy)

The Claude Code sandbox proxies HTTPS to allow-listed domains (including `gitlab-master.nvidia.com`) but blocks raw TCP/UDP, so an SSH `origin` can't be used in-sandbox — it fails on a blocked connection (`Permission denied (publickey)` / no route), **not** on DNS: the hostname resolves fine. Configure this repo's `origin` to use HTTPS, with `glab` providing the credential, so the full git-flow (pull, push, `glab mr ...`) runs in-sandbox without `dangerouslyDisableSandbox`.

One-time setup per clone:

```bash
glab auth status                          # read-only; verifies glab is logged in to gitlab-master.nvidia.com
git remote set-url origin https://gitlab-master.nvidia.com/<group>/<project>.git
git config credential.https://gitlab-master.nvidia.com.helper '!glab auth git-credential'
git config push.default simple            # bare `git push` pushes only the current branch, never main
```

After this, `git pull`, `git push`, `glab mr create`, and `glab mr list` all work in-sandbox.

Two things worth knowing:

- **`push.default simple` matters.** Under git's old `matching` mode a bare `git push` also tries to push `main`, which branch protection rejects — noisy and confusing. `simple` (git's modern default, set explicitly here) pushes only the current branch.
- **The credential helper must live in the config *file*.** The `!glab auth git-credential` shell-escape only works when persisted with `git config`. Injecting it per-command via `git -c …helper=…` or `GIT_CONFIG_*` does **not** work — git reads `!glab` as a literal helper name (`git: 'credential-!glab' is not a git command`).

> **If you hit a read-only sandbox mount:** some sandbox configurations bind-mount `.git/config` and `$HOME` (`~/.gitconfig`, `~/.config/glab-cli/`) read-only, so `git config` / `glab mr create` writes fail with `Device or resource busy` or `read-only file system`. This repo's environment does **not** — those paths are writable and `glab mr create` works in-sandbox — but if you do hit that error, run that one command unsandboxed. A harmless `.gitmodules: Permission denied` warning on fetch/pull can be ignored.

## Language-Specific Conventions

When working on code in this repo, read the relevant file for conventions, commands, testing patterns, and tooling:

| Language | Conventions file |
|----------|-----------------|
| Python | `.claude/lang/python.md` |

Subagents that work on Python code load `.claude/lang/python.md` automatically via their agent definition. When you (the main agent) are about to write, review, or reason about Python code and haven't yet read that file, read it first.

> Adding a new language? Create `.claude/lang/<language>.md` following the same structure, then add a row above and update the subagents that will work in that language.

## General Guidance

Language-agnostic guidance lives in `.claude/guidance/`. Read the relevant file on demand; these are the canonical sources the Cursor rules `@`-import.

| Topic | File |
|-------|------|
| Test-driven development | `.claude/guidance/tdd.md` |
| Markdown conventions | `.claude/guidance/markdown.md` |
| Diátaxis documentation process | `docs/diataxis.md` |

## Multi-Agent Workflow

All feature work routes through explicit agents invoked as slash commands (e.g. `/architect`). Definitions are in `.claude/commands/`.

**Scope: this workflow currently targets Python projects.** The agents assume `uv`, `ruff`, `ty`, `pytest`, and `tox` are available. See `.claude/lang/python.md` for the full Python convention set.

### Routing

The branch / commit / push / MR / return-to-main lifecycle is described once in `.claude/git-flow.md` (loaded above) and applies to **every** change — TDD or trivial. Branch protection on `main` (configured by `/setup-repo`) enforces this: direct pushes are rejected and merging requires approval, so even a one-line edit must go through a feature branch and an MR. The diagram below covers the in-between "do the work" phase for non-trivial changes; the surrounding lifecycle steps come from `git-flow.md`.

```
New feature or non-trivial change
        │
        ▼
   (Step 1 of git-flow.md: ensure feature branch)
        │
        ▼
   /architect          ← design first; skippable only for trivial single-function changes
        │
        ▼
   /tdd-red ──────────── /diataxis-writer
   failing tests          docs/ content by Diátaxis category
        │                         │
        └──────────────┬──────────┘
                       ▼
   /tdd-green          ← Implementer: minimal code to pass tests
        │
        ├─────────────────────────┐   (run in parallel after tdd-green)
        ▼                         ▼
   /tdd-judge              /diataxis-judge
   implementation checks    docs/ accuracy + Diátaxis fit
        │                         │
        └──────────────┬──────────┘
                       ▼
        ├──────────────┬──────────────┬──────────────┬─────────────────┐   (run in parallel after both approve)
        ▼              ▼              ▼              ▼                 ▼
  /judge-quality   /judge-test    /tdd-document  /judge-cicd  /cli-syntax-judge
   pure code       pure test      README landing checks CI + pipeline  CLIG + CLI contract
   review          review         accuracy       health
        │              │              │              │                 │
        └──────┬───────┴──────────────┴──────────────┴─────────────────┘
               ▼
        /tdd-finalizer  ← always runs; verifies README, runs tox once across supported versions,
                          and commits locally. Rejects to /tdd-document if README is stale.
        │
        ▼
   (Steps 3–4 of git-flow.md: push + open/update MR, then stay on the branch until it merges)
```

### Agent Summary

| Command | Role | Does not |
|---------|------|----------|
| `/architect` | Designs modules, data structures, dependency boundaries | Write code or tests |
| `/tdd-red` | Writes failing tests; stops at Red | Touch implementation |
| `/diataxis-writer` | Creates or updates `docs/` pages as Tutorials, How-to guides, Reference, or Explanation | Touch code, tests, or README.md |
| `/tdd-green` | Minimal implementation to reach Green | Refactor or add untested features |
| `/tdd-judge` | Runs ruff + non-tox pre-commit checks | Fix issues — rejects and routes back |
| `/diataxis-judge` | Read-only validation that `docs/` is technically accurate and fits its Diátaxis category | Edit docs, code, tests, or README.md |
| `/judge-quality` | Pure code review: design, clarity, conventions; tags findings **Major** / **Minor** / **Existing** | Run any tooling |
| `/judge-test` | Pure test-quality review (Sonnet): meaningful assertions, parametrize, hypothesis, order-independence, no internal mocking; tags findings **Major** / **Minor** / **Existing** | Run any tooling; review production-code quality |
| `/tdd-document` | Ensures README.md accurately orients people landing on the project | Touch `docs/`, code, tests, or agent definitions |
| `/judge-cicd` | Verifies `.gitlab-ci.yml` is in sync with the change and checks pipeline health via `glab` | Edit CI config or retry pipelines |
| `/cli-syntax-judge` | Validates CLI changes against CLIG and the `--json`, `--help`, `--apply`, dry-run, exit-code, and output contracts | Run tooling or edit files |
| `/tdd-finalizer` | Verifies README sync, runs tox across all Python versions, commits locally; the orchestrator then follows `git-flow.md` Steps 3–4 (push + MR; stay on the branch until it merges) | Fix issues — rejects and routes back |

Never write implementation before a failing test exists. Never merge without all validators approving.

### Diátaxis documentation

Diátaxis separates documentation by reader need: **Tutorials** teach through a
lesson, **How-to guides** solve a task, **Reference** supports accurate lookup,
and **Explanation** supplies understanding and rationale. The writer and judge
use this separation so `docs/` remains navigable instead of becoming a mixed
collection of catch-all pages. See [`docs/diataxis.md`](docs/diataxis.md) for
the required workflow and category guidance.

### Completion requires publication

For any user-requested repository change, local validation is not a completion point. The main agent must continue through `.claude/git-flow.md` Step 3 and report the direct merge-request URL and branch-tip commit in its final response. It may stop before publication only when the user explicitly requests local-only work or a concrete commit, authentication, push, or MR-creation blocker prevents it; report that blocker and the required next action plainly.

### Routing back on rejection

When a judge (`/tdd-judge`, `/diataxis-judge`, `/judge-quality`, `/judge-test`, `/judge-cicd`, `/cli-syntax-judge`, `/tdd-finalizer`) rejects and routes back to an earlier agent, proceed automatically — **do not pause to ask the user for approval**. Rejection-and-reroute is a normal part of the workflow, not a decision point. Only stop for the user when there is a genuine decision to make (ambiguous design, conflicting requirements, destructive action). A judge's structured reroute is not such a decision.

### `/judge-quality` and `/judge-test` severity model

`/judge-quality` (production-code review) and `/judge-test` (test-quality review) both classify every finding as one of:

- **Major** — defect in this change. Triggers `Overall: Reject`. `/judge-quality` reroutes to `/tdd-green`; `/judge-test` reroutes to `/tdd-red` (test defects belong to the Test Writer) — except a `/judge-test` Major flagging production code that *forces* internal mocking (a missing injection point), which reroutes to `/tdd-green`. Reroutes are automatic.
- **Minor** — non-blocking style/clarity nit caused or kept by this change. The main agent (under the `/judge-quality` or `/judge-test` slash command) appends each Minor finding to `FIXME.md` and stages it so `/tdd-finalizer` commits it alongside the change. Does not reroute. See "FIXME.md and TODO.md" below.
- **Existing** — pre-existing issue in surrounding code or tests, not introduced by this change. Surfaced verbatim in the judge's report but **not** written to `FIXME.md`. Never blocks.

Both reviewers end their return note with `Added <N> entries to FIXME.md` (or `No new FIXME entries`). The main agent forwards those lines to `/tdd-finalizer` so the finalizer knows whether `FIXME.md` is part of this commit, and so post-MR the user is asked whether to elevate any FIXME entries to `TODO.md`. Existing findings are surfaced to the user at the same point but kept out of `FIXME.md`.

`/judge-quality` owns production-code quality; `/judge-test` runs on **Sonnet** and owns test quality — meaningful assertions that would catch a wrong implementation, `pytest.mark.parametrize`, `hypothesis` for pure functions, order-independence (`pytest-randomly`), dependency-injected test doubles instead of internal-symbol patching, and test hygiene. The two reviewers do not duplicate each other.

## FIXME.md and TODO.md

Two repo-root, checked-in files capture work that is not part of the current change but should not be forgotten. Both apply to this repo and to projects scaffolded from it (mirrored under `template/`).

### `FIXME.md` — agent-written, deferred quality findings

- **Source.** Only the quality reviewers `/judge-quality` (production code) and `/judge-test` (tests) write to `FIXME.md`, and only for **Minor** findings (`Existing` findings stay in the judge's report). The other agents (`/judge-cicd`, `/tdd-judge`, `/tdd-finalizer`) never touch it.
- **When.** During the `/judge-quality` or `/judge-test` slash command, after the subagent returns `Overall: Approve` with Minor findings. The main agent appends, then `git add FIXME.md` so `/tdd-finalizer` commits it in the same commit as the change.
- **Schema.**
  ```markdown
  # FIXME

  Non-blocking Minor findings from `/judge-quality`. Entries are removed by the main agent when the user directs the fix and it lands. See CLAUDE.md → "FIXME.md and TODO.md".

  ## YYYY-MM-DD — <branch name>

  - **<file>:<line>** — <verbatim Minor finding>
  ```
  New iterations append a new dated `##` section; do not collapse history.
- **Lifecycle.** Entries leave `FIXME.md` when the user directs them to be addressed and the corresponding fix lands. The main agent removes the entry as part of the fix's commit. No agent auto-prunes; no append-only rule — explicit user direction drives removal.

### `TODO.md` — user-driven, future work

- **Source.** Only the **main agent** writes to `TODO.md`, and only at user direction (never auto-appended). The trigger is always a confirmation step: main agent asks, user answers, then main agent writes.
- **When the main agent asks.**
  1. **After `/tdd-finalizer` reports the MR.** When `FIXME.md` gained entries this iteration, ask whether any should be elevated to `TODO.md` as scheduled follow-up. If so, move them (remove from `FIXME.md`, add to `TODO.md`) in a separate trivial commit per `git-flow.md`.
  2. **At `/architect` time.** When the design splits into phases or carves scope for "later", ask whether the deferred phases / follow-ups go into `TODO.md` before routing to `/tdd-red`.
  3. **Mid-conversation.** When the user describes work as multi-step or explicitly out of scope ("we'll do Y later", "phase 2 is…"), confirm and write it down.
- **Schema.**
  ```markdown
  # TODO

  Deferred work, multi-phase plans, and follow-ups recorded at user direction. Agents only append at user direction; entries are removed when the work lands or the user says to drop them. See CLAUDE.md → "FIXME.md and TODO.md".

  ## YYYY-MM-DD — <short title>

  <context the user provided; link to originating MR / discussion if useful>
  ```
- **Lifecycle.** Entries leave when the work lands (main agent removes as part of that work's commit) or when the user explicitly says to drop them. No agent auto-prunes.

### Why both files are checked in

`FIXME.md` and `TODO.md` are part of the repo so reviewers and future-you can see what is deferred. They are not scratch — keep entries terse and link to context rather than dumping detail. Both files travel with the change that produced them, so the MR diff is the audit trail.
