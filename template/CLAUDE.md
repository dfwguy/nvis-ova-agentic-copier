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

````text
```
https://example.com/path
```
````

Add any label as plain prose *before* the block, not inside it.

## Git and GitLab connectivity

The Git flow supports both managed environments that route HTTPS through a
sandbox proxy and regular VMs with their own working Git and GitLab setup.
Do not normalize every clone to one transport or credential mechanism.

In a managed sandbox where raw SSH is blocked, configure HTTPS with `glab` as
the Git credential helper:

```bash
glab auth status                          # read-only; verifies glab is logged in to gitlab-master.nvidia.com
git remote set-url origin https://gitlab-master.nvidia.com/<group>/<project>.git
git config credential.https://gitlab-master.nvidia.com.helper '!glab auth git-credential'
git config push.default simple            # bare `git push` pushes only the current branch, never main
```

On a regular VM, retain a working HTTPS or SSH `origin` and the established
credential mechanism (for example, an SSH agent, a Git credential manager, a
stored `glab` login, or a directory-scoped token). Do not replace it merely to
match the sandbox instructions. Environment-provided credentials must be kept
secret: never print, inspect, or copy their values.

Before branching from `main` or publishing, `.claude/git-flow.md` requires a
read-only `git pull --ff-only` and a `glab api` query against the numeric
project ID. Those checks prove Git and GitLab API access separately; success
for one does not prove the other.

Two things worth knowing:

- **`push.default simple` matters.** Under git's old `matching` mode a bare `git push` also tries to push `main`, which branch protection rejects — noisy and confusing. `simple` (git's modern default, set explicitly here) pushes only the current branch.
- **The credential helper must live in the config *file*.** The `!glab auth git-credential` shell-escape only works when persisted with `git config`. Injecting it per-command via `git -c …helper=…` or `GIT_CONFIG_*` does **not** work — git reads `!glab` as a literal helper name (`git: 'credential-!glab' is not a git command`).

> **If you hit a read-only sandbox mount:** some sandbox configurations bind-mount `.git/config` and `$HOME` (`~/.gitconfig`, `~/.config/glab-cli/`) read-only, so `git config` / `glab mr create` writes fail with `Device or resource busy` or `read-only file system`. If that happens, use the VM's approved route for that command. A harmless `.gitmodules: Permission denied` warning on fetch/pull can be ignored.

## Language-Specific Conventions

When working on code in this repo, read the relevant file for conventions, commands, testing patterns, and tooling:

| Language | Conventions file |
|----------|-----------------|
| Python | `.claude/lang/python.md` |
| Bash | `.claude/lang/bash.md` |

Before assigning work or running validators, identify the language of the
changed files. Python applies to `*.py` and Python entry points; Bash applies
to `*.sh` and files with a Bash shebang. Load every matching conventions file
for mixed changes. Do not infer that a project is Python-only just because it
contains `pyproject.toml`; run only the relevant gates, plus shared checks.

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

**Scope: this workflow supports Python and Bash projects.** First detect the
language of the changed files, then run the matching gates: Python uses `uv`,
`ruff`, `ty`, `pytest`, and `tox`; Bash uses `bash -n` and ShellCheck. Mixed
changes run both sets of applicable checks. See the language convention files
for the complete rules.

### Filesystem structure gate

Before finalization, the filesystem-structure judge verifies that Python uses
`src/<package>/` with tests outside production code, and that a Bash application
uses one public `bin/<tool>` entry point backed by implementation in `src/` or
`lib/`. A project may use another layout only when `.claude/project.md` records
the affected language, replaced rule, actual paths, and rationale.

### Routing

The branch / commit / push / MR / return-to-main lifecycle is described once in `.claude/git-flow.md` (loaded above) and applies to **every** change — TDD or trivial. Branch protection on `main` (configured by `/setup-repo`) enforces this: direct pushes are rejected and merging requires approval, so even a one-line edit must go through a feature branch and an MR. The diagram below covers the in-between "do the work" phase for non-trivial changes; the surrounding lifecycle steps come from `git-flow.md`.

```text
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
        ├───────── fan out after both approve ─────────┐
        ▼                                               ▼
 /judge-quality  /judge-test  /tdd-document  /judge-cicd  /cli-syntax-judge  /filesystem-structure-judge
        │                                               │
        └──────── every final gate approves ───────────┘
                               ▼
        /tdd-finalizer  ← always runs; verifies README, runs tox once across supported versions,
                          and commits locally. Rejects to /tdd-document if README is
                          stale.
        │
        ▼
   (Steps 3–4 of git-flow.md: push + open/update MR, then stay on the branch until it merges)
```

### Agent Summary

| Command | Role | Does not |
|---------|------|----------|
| `/architect` | Designs modules, data structures, dependency boundaries | Write code or tests |
| `/remediate` | Establishes an evidence-backed standards baseline and sequences existing-repository fixes | Edit files, weaken standards, or declare skipped gates passing |
| `/tdd-red` | Writes failing tests; stops at Red | Touch implementation |
| `/diataxis-writer` | Creates or updates `docs/` pages as Tutorials, How-to guides, Reference, or Explanation | Touch code, tests, or README.md |
| `/tdd-green` | Minimal implementation to reach Green | Refactor or add untested features |
| `/tdd-judge` | Runs applicable Python/Bash lint and non-test pre-commit checks | Fix issues — rejects and routes back |
| `/diataxis-judge` | Read-only validation that `docs/` is technically accurate and fits its Diátaxis category | Edit docs, code, tests, or README.md |
| `/judge-quality` | Pure code review: design, clarity, language conventions; tags findings **Major** / **Minor** / **Existing** | Run any tooling |
| `/judge-test` | Pure test-quality review (Sonnet): meaningful assertions and language-appropriate test practices; tags findings **Major** / **Minor** / **Existing** | Run any tooling; review production-code quality |
| `/tdd-document` | Ensures README.md accurately orients people landing on the project | Touch `docs/`, code, tests, or agent definitions |
| `/judge-cicd` | Verifies `.gitlab-ci.yml` is in sync with the change and checks pipeline health via `glab` | Edit CI config or retry pipelines |
| `/cli-syntax-judge` | Validates CLI changes against CLIG and the `--json`, `--help`, `--apply`, dry-run, exit-code, and output contracts | Run tooling or edit files |
| `/filesystem-structure-judge` | Verifies the Python/Bash filesystem layout or a specific `.claude/project.md` deviation | Edit files, run tooling, or review non-layout quality |
| `/tdd-finalizer` | Verifies README sync, all final judges including filesystem structure, applicable Python/Bash gates, then commits locally; the orchestrator then follows `git-flow.md` Steps 3–4 (push + MR; stay on the branch until it merges) | Fix issues — rejects and routes back |

Never write implementation before a failing test exists. Never merge without all validators approving.

### Diátaxis documentation

Diátaxis separates documentation by reader need: **Tutorials** teach through a
lesson, **How-to guides** solve a task, **Reference** supports accurate lookup,
and **Explanation** supplies understanding and rationale. The writer and judge
use this separation so `docs/` remains navigable instead of becoming a mixed
collection of catch-all pages. See [`docs/diataxis.md`](docs/diataxis.md) for
the required workflow and category guidance.

### Remediation workflow

When a user says **“Make this repo meet our standards”**, run `/remediate`.
It treats this repository's checked-in standards as the baseline unless the
user supplies another written standard, audits each applicable gate, and turns
every gap into a small normal-workflow slice. Do not claim compliance merely
because a best-effort review found nothing: all required checks plus the
Diátaxis and documentation-index reviews must pass. The workflow never weakens a
configured standard to make the report green.

### Completion requires publication

For any user-requested repository change, local validation is not a completion point. The main agent must continue through `.claude/git-flow.md` Step 3 and report the direct merge-request URL and branch-tip commit in its final response. It may stop before publication only when the user explicitly requests local-only work or a concrete commit, authentication, push, or MR-creation blocker prevents it; report that blocker and the required next action plainly.

### Routing back on rejection

When a judge (`/tdd-judge`, `/diataxis-judge`, `/judge-quality`, `/judge-test`, `/judge-cicd`, `/cli-syntax-judge`, `/filesystem-structure-judge`, `/tdd-finalizer`) rejects and routes back to an earlier agent, proceed automatically — **do not pause to ask the user for approval**. Rejection-and-reroute is a normal part of the workflow, not a decision point. Only stop for the user when there is a genuine decision to make (ambiguous design, conflicting requirements, destructive action). A judge's structured reroute is not such a decision.

### `/judge-quality` and `/judge-test` severity model

`/judge-quality` (production-code review) and `/judge-test` (test-quality review) both classify every finding as one of:

- **Major** — defect in this change. Triggers `Overall: Reject`. `/judge-quality` reroutes to `/tdd-green`; `/judge-test` reroutes to `/tdd-red` (test defects belong to the Test Writer) — except a `/judge-test` Major flagging production code that *forces* internal mocking (a missing injection point), which reroutes to `/tdd-green`. Reroutes are automatic.
- **Minor** — non-blocking style/clarity nit caused or kept by this change. The main agent creates a GitLab issue from the Bug template, records the finding, and labels it `type::quality`. Does not reroute. See "GitLab issues" below.
- **Existing** — pre-existing issue in surrounding code or tests, not introduced by this change. Surfaced verbatim in the judge's report but not automatically filed. Never blocks.

Both reviewers end their return note with `Created <N> GitLab issues` (or `No new issues`). The main agent forwards those lines to `/tdd-finalizer` so the finalizer can link the issues from the MR. Existing findings are surfaced to the user at the same point but are not automatically filed.

`/judge-quality` owns production-code quality; `/judge-test` runs on **Sonnet** and owns test quality — meaningful assertions that would catch a wrong implementation, `pytest.mark.parametrize`, `hypothesis` for pure functions, order-independence (`pytest-randomly`), dependency-injected test doubles instead of internal-symbol patching, and test hygiene. The two reviewers do not duplicate each other.

## GitLab issues

GitLab issues are the system of record for deferred work, planned work, and non-blocking quality findings. Do not create or maintain `TODO.md` or `FIXME.md`; generated projects use the templates in `.gitlab/issue_templates/` instead.

### Templates and fields

- Use **Bug** for defects and deferred Minor quality findings. It captures reproduction steps, expected and actual behavior, environment, priority, and severity.
- Use **Feature** for requested capabilities and planned follow-up work. It captures the problem, intended outcome, acceptance criteria, scope, dependencies, priority, and severity.
- In every issue choose exactly one **priority**: `P0` (stop-ship), `P1` (current milestone), `P2` (planned), or `P3` (low/no current commitment).
- In every issue choose exactly one **severity**: `S1` (critical), `S2` (major), `S3` (moderate), or `S4` (minor). Priority measures urgency; severity measures impact. For features, severity is the impact of not delivering the capability.

### Workflow

- The main agent files a Bug issue for each new **Minor** finding after an approving quality or test review, uses `S4` unless the finding has greater impact, and applies the `type::quality` label when that label exists. The issue must link to the relevant file, line, MR, or review note.
- Existing findings remain in the review report and are never filed automatically.
- When design, planning, or conversation identifies deferred scope, the main agent files a Feature issue after confirming it with the user. Link it from the originating issue or MR.
- Close issues only when the associated work lands or a user explicitly drops it. Link MRs with `Closes #<issue>` when the MR resolves the work.

### Suggested labels

Create scoped labels in GitLab once for consistent filtering: `type::bug`, `type::feature`, `type::quality`, `priority::p0` through `priority::p3`, and `severity::s1` through `severity::s4`. The templates remain useful without these labels, but the labels make boards and triage views easier to maintain.
