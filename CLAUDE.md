# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@.claude/project.md

@.claude/git-flow.md

## Repository lock-down

Run `grep -Fq 'All changes go through a merge request.' CONTRIBUTING.md`
before repository work. If it fails, load and follow
`.claude/skills/repository-setup/SKILL.md` before doing anything else in the
repository.

## Sandbox

Run commands through the configured execution path first. When a known
environment failure occurs, apply its documented recovery and continue without
asking the operator to repeat it. Keep recovery bounded: do not rerun the same
known-failing route, but use the next documented sandbox, credential, host, or
CLI compatibility path when it is available. Report a blocker only after the
bounded recovery path is exhausted or when the next recovery requires new
authority; include the attempted recoveries and the final non-secret
diagnostic concisely.

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
| Diátaxis documentation process | `DIATAXIS.md` |

## Multi-Agent Workflow

All feature work routes through explicit agent roles. Start every role from its
portable `.claude/skills/<role>/SKILL.md` directory; its `model-tier` metadata
selects the configured capability tier. The matching `.claude/agents/` file is
a compatibility reference for the detailed role instructions.

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

The branch / commit / push / MR / return-to-main lifecycle is described once in `.claude/git-flow.md` (loaded above) and applies to **every** change — TDD or trivial. Branch protection on `main` (configured by the repository setup procedure) enforces this: direct pushes are rejected and merging requires approval, so even a one-line edit must go through a feature branch and an MR. The diagram below covers the in-between "do the work" phase for non-trivial changes; the surrounding lifecycle steps come from `git-flow.md`.

```text
New feature or non-trivial change
        │
        ▼
   (Step 1 of git-flow.md: ensure feature branch)
        │
        ▼
   Architect           ← design first; skippable only for trivial single-function changes
        │
        ▼
   Test Writer ───────── Documentation Writer
   failing tests          docs/ content by Diátaxis category
        │                         │
        └──────────────┬──────────┘
                       ▼
   Implementer          ← minimal code to pass tests
        │
        ▼
 Non-functional Reviewer ← reviews shared test-results.xml timing evidence
        │
        ├─────────────────────────┐   (run in parallel after non-functional review)
        ▼                         ▼
   Implementation Judge     Documentation Judge
   implementation checks    docs/ accuracy + Diátaxis fit
        │                         │
        └──────────────┬──────────┘
                       ▼
        ├───────── fan out after both approve ─────────┐
        ▼                                               ▼
 Quality Judge  Test Judge  README Reviewer  CI/CD Judge  CLI Judge  Filesystem Judge
        │                                               │
        └──────── every final gate approves ───────────┘
                               ▼
        Finalizer        ← always runs; verifies README, runs tox once across supported versions,
                          and commits locally. Rejects to the README Reviewer if README is stale.
        │
        ▼
   (Step 3 of git-flow.md: push + open/update MR)
        │
        ▼
 Pipeline Performance Judge ← bounded analysis of the current MR pipeline after it becomes terminal
        │
        ▼
 Findings Triage           ← files deduplicated GitLab issues for accumulated Minor findings
        │
        ▼
   (Step 4 of git-flow.md: hand off as awaiting_human; never wait or merge)
```

### Agent Summary

| Command | Role | Does not |
|---------|------|----------|
| Architect | Designs modules, data structures, dependency boundaries | Write code or tests |
| Remediator | Establishes an evidence-backed standards baseline and sequences existing-repository fixes | Edit files, weaken standards, or declare skipped gates passing |
| Test Writer | Writes failing tests; stops at Red | Touch implementation |
| Documentation Writer | Creates or updates `docs/` pages as Tutorials, How-to guides, Reference, or Explanation | Touch code, tests, or README.md |
| Implementer | Minimal implementation to reach Green | Refactor or add untested features |
| Non-functional Reviewer | Reviews shared JUnit test output and performance evidence before final validation | Rerun tests, edit code, or wait for finalization |
| Implementation Judge | Runs applicable Python/Bash lint and non-test pre-commit checks | Fix issues — rejects and routes back |
| Documentation Judge | Read-only validation that `docs/` is technically accurate and fits its Diátaxis category | Edit docs, code, tests, or README.md |
| Quality Judge | Pure code review: design, clarity, language conventions; tags findings **Major** / **Minor** / **Existing** | Run any tooling |
| Test Judge | Pure test-quality review (Sonnet): meaningful assertions and language-appropriate test practices; tags findings **Major** / **Minor** / **Existing** | Run any tooling; review production-code quality |
| README Reviewer | Ensures README.md accurately orients people landing on the project | Touch `docs/`, code, tests, or agent definitions |
| CI/CD Judge | Verifies `.gitlab-ci.yml` is in sync with the change and checks pipeline health via `glab` | Edit CI config or retry pipelines |
| Pipeline Performance Judge | After publication, analyzes the current MR pipeline's queue, job timing, bootstrap, image, cache, and failure evidence | Edit code or CI, retry/cancel jobs, approve, or merge |
| Findings Triage | At the end, creates deduplicated GitLab Bug issues for actionable Minor findings from any judge; its MR remediation issues update the originating open MR | Edit code, run validation, commit, push, approve, or merge |
| CLI Judge | Validates CLI changes against CLIG and the `--json`, `--help`, `--apply`, dry-run, exit-code, and output contracts | Run tooling or edit files |
| Filesystem Judge | Verifies the Python/Bash filesystem layout or a specific `.claude/project.md` deviation | Edit files, run tooling, or review non-layout quality |
| Finalizer | Verifies README sync, all final judges including filesystem structure, applicable Python/Bash gates, then commits locally; the orchestrator then follows `git-flow.md` Steps 3–4 (push + MR; hand off without waiting for merge) | Fix issues — rejects and routes back |

Never write implementation before a failing test exists. The Implementer shares
Python timings through the gitignored `test-results.xml` JUnit artifact, and
the Non-functional Reviewer consumes it before later validation. Never merge
without all validators approving.

### Diátaxis documentation

Diátaxis separates documentation by reader need: **Tutorials** teach through a
lesson, **How-to guides** solve a task, **Reference** supports accurate lookup,
and **Explanation** supplies understanding and rationale. The writer and judge
use this separation so `docs/` remains navigable instead of becoming a mixed
collection of catch-all pages. See [`DIATAXIS.md`](DIATAXIS.md) for
the required workflow and category guidance.

### Remediation workflow

When a user says **“Make this repo meet our standards”**, use the Remediator role.
It treats this repository's checked-in standards as the baseline unless the
user supplies another written standard, audits each applicable gate, and turns
every gap into a small normal-workflow slice. Do not claim compliance merely
because a best-effort review found nothing: all required checks plus the
Diátaxis and documentation-index reviews must pass. The workflow never weakens a
configured standard to make the report green.

### Completion requires publication

Every repository change must publish a feature branch and open or update a
merge request. A publication failure is a blocker, not completion. For any
user-requested repository mutation, local validation is not a completion point.

#### Serial agent-work worker exception

For a coding agent invoked by `agent-work run-next`, this exception takes
precedence over repository-wide publication instructions. The worker edits and
validates only uncommitted working-tree changes on its prepared
`agent/issue-<IID>` branch. It must not commit, push, create a merge request,
approve, or merge; the trusted runner alone stages, commits, pushes, and opens
the merge request from the worker's one delimited draft. This exception applies
only inside that runner-owned worker invocation, not to the runner or an
ordinary user-requested repository change.

The main agent must continue through `.claude/git-flow.md` Step 3 and report the
direct merge-request URL and branch-tip commit in its final response. A concrete
commit, authentication, push, or MR-creation failure is a blocker, never
completion; report that blocker and the required next action plainly. Read-only
inspection is the only work that does not require publication.

### Routing back on rejection

When a judge rejects and routes work back to an earlier role, proceed automatically — **do not pause to ask the user for approval**. Rejection-and-reroute is a normal part of the workflow, not a decision point. Only stop for the user when there is a genuine decision to make (ambiguous design, conflicting requirements, destructive action). A judge's structured reroute is not such a decision.

### Quality and Test Judge severity model

The Quality Judge (production-code review) and Test Judge (test-quality review) both classify every finding as one of:

- **Major** — defect in this change. Triggers `Overall: Reject`. The Quality Judge reroutes to the Implementer; the Test Judge reroutes to the Test Writer (test defects belong to the Test Writer) — except a Test Judge Major flagging production code that *forces* internal mocking (a missing injection point), which reroutes to the Implementer. Reroutes are automatic.
- **Minor** — non-blocking style/clarity nit caused or kept by this change. Findings Triage creates a deduplicated GitLab issue from the Bug template after post-publication pipeline review, and labels it `type::quality` when available. Does not reroute. See "GitLab issues" below.
- **Existing** — pre-existing issue in surrounding code or tests, not introduced by this change. Surfaced verbatim in the judge's report but not automatically filed. Never blocks.

Both reviewers emit their Minor records in the standard output format. Findings Triage runs after the Finalizer and Pipeline Performance Judge, then ends with `Created <N> GitLab issues` (or `No new issues`). Existing findings are surfaced to the user but are not automatically filed.

The Quality Judge owns production-code quality; the Test Judge runs on **Sonnet** and owns test quality — meaningful assertions that would catch a wrong implementation, `pytest.mark.parametrize`, `hypothesis` for pure functions, order-independence (`pytest-randomly`), dependency-injected test doubles instead of internal-symbol patching, and test hygiene. The two reviewers do not duplicate each other.

## GitLab issues

GitLab issues are the system of record for deferred work, planned work, and non-blocking quality findings. Do not create or maintain Markdown backlog files; both this repository and generated projects use the templates in `.gitlab/issue_templates/` instead.

### Templates and fields

- Use **Bug** for defects and deferred Minor quality findings. It captures reproduction steps, expected and actual behavior, environment, priority, and severity.
- Use **Feature** for requested capabilities and planned follow-up work. It captures the problem, intended outcome, acceptance criteria, scope, dependencies, priority, and severity.
- In every issue choose exactly one **priority**: `P0` (stop-ship), `P1` (current milestone), `P2` (planned), or `P3` (low/no current commitment).
- In every issue choose exactly one **severity**: `S1` (critical), `S2` (major), `S3` (moderate), or `S4` (minor). Priority measures urgency; severity measures impact. For features, severity is the impact of not delivering the capability.

### Workflow

- Findings Triage files a Bug issue for each deduplicated, actionable **Minor** finding from any judge after the Finalizer and terminal MR-pipeline review. It uses `S4` unless the finding has greater impact, applies `type::quality` when that label exists, and links to the relevant file, line, MR, pipeline, job, or review note.
- When the finding belongs to an open MR, Findings Triage creates an **MR remediation issue** with the verified MR IID, source branch, and source SHA in its `agent-mr-remediation` marker. The next worker verifies those fields against GitLab, resumes that source branch, and must update the existing merge request with `Closes #<triage-issue>`; it never opens a second MR. A stale, merged, cross-project, or mismatched marker is failed without touching the MR.
- Existing findings remain in the review report and are never filed automatically.
- When design, planning, or conversation identifies deferred scope, the main agent files a Feature issue after confirming it with the user. Link it from the originating issue or MR.
- Close issues only when the associated work lands or a user explicitly drops it. Link MRs with `Closes #<issue>` when the MR resolves the work.

### Suggested labels

Create scoped labels in GitLab once for consistent filtering: `type::bug`, `type::feature`, `type::quality`, `priority::p0` through `priority::p3`, and `severity::s1` through `severity::s4`. The templates remain useful without these labels, but the labels make boards and triage views easier to maintain.
