---
title: Serial GitLab agent-loop reference
type: reference
summary: Look up loop commands, states, data, and limits.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-10-02
related:
  - path: ../how-to/configure-agent-capacity.md
    why: Explains how to prepare capacity evidence before a worker launch.
  - path: ../how-to/validate-template-changes.md
    why: Shows how to publish a verified template change for review.
  - path: ../how-to/setup-serial-gitlab-agent-loop.md
    why: Configures, recovers, and migrates the loop.
  - path: ../how-to/resolve-factory-issues.md
    why: Applies one reviewed decision to exceptional retained issue evidence.
  - path: ../how-to/update-a-generated-project.md
    why: Explains the project-owned dependency step before opting into the preview.
  - path: ../explanation/serial-gitlab-agent-loop.md
    why: Explains the architecture behind this contract.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains the recoverable state machine in detail.
  - path: ../explanation/agentic-workflow.md
    why: Explains the Judge role that owns validation sessions.
  - path: license-compliance.md
    why: Defines the license check used before full-hook validation.
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

# Serial GitLab agent-loop reference

## Commands

```text
agent-work scope capture --issue IID --scope-file PATH [--apply] [--json] [--debug]
agent-work scope inspect --issue IID [--apply] [--json] [--debug]
agent-work cli-contract inspect --surface {exec,sandbox} [--executable PATH]
                                [--json] [--debug] [--apply]
agent-work loop run [--apply] [--max-segments COUNT]
                    [--max-activation-seconds SECONDS]
                    [--json] [--debug]
agent-work loop status [--issue-iid IID] [--apply] [--json] [--debug]
agent-work loop retry --issue-iid IID [--apply --plan-file PATH] [--json] [--debug]
agent-work loop canary [--apply --plan-file PATH] [--json] [--debug]
agent-work loop smoke [--apply] [--json] [--debug]
agent-work loop recover --issue-iid IID [--apply --plan-file PATH]
                        [--max-segments COUNT]
                        [--max-activation-seconds SECONDS] [--json] [--debug]
agent-work recovery inventory [--issue-iid IID] [--json] [--apply]
agent-work recovery resolve --issue-iid IID
                            --choice {retry,close,reopen,relabel,supersede}
                            --reason TEXT [--evidence-url URL ...]
                            [--replacement-issue-iid IID]
                            [--target-label LABEL] [--plan-file PATH]
                            [--apply] [--json]
agent-work credential rotate --environment-file PATH --unit-name NAME [--apply] [--json] [--debug]
agent-work service-environment migrate --environment-file PATH --unit-name NAME [--apply | --dry-run] [--json] [--debug]
agent-work worker-result build --issue-iid IID --input-file PATH
                               [--broker-socket PATH]
                               [--broker-nonce NONCE]
                               [--json] [--apply] [--debug]
agent-work interactive-commit red --worktree PATH --branch-ref REF
    --base-sha SHA --red-ref REF [--red-sha PRIOR_RED_SHA] --red-tree TREE
    --message-file PATH [--apply] [--json] [--debug]
agent-work interactive-commit diagnose --worktree PATH --branch-ref REF
    --base-sha SHA --red-ref REF --red-sha SHA --candidate-tree TREE
    [--baseline-module PATH ...] [--apply] [--json] [--debug]
agent-work interactive-commit green --worktree PATH --branch-ref REF
    --base-sha SHA --red-ref REF --red-sha SHA --judge-tree TREE
    --message-file PATH [--apply] [--json] [--debug]
agent-work interactive-commit verify --worktree PATH --branch-ref REF
    --base-sha SHA --red-ref REF --red-sha SHA --green-sha SHA
    --judge-tree TREE [--json] [--debug]
agent-work reconcile-commit commit --worktree PATH --branch-ref REF
    --source-sha SHA --target-sha SHA --red-ref REF --red-sha SHA
    --judge-tree TREE --message-file PATH [--apply | --dry-run] [--json] [--debug]
agent-work reconcile-commit verify --worktree PATH --branch-ref REF
    --source-sha SHA --target-sha SHA --red-ref REF --red-sha SHA
    --judge-tree TREE --reconcile-sha SHA [--apply] [--json] [--debug]
agent-work merge-request publish --source-branch BRANCH --target-branch BRANCH
                                 --title TEXT --problem TEXT --outcome TEXT
                                 --validation TEXT [--validation TEXT ...]
                                 [--impact TEXT] [--human-action TEXT]
                                 [--tracking TEXT] [--issue-iid IID]
                                 [--apply] [--json]
agent-work issue publish --title TEXT --problem TEXT --outcome TEXT
                         --acceptance TEXT [--acceptance TEXT ...]
                         --marker TEXT
                         [--impact TEXT] [--additional-context TEXT]
                         [--priority TEXT] [--severity TEXT]
                         [--label TEXT] [--issue-iid IID] [--feedback-audit]
                         [--apply] [--json] [--debug]
agent-work validation run --candidate-tree TREE [--serial-timing --baseline-sha SHA]
                          [--changed-path PATH]... [--apply] [--json]
agent-work validation retry --candidate-tree TREE [--serial-timing --baseline-sha SHA]
                            [--changed-path PATH]... [--apply] [--json]
agent-work validation window [--apply] [--json] -- COMMAND [ARG ...]
agent-work validation status --candidate-tree TREE
                             [--serial-timing --baseline-sha SHA]
                             [--changed-path PATH]...
                             [--wait-seconds SECONDS] [--apply] [--json]
agent-work candidate stage --issue-iid IID [--candidate-socket PATH]
                           [--broker-nonce NONCE] [--apply] [--json]
agent-work candidate commit --issue-iid IID --candidate-tree TREE
                            [--candidate-socket PATH] [--broker-nonce NONCE] [--apply] [--json]
agent-work candidate changed-paths --issue-iid IID --candidate-tree TREE
                                   [--candidate-socket PATH] [--broker-nonce NONCE] [--apply] [--json]
agent-work candidate read --issue-iid IID --candidate-tree TREE
                          --path PATH --side {old,new}
                          --offset NONNEGATIVE_INT --length 1..131072
                          [--candidate-socket PATH] [--broker-nonce NONCE] [--apply] [--json]
agent-work candidate validate --issue-iid IID --candidate-tree TREE
                              [--candidate-socket PATH] [--broker-nonce NONCE] [--apply] [--json]
agent-work candidate status --issue-iid IID --candidate-tree TREE
                            [--candidate-socket PATH] [--broker-nonce NONCE] [--apply] [--json]
agent-work candidate committed-changed-paths --issue-iid IID --mr-iid IID
    --candidate-sha SHA --candidate-tree TREE --role {router,judge,finalizer}
    [--apply] [--json]
agent-work candidate committed-read --issue-iid IID --mr-iid IID
    --candidate-sha SHA --candidate-tree TREE --role {router,judge,finalizer}
    --path PATH --side {old,new} --offset NONNEGATIVE_INT --length 1..131072
    [--apply] [--json]
agent-work migration preview [--json] [--debug]
agent-work migration apply --plan-file PATH --apply [--json] [--debug]
agent-work upstream-finding preview --manifest-file PATH --upstream-host HOST --upstream-project-id POSITIVE_INT --upstream-credential-file PATH [--apply] [--json] [--debug]
agent-work slow-root reconcile --manifest-file PATH [--json] [--apply] [--debug]
```

| Command | Required input | Optional control or result |
| --- | --- | --- |
| `scope capture` | `--issue IID` and `--scope-file PATH` | Previews a scope binding; `--apply` may persist it only in an existing matching issue record. |
| `scope inspect` | `--issue IID` | Verifies the saved binding without writing it; `--apply` is accepted and changes nothing. |
| `cli-contract inspect` | `--surface exec` or `--surface sandbox` | Read-only advisory report for the exact configured Codex executable; `--executable PATH` selects an absolute binary path; `--apply` changes nothing. |
| `loop run` | None | `--apply` performs one activation; segment and activation-time limits bound it. |
| `loop status` | Optional exact `--issue-iid` | Read-only status; `--apply` is accepted for consistency. |
| `loop retry` | Exact `--issue-iid`; the exact reviewed `--plan-file PATH` when applying | Reuses recovery checks and requires exactly one `safely_retryable` inventory result. |
| `loop canary` | `--plan-file PATH` when applying | Bare invocation creates the immutable plan. |
| `loop smoke` | Codex and `--apply` for a real smoke | Bare invocation previews the isolated smoke. Use the companion user service for the installed route. |
| `loop recover` | Exact `--issue-iid`; reviewed `--plan-file PATH` when applying | Segment and activation-time limits retain `loop run` controls. |
| `recovery inventory` | Optional exact `--issue-iid` | Read-only reconciliation; `--apply` changes nothing. |
| `recovery resolve` | Exact `--issue-iid`, explicit `--choice`, and `--reason` | Applying also needs the exact reviewed `--plan-file`; some choices need evidence, a replacement IID, or a target label. |
| `credential rotate` | `--environment-file PATH`, `--unit-name NAME` | `--apply` performs the bounded rotation. |
| `service-environment migrate` | `--environment-file PATH`, `--unit-name NAME` | Bare invocation previews; choose `--apply` or `--dry-run`. |
| `worker-result build` | Exact `--issue-iid`, `--input-file PATH`, private `--broker-socket PATH`, and `--broker-nonce NONCE` when applying | The live producer invokes the trusted controller-checkout builder through the socket. An accepted envelope stays in controller memory. |
| `interactive-commit red` | Worktree, branch, base, Red ref, test-only tree, message, and optional prior Red commit | `--apply` preflights and rechecks the exact changed test tree, then makes a test-only Red commit with `--no-verify`. Its JSON result lists accepted missing APIs in `residual_details`. |
| `interactive-commit diagnose` | Worktree, branch, base, authentic Red ref and commit, and exact `--candidate-tree` | Previews by default; `--apply` runs bounded focused feedback. `--baseline-module` needs trusted host approval and unchanged source. No commit or validation proof is created. |
| `interactive-commit green` | Worktree, branch, base, final Red commit and ref, Judge tree, and message | After Judge reads the exact tree, `--apply` runs its license preflight and shared cheap gate, then makes the sole normal Green commit; its installed hook runs the full suite. |
| `interactive-commit verify` | Green commit, Judge tree, final Red commit and ref, branch, and base | Checks commit, tree, and ancestry read-only. |
| `reconcile-commit commit` | Worktree, branch, captured source and exact target commits, retained Red ref and commit, Judge tree, and message file | Omitting `--apply` or using `--dry-run` previews without changing Git state. `--apply` checks the pending merge and exact tree, runs the license preflight and shared cheap gate, then makes one normal commit with the source first and target second. The installed hook runs the full suite. |
| `reconcile-commit verify` | Resulting reconciliation commit, worktree, branch, source and target commits, retained Red ref and commit, and Judge tree | Checks the ordered parents, tree, unchanged Red ref, cleared merge state, and clean worktree read-only. Optional `--apply` is accepted but changes nothing. |
| `merge-request publish` | Source and target branches, title, problem, outcome, and one or more validations | `--apply` creates or updates one exact MR; `--json` returns its result. |
| `issue publish` | Title, problem, outcome, repeated acceptance criteria, and a task-specific marker | `--apply` creates or updates one exact issue in the configured checkout project. `--feedback-audit` checks new automatic feedback issues; `--json` returns the result. |
| `validation run` | `--candidate-tree TREE` | `--apply` runs the read-only license preflight and shared cheap gate on the exact staged tree before the one full hook run. Serial work adds `--serial-timing`, `--baseline-sha SHA`, and repeatable `--changed-path PATH` for paired timing proof. |
| `validation retry` | `--candidate-tree TREE` with an eligible saved failure | Bare invocation previews. `--apply` retries pre-hook cleanup, or performs one timing-only retry after a successful hook. Serial work repeats `--serial-timing`, the baseline SHA, and changed paths. |
| `validation window` | A child command after `--` | `--apply` holds one project timing admission while the child runs; `--json` returns a bounded result. |
| `validation status` | `--candidate-tree TREE` | Optional `--wait-seconds` bounds polling; it stays read-only. Serial work repeats `--serial-timing`, the baseline SHA, and changed paths; status checks the saved timing proof or retry result. |
| `candidate stage` | Exact issue, private socket, and nonce | The live serial worker requests controller-owned staging of observed paths. |
| `candidate commit` | Same identity and returned tree | The controller makes or replays the coding Green commit. Its installed hook validates that exact tree once; the response binds the commit, tree, and Red checkpoint. |
| `candidate changed-paths` | Same identity and returned tree | Lists canonical changed paths and old and new file metadata from the controller. |
| `candidate read` | Same identity and returned tree, changed path, side, offset, and length | Reads one bounded byte range from the exact old or new Git blob. |
| `candidate validate` | Same identity and returned tree | Content and remediation work, and coding work with a pending target merge, run or return the durable staged-tree Judge result. |
| `candidate status` | Same identity and returned tree | Those staged-tree routes read the durable result without rerunning the hook. Ordinary coding work replays `candidate commit` to recover its exact result. |
| `candidate committed-changed-paths` | Exact issue, open merge request, current commit, tree, and review role | Reads changed paths and old and new Git blob metadata after a restart, without a live candidate socket. |
| `candidate committed-read` | Same identity, changed path, side, offset, and length | Reads one bounded byte range from the exact committed Git blob. |
| `migration preview` | None | Returns the planned migration actions. |
| `migration apply` | Reviewed `--plan-file PATH` and `--apply` | Applies only unchanged, preflighted actions. |
| `upstream-finding preview` | Manifest, upstream host, project ID, and credential file | `--apply` is the only mutation switch. |
| `slow-root reconcile` | JSON manifest at `--manifest-file PATH` | Previews 1–100 roots by default; `--apply` creates at most one unmatched confirmed root per invocation. `--debug` shows a traceback on failure. |

### Human scope bindings

`scope capture` reads a bounded JSON scope request from `--scope-file`.
The request names the task and source issue. It includes the scope revision,
source description digest, criteria, requested revision and time, and selected
approval reply. Criterion order and stable IDs are part of the canonical scope
digest. The selected reply must match the exact reviewed request and source
revision. A changed description, scope, reply, or identity cannot reuse old
approval.

The configured GitLab adapter reads the source issue, replies, and numeric
author facts. It uses authenticated requests. Those facts alone do not prove
human approval. A trusted host must inject a verifier. It must confirm the
person, reply meaning, and reviewed revision. No command flag, environment
value, worker JSON, or cached verdict can supply that proof. Without the host
verifier, the result is `unknown`. Source evidence cannot prove approval if it
is ambiguous, edited, missing, incomplete, or unavailable.

Capture previews by default. With `--apply`, it may bind the result only to an
existing matching issue ledger record. It uses that record's existing lock.
It creates no workflow record or separate approval store. `scope inspect`
reads the saved binding. It checks the authenticated source and host proof
without writing. Inspect accepts `--apply`, which changes nothing.
Valid nonterminal bindings compare source evidence before and after the host
check. Terminal `stale` or `revoked` bindings and invalid digests refuse
locally. They read no source and call no host verifier. A saved `approved`
binding is evidence to recheck. It never grants execution authority.
States are `pending`, `approved`, `unknown`, `stale`, and `revoked`.
Stale or revoked bindings need a fresh reviewed revision, a fresh request,
and a distinct approval event.

JSON reports use stdout. Unknown or failed human summaries use stderr and
exit with a nonzero status. Debug logging uses the existing local log.
It records safe IDs or reasons. It includes no raw reply, private data, or
token. The commands change no factory phase, label, merge request, or
activation state. Scope capture adds no design MR or approval channel.
Gates and shared-role delivery in issues 361, 362, and 385 remain separate work.

### CLI contract inspection

`cli-contract inspect` reports whether the selected Codex binary accepts the
fixed command forms used by the factory. It is advisory and read-only. It adds
no required workflow gate and grants no role-launch or sandbox authority.
The executable comes from `--executable`, then `AGENT_WORK_AGENT_PATH`, then
one lookup of `codex` on `PATH`. The selected executable path must be absolute.
`--apply` is accepted for compatibility; inspection is always read-only.

The inspector runs at most five fixed probes: version, root help, surface help,
help with the factory's option placement, and version again. The surface is
`exec` or flat `sandbox`. The placement probe puts `--ask-for-approval never`
before the surface name. Each probe uses an empty temporary home and the
housekeeping command timeout. It sends no prompt and runs no work payload,
feature inventory, MCP server, or role profile. It reads no private auth,
controller state, or user config and makes no network request.

The JSON report gives `executable`, `surface`, `checkout`, `version`,
`checked_argv`, `state`, and fixed safe `diagnostics`. States are `supported`,
`unsupported`, `unavailable`, `invalid_output`, and `version_changed`. Exit
status is `0` for supported grammar, `1` for an inspection failure, and `2`
for invalid arguments or a request with relative paths. It omits raw help,
stderr, and exception text. Configuration
(`effective_config`) and isolation (`isolation`) always remain `unknown`.
A `supported` state proves only that the sampled help and parse checks passed.
It does not prove that options take effect or that the sandbox blocks access.
The two version samples detect reported version drift; they cannot prove that
the binary was not replaced by another with the same version.

Supported human reports go to stdout. Failed human reports go to stderr and
leave stdout empty. With `--json`, valid reports go to stdout for either
outcome; the exit status still shows success or failure. `--debug` is accepted
for compatibility. It keeps the fixed safe diagnostics and never prints raw
probe output or a traceback.

Each probe's stdout and stderr must each fit within 65,536 bytes to count as
evidence. The `evidence_limit` is `accepted-output-only`: this cap limits
accepted evidence, not the memory used to capture subprocess output. Failed,
unavailable, ambiguous, or oversized evidence cannot support a `supported`
report. Existing host audits and live tool checks still own activation proof.

### Smoke

`loop smoke` is a Codex-only delegation check. Do not use it with Claude or
Pi. The installed route is the companion user service, which shares the normal
service's `EnvironmentFile`, calls its wrapper with `--smoke`, and has no
timer.

Without `--apply`, the command prints `preview` and exits `0`. With `--json`,
it returns this preview object:

```json
{"schema_version":1,"command":"loop.smoke","applied":false,"outcome":"preview"}
```

With `--apply`, it uses an isolated private workspace. A successful check
prints `passed` and exits `0`. With `--json`, it returns this object:

```json
{"schema_version":1,"command":"loop.smoke","applied":true,"outcome":"passed","broker_accepted":true}
```

The applied check requires the live Codex child proof and broker acceptance.
It does not create or change an issue, merge request, normal-loop ledger
record, or retained worktree. A failed check exits `1` with a bounded safe
diagnostic. Invalid command use exits `2`. `--debug` is accepted but does not
change smoke output.

### Loop run

| `loop run` mode or condition | Behavior |
| --- | --- |
| Applied run | Runs one bounded, recoverable activation for one issue. The worker owns the full workflow session. |
| Default limits and overrides | Allows 16 workflow attempts and one 10,800-second (three-hour) work budget. `--max-segments` and `--max-activation-seconds` override those limits. Each workflow call gets the positive, ceil-rounded time left in that budget. On expiry, normal retry and durable-worktree recovery apply. |
| Before work selection | Reconciles prerequisite-label projections for split children. If it fixes projection drift, it does only that work. It does not claim a dependent issue, create a ledger record or worktree, or start a worker. A partial remote failure is retryable, and it is safe to repeat the reconciliation. |
| Open closing merge request | Before claiming an eligible issue, skips it when an open merge request targets the configured project's default branch and GitLab confirms it closes that issue. The source may be a fork. Before publishing local work, checks again and blocks publication if another merge request now closes it; the local worktree and ledger remain for review. An unavailable or malformed closing-issue response fails closed. |
| Without `--apply` | Reports each current and intended prerequisite-label projection but makes no writes. |
| Open factory MR awaiting review | `AGENT_WORK_FACTORY_USERNAME` is needed and must equal the authenticated GitLab service username. The loop reads bounded general comments and inline discussion replies. New human feedback wakes only that issue for existing-MR remediation. It acknowledges no-change feedback once. Code changes reuse the same MR and preserve its title, description, and closing reference. |
| Finding against a merged MR | An open finding issue with an exact remediation marker starts ordinary work on a fresh `agent/issue-<IID>` branch from refreshed `main` only after the runner verifies the configured project, source branch, target branch, and that the marker commit belongs to the merged MR. The merged MR remains context for the finding; publication uses the new issue's branch and MR. A blocked mismatch with no retained worktree may be rechecked against the same evidence. Missing or ambiguous evidence stays blocked. |
| Idle recovery admission | Only after an applied activation has no ordinary work, the controller may consider one dirty ordinary `BLOCKED` issue whose Red checkpoint failed. It excludes confidential issues and issues marked `agent-work::manual`. It checks the original issue, ledger, worktree, dependencies, owner, and open merge requests before advice and rechecks the issue and other evidence before creating a child. |
| Idle recovery child | A bounded read-only adviser may return `create_child` or `defer`, but cannot authorize a write. The controller persists an intent before creating one clean child, then verifies that the child is open, nonconfidential, and unlabeled. A lost response leaves the intent pending; a later pass may adopt one exact marker match without reposting. Repeated failure cools the original while another issue may proceed. The child runs through the normal workflow on a later activation. The original stays blocked and untouched. The applied report returns `recovery_child_created` or `recovery_child_adopted`. |

The tables list the commands and their use.

### Interactive Red and Green commits

Red uses `interactive-commit red --apply` to make each test-only commit with
`--no-verify`. A later Red commit names its predecessor with `--red-sha`.
Before the commit, the boundary runs read-only Ruff check, Ruff format check,
and Ty on the changed Python tests in the exact staged tree. Test-local errors,
an unavailable preflight, or a tool or malformed-output failure block the
commit. The boundary rechecks the candidate after preflight. Ty warnings may
pass. Expected missing production members, package exports, or new modules may
pass only when trusted source ownership identifies the production package.
With `--json`, `residual_details` lists each accepted missing API by test path,
line, and production name. A passing Red preflight is required before any Red ref
is retained. Preview and Green JSON results list no residual details.
An applied replay of the exact Red ref preflights the same committed test tree
again and returns fresh `residual_details`. It makes no new commit or ref update.
Green keeps tests frozen. After Judge reads the exact staged tree, Judge uses
`interactive-commit green --apply` to make the sole normal Green commit. The
installed hook runs the full suite once.

Without `--apply`, `red` and `green` preview the operation and leave the
branch, refs, index, and worktree unchanged. Finalizer uses
`interactive-commit verify` to check the committed tree and Red ancestry
without changing the repository. The review branch retains its Red and Green
commits for a human squash merge.

After a restart, reconstruct the workflow from source code, Git commits, and
the exact issue and merge request. Rerun any needed checks. Do not restore an
agent session or stored role and session receipts.

### Interactive focused diagnostics

`interactive-commit diagnose` provides early feedback on the staged candidate
before Judge's sole full native validation. It uses the same retained Red,
branch, base, tree, test-freeze, merge, and staged-only checks as the commit
boundary. It does not commit or grant validation, review, or publication
permission.

| Area | Diagnostic contract |
| --- | --- |
| Preview and selection | Without `--apply`, verifies the candidate and lists selected modules without starting pytest. The standalone command selects authentic Red test modules only. Each repeatable `--baseline-module` must name a canonical tracked regular test module approved by trusted host composition and byte-unchanged from base to candidate. No arbitrary pytest arguments or node IDs are accepted. |
| Trusted caller | Main invokes the installed production `cli.main` with `approved_diagnostic_modules` from Architect's approved owning inventory. For issue 392, that inventory includes `tests/test_agent_work_interactive_commit.py` and `tests/test_agent_work_cli.py`. A CLI selector or environment value cannot approve a module. Later work needs its own approved inventory. |
| Process | Uses a fixed installed Python in isolated mode with bytecode disabled (`-I -B`) and installed pytest, then loads the exact candidate source. The fixed `-p no:cacheprovider` option suppresses pytest cache artifacts; other plugins remain enabled. No UV sync, shell, or custom pytest recipe runs. Pytest capture is disabled; process stdin, stdout and stderr use `DEVNULL`. Owned process cleanup runs after each attempt. This supplies no descendant confinement or sandbox-backend proof; an unsupported selected execution policy refuses before execution. |
| Safe failure facts | Reports only selected module, declared static test/function name and `setup`, `call`, `teardown`, or `collection` phase. Parametrization labels, exception messages, captured output, and private traces are absent. Unsupported dynamic names make details unavailable. A collection failure has no test name. `details_unavailable` and `truncated` qualify bounded failed-case details. |
| Report and exit | Reports `applied`, `candidate_tree`, `red_sha`, `modules`, `outcome`, `returncode`, safe failed cases, `details_unavailable`, `truncated`, and a fixed `stop_reason`. Outcomes are `preview`, `passed`, `failed`, `unavailable`, `identity_changed`, `unsupported_policy`, `invalid_selection`, or `invalid_result`. Human output gives the safe outcome summary, with failures on stderr; `--json` exposes the full safe report on stdout. Only preview or passed exits `0`; other outcomes exit `1`. Invalid command or request input exits `2` with a fixed error. `--debug` keeps diagnostics safe and adds no raw traceback. |
| Result handling | Uses one owned temporary result file with checked identity and bounded reads, then cleans it up. Missing, malformed, oversized, or changed result data cannot count as success. No persistent result store, JUnit validation receipt, or workflow proof is created. |
| Candidate guards | Checks the exact candidate before and after each attempted process, including failure or timeout. Drift invalidates the result. A passing diagnostic still requires the full native hook, Non-functional Reviewer, required reviews, Finalizer, and human MR approval and merge. |
| Correction | A diagnostic failure follows the approved production-only repair before the first Green commit. Tests stay frozen; production changes require restaging and invalidate earlier candidate evidence. A needed test change returns to fresh Red. These diagnostics change no serial or MCP route, review concurrency, profile, or loop activation. |

### Coding reconciliation commit

This is the explicit legacy ordered-merge API. It remains available to callers
that prepare its pending merge and required proof. Default interactive MR repair
uses Reconciler and rebase instead. The serial-controller descriptions in this
reference describe implemented behavior; they do not provide a serial rebase
capability.

After review, the branch still points to the saved source commit. The merge
remains pending. `reconcile-commit commit` checks the branch, source and target
commits, Red ref, staged Judge tree, and worktree. It rejects changed values,
unresolved merge entries, and unstaged or untracked files. Omitting `--apply`
or using `--dry-run` previews the commit and leaves the pending merge untouched.

With `--apply`, the command runs the license check and shared cheap gate for
the Judge tree. It then makes one merge commit through the installed hook. Its
parents are the saved source first and exact target second.
`reconcile-commit verify` checks the new commit, parent order, tree, Red ref,
cleared merge state, and clean index and worktree. It makes no changes, even
when passed `--apply`.
Compare the commit message with the reviewed message file separately. Ordinary
`interactive-commit green` remains the single-parent Green commit for work
without a pending merge.

### Optional test execution policy

`TestExecutionPolicy` is an optional, host-owned Python boundary for Red test
execution. A trusted host supplies it through runtime dependency injection.
The default trusted workflow stays unchanged. Environment values, model text,
and public JSON cannot select this policy or grant execution authority.

This boundary supplies grouped execution ports and runtime consumers. It does
not include a production OS confinement backend, a working confined Red route,
or a native-hook adapter. The host route stays closed until a real backend
and host proof exist. Positive tests with fake ports prove caller and proof
logic; they do not prove host isolation. These parts remain work for issue 387.

A selected policy binds the exact project, issue, ledger revision, branch,
base, candidate tree, test paths, and session nonce. It must also bind five
private known-file categories: `auth`, `source_auth`, `controller`,
`git_admin`, and `sibling`. Proof must cover each category independently and
match the expected path manifest. Empty, incomplete, duplicate, or changed
coverage cannot authorize a payload. Proof reports include no private paths or file
bytes. The public probe producer and parser support future backend proof;
they do not grant host authority.

Red execution checks assets and proof, closes input descriptors, and executes
through the supplied ports. It then stops descendants, checks assets, and
cleans up.
Only a complete lifecycle can return a command result. Refusal raises a safe
error and cannot count as an expected Red failure or retain a checkpoint.
Missing providers or unknown host support refuse with no ordinary-process
fallback. An unproven sandbox audit uses `sandbox_or_probe_failed` with
`cause_proven` false.

Every native-hook operation is refused when this policy is selected, including
Judge validation and serial Green commits. The backend, host proof, and hook
adapter remain required before that route can run. This boundary enables no
role profile or factory loop.

### Coding workflow Red boundary

A coding workflow creates its Red checkpoint while its producer is still live.
The producer writes bounded input in private scratch and calls the trusted
builder through its private broker. That one boundary establishes coding-lane
authority, reruns and validates Red, and persists and ref-binds the checkpoint
before it accepts a result.

| Condition or step | Controller contract |
| --- | --- |
| Fresh coding work | The producer may establish coding authority only through this atomic boundary. There is no pre-checkpoint lane write, Red handoff result, or later controller checkpoint command. |
| Atomic checkpoint transaction | The broker uses controller-observed candidate identity to reject drift, rerun the declared Red command, require its expected failure marker, and run the same read-only Ruff and Ty preflight on the exact changed test tree before binding evidence in one issue-namespaced checkpoint commit ref. An unavailable preflight, test-local errors, and tool or malformed-output failures block retention. It rechecks the candidate after preflight. Ty warnings may pass; trusted production ownership can allow missing module members, package exports, or new modules. It returns controller-owned checkpoint evidence to the still-live producer. |
| Test command scope | Checkpoint commands start with exactly `uv run pytest`. Every remaining argument is a declared root `tests/` Python path or its node ID. Every declared path must have a target. Options, undeclared paths, traversal, controls, alternate separators, and malformed node IDs are rejected before execution. |
| Test asset verification | Runtime supplies a trusted verifier. Missing verification blocks execution and retention. The controller verifies assets before and after execution, before snapshot and preflight, after preflight, and on retained replay. Each path component is checked without following symlinks. Surviving leaves must be regular files; a deleted leaf is allowed only below existing verified directories. |
| Snapshot scope | Red snapshots accept only the declared test assets. Surviving assets must have Git regular-file mode `100644` or `100755`; symlink and gitlink modes are rejected. Topology and staged-mode checks around snapshot creation reject changed proof before accepting a tree. Generic candidate reads keep their production and content scope. |
| Canonical correction | The broker returns at most one canonical rejection. It accepts one corrected request only when the controller freshly observes unchanged worktree evidence. A further request, changed paths, stale identity, missing marker, passing command, malformed input, or time-limit failure fails closed. |
| Retained authority | A retained exact checkpoint binds the coding lane to its project, issue, branch, base, ref, tree, paths, test command, expected marker, nonzero exit, output digest, and `residual_details` for accepted missing APIs. The details have at most 128 entries within the 32 KiB evidence limit. This local recovery evidence is never pushed. |
| Resume | The controller freshly reloads and verifies the checkpoint before a resumed coding workflow. Missing, stale, malformed, or mismatched identity stops the workflow. It does not recreate a checkpoint from model output. |
| Failure and retention | The controller preserves the worktree and records only bounded sanitized diagnostics. It retains no raw worker content, rejected values, exception text, command output, unsafe paths, or obsolete Red-stage transport state. |
| Content lane | Content work does not establish a Red checkpoint. |

These checks detect and reject invalid Red capture. They do not confine the
writable Red process before launch or prove OS denial of private-file access.
They enable no host route, timer, or factory activation. The selected execution
policy and native-hook refusals above still apply.

#### Optional Architect risk intent

Router may include Architect's advisory `architect_risk_intent` in
`red_checkpoint_request`. It records concrete state or protocol risks.
Green can use them for later planning. The same typed value appears in the
controller-owned `RedCheckpointEvidence` after acceptance. It is nested in the Red request,
including through the opt-in `worker_submit_result` MCP tool.

| Field | Contract |
| --- | --- |
| `architect_risk_intent.risks` | A nonempty list of risk cases. |
| `kind` | Exactly `state` or `protocol`. |
| `scenario` | A nonblank description of a concrete failure situation, without NUL. |
| `invariant` | A nonblank statement of the property that must remain true, without NUL. |
| Shape | Unknown fields are rejected in both the intent and each case. |
| Storage limit | Intent must fit within the existing 32,768-byte UTF-8 budget. The complete serialized checkpoint evidence, plus its one-byte framing, must also fit that budget. |
| Absent intent | Older evidence loads as `None`. Durable wire output omits absent optional fields. |

The controller retains the intent with the checkpoint in one atomic capture.
It checks the current project, issue, branch, base, and observed candidate
tree before acceptance. It then requires an exact read-back. Request and
evidence must contain equal intent. A matching retry reuses the retained
checkpoint; adding, changing, or omitting retained intent rejects the retry.

On resume, the controller checks the current checkpoint lookup identity.
Source changes may require the existing historical recovery route. In that
case, it checks the audit checkpoint identity. It reloads the full evidence
at its original base. The reload must equal the audit checkpoint. Stale audit
or reloaded evidence cannot supply risk context.

Green receives only the separately supplied, verified retained intent as
structured advisory data. Raw record fields, audit prose, issue text, and
summaries cannot supply it. The declaration grants no validation, commit,
publication, or specialist authority.

### `merge-request publish`

`merge-request publish` creates Markdown text for review. It has the needed
Problem, Outcome, and Validation sections. It adds Impact, Human action, and
Tracking only when matching flags are supplied. Supply one or more
`--validation` values. The command rejects literal escaped line-break payloads
before it can change GitLab.

`--issue-iid` accepts one positive issue IID. When present, preview and rendered
output end with exactly one canonical `Closes #IID` line. Omit it for issue-less
publication. `--tracking` can accompany `--issue-iid`, but only for non-closing
relationships. The command rejects a closing directive in user-supplied
structured text. Use `--issue-iid` instead. Inputs and merge-request text are
untrusted data.

Without `--apply`, the command previews the intended description. It says if it
would create or update. It makes no change. With `--apply`, it uses the
configured numeric GitLab project to find zero or one open merge request for
`--source-branch`. It creates one when none exists. It updates one when it
exists. It rejects an ambiguous result. It then reads that exact merge request
back. It verifies its IID, source and target branches, title, and description.
When `--issue-iid` is present, GitLab's trusted closing-issue projection must
contain exactly that issue in the configured project. After create or update
and the exact authoritative merge-request read-back, only an empty or missing
projection is treated as GitLab projection lag. The publisher reads it up to
eight times. It waits one second before each of seven retries, for at most
seven seconds. Each retry checks the same merge request and source commit.
It never repeats the create or update. The exact issue in the configured
project succeeds. A conflicting, malformed, extra, or multiple projection
fails at once. So does a changed merge request, wrong project, or wrong IID.
If the eighth read is still empty, the error names the existing merge request
IID and source commit.

The command never pushes a branch, approves a merge request, or merges it.
Push the branch first. See [how to validate template changes](../how-to/validate-template-changes.md#publish-the-verified-branch-for-review)
for a practical publication sequence. `--json` returns the matching
machine-readable result.

### `issue publish`

`issue publish` creates or updates one GitLab issue. It uses the configured
checkout project.
Its title and description are structured input. The description is rendered as
real Markdown before publication. Literal escaped line breaks and
percent-encoded line breaks are rejected; supply real line breaks instead.

Each publication task needs its own `--marker`. An omitted or empty marker
fails. The old shared marker `<!-- agent-work-issue-publication:v1 -->` also
fails. The command checks this before it contacts GitLab. Use the same marker
when updating that task's issue.

Without `--apply`, the command previews the title and rendered description
locally. It makes no GitLab calls or changes. With `--apply`, it selects one
exact open-marker match. More than one exact match fails closed. It creates
only when no exact open match exists. It then reads back that issue.
The read-back must match the configured project,
the returned IID, title, and rendered description. A mismatch fails closed. A
malformed response or failed read-back does too. The command does not retry
its create request after an uncertain result.

An explicit `--issue-iid` is update-only. It must select exactly one open
issue with the supplied marker. Missing, mismatched, or ambiguous evidence
fails closed before any mutation. It never creates a replacement issue.

Use `--feedback-audit` to create a new issue from automatic feedback. The CLI
finds the sole `*-agent-work.service` unit in the checkout's `systemd/` folder.
It checks that the installed user unit has the same name and bytes. It must be
under `.config/systemd/user/` in the effective user's OS account home. The
source unit must have one active, exact
`EnvironmentFile=%h/.config/<unit-stem>/agent-work.env` line. The CLI reads
that file from the same home. Its parent directory must belong to that user
and have mode `0700`. The file must be regular, bounded in size, owned by that
user, and have mode `0600`. It must use the exact version 1 non-secret format.

The audit checks these facts before a new issue POST:

- The file's host and numeric project ID match the configured checkout project.
- `AGENT_WORK_FACTORY_USERNAME` matches the GitLab `/user` name.
- Project issues are enabled, and the user has Developer (`30`) access
  from the project or its group.
- The project's default branch is protected, with no Developer push or merge
  access. The user cannot push to it.
- A global or applies-all approval rule needs at least one approval.

Missing or conflicting facts stop the POST. The audit does not call the
Maintainer-only project `/approvals` endpoint.

The audit runs after the exact-marker search. It runs only for a new feedback
issue. An exact-marker match reuses the open issue. An explicit `--issue-iid`
updates one open issue. Neither path runs the audit. Preview stays local and
reads no unit, config, or GitLab API. The audit uses only API reads: `/user`,
the numeric project, its default branch, and `/approval_rules`. It does not
change project settings or grant approval or merge authority.

`issue publish` is the configured-checkout boundary for policy callers. These
include findings triage. Upstream-finding and split-child
flows keep distinct deduplication and lifecycle transports. They reuse the
shared escaped- and encoded-linebreak guard. They also reuse the exact
authoritative read-back safety contract. Both checks apply to both flows. Both
are required. Both are strict. The command applies only explicitly supplied,
already verified labels. Callers, not the CLI boundary, must verify labels
before supplying `--label`. It never assigns, approves, or merges an issue.
`--json` returns the bounded publication result.

### Candidate stage, commit, read, validate, and status

#### Opt-in worker read MCP primitive

The repository provides an opt-in read broker and a separate MCP server for
one worker issue. The controller can create a live session through
`create_live_worker_read_session` only for an executing workflow record. The
session gives its trusted caller a socket path and nonce. The stdio entry
point in `agent_work.mcp_server` connects to that private broker when the
trusted host explicitly selects `worker-read` mode.

| Private environment variable | Contract |
| --- | --- |
| `AGENT_WORK_MCP_MODE` | Exact `worker-read` selects worker reads. Absent or exact `candidate` selects the existing candidate server and its role grants. |
| `AGENT_WORK_WORKER_READ_SOCKET` | Required in worker-read mode: a nonempty socket path with no NUL character. |
| `AGENT_WORK_WORKER_READ_NONCE` | Required in worker-read mode: exactly 32 lowercase hexadecimal characters from the controller's live session. |

Launch-field validation preserves the supplied socket and nonce. The entry
point rejects unknown modes and missing or malformed worker-read fields
before stdio starts, with no private details in the setup error. It never
infers the mode from credentials or falls back to another mode. Candidate
mode uses its existing private fields; worker-read mode uses only its own
connection fields.

This adds stdio transport access to the existing broker. The normal worker
launch, role profiles, tool allowlists, and timer remain unchanged. Full
worker activation still requires its host wiring and live isolation proof.

The controller creates the private socket and nonce. Each read checks the
project, issue, checkout, ledger revision, workflow state and phase, lane,
branch, source commit, and repository HEAD against the bound record. A stale
or mismatched identity stops the read.
The model cannot choose a path, field, Git command, GitLab endpoint, or another
issue through these tools.

| Worker MCP tool | Model arguments | Read result |
| --- | --- | --- |
| `worker_context` | None | Bounded workflow context, including state, phase, and lane. |
| `worker_issue` | None | Bounded issue title and description. |
| `worker_ledger` | None | Bounded ledger revision, source branch and commit, state, and phase. |

The worker read server lists only these three tools. Each has an empty, closed
argument schema. It does not add tools to
the candidate-preview MCP server. Private connection details, checkout paths,
credentials, and controller state paths are not returned. Closing or expiring
the session makes further reads fail. Provider errors return a bounded failure
without private detail. This interface supplies context only;
it grants no file, Git, GitLab, publication, approval, or merge operation.

#### Exact documentation patch MCP primitive

A trusted host can compose `create_live_worker_documentation_patch_session`
with an exact reviewed grant. The record must be executing in the content lane.
That public session owns the MCP server and private broker.
It checks live identity and supplies the default descriptor filesystem.
The tool applies approved replacements. It grants no general directory authoring.

| Area | Contract |
| --- | --- |
| Tool input | `worker_documentation_patch` accepts `patches` and optional `apply`, default `false`. Each patch has `path`, `base_sha256`, and `replacement_text`. The request cannot choose a role, grant, identity, root, mode, command, create, delete, or rename operation. |
| Host grant | Trusted composition binds project, issue, checkout, branch, source, phase, ledger revision, complete disjoint path groups, measured old/new byte sizes, expected base hashes, and approved replacement hashes. Model text, issue content, environment values, or serialized approval cannot create the grant. |
| Eligible files | Only existing `README.md` and eligible non-executable Markdown under `docs/` or `template/docs/`. Policy files, Jinja templates, executable files, and unrelated paths are refused. Code checks preserve old and new fenced blocks and every indented line exactly; malformed or uncertain regions refuse. An approved hash cannot authorize an executable-region change. |
| Groups and identity | Every required group member, base/replacement hash, measured size, and declared mirror equality must match before effects. The private broker binds a fresh nonce and deadline to live ledger and repository identity, then rechecks the grant and identity around operations. |
| Preview and apply | Preview changes no candidate content, inode, mode, ledger, or Git state. An accepted apply changes only the exact approved files and preserves their modes. Successful apply consumes the session. |
| Safe results | `outcome` is `preview`, `applied`, `refused`, or `failed_partial`. `changed` contains allowed member paths and hashes; `reason` is a fixed refusal code, and `quarantined` marks an unusable session. Results include no private contents, outside paths, nonce, exception text, or traces. They grant no staging, validation, commit, or publication authority. |
| Partial failure | Group replacement is not atomic. A later failure reports the exact already changed approved members and quarantines the session. There is no rollback and no claim of successful mirror parity. Cleanup removes only owned temporary files; failed cleanup or post-write drift cannot count as success. A new trusted grant is required. |
| Filesystem limits | Descriptor-relative no-follow checks reject observable parent, inode, hash, mode, or link drift. Ordinary replacement cannot atomically compare and swap the destination inode; the final check-to-rename window remains. These checks prove no OS confinement. |
| Activation limits | Existing worker invocation and read-only profiles receive no write tool. This primitive enables no writer profile, transport rollout, timer, or loop. Writer transport and broader file ownership remain separate work; this does not complete issue 354. |

#### Opt-in worker Git MCP primitive

The controller can create a separate live worker Git session through
`create_live_worker_git_session`. It binds the session to one executing worker
record and its checked checkout. The repository supplies the broker and MCP
server as opt-in parts. It does not install a CLI or host launch for them.
The normal worker launch remains unchanged.

| Worker Git MCP tool | Model arguments | Result |
| --- | --- | --- |
| `worker_git_status` | None | `dirty` and up to 128 changed paths from the bound checkout. |
| `worker_git_stage_candidate` | None | Controller-owned candidate staging and its exact tree. |
| `worker_git_commit_candidate` | Exact `candidate_tree` returned by staging | Bounded controller commit proof for that tree in coding work after Red. |
| `worker_git_candidate_status` | Exact `candidate_tree` returned by staging | Bounded controller status summary when its workflow route permits status. |

Status permits at most 512 UTF-8 bytes per path and 16,384 UTF-8 path bytes
in total. The transport frame limit is 131,072 bytes. An oversized result
fails instead of returning a partial path list.
Stage may return `candidate_tree`, `state`, and `changed_path_count`. Candidate
status may return those fields plus `timing_status`. Commit may return
`candidate_tree`, `committed_tree`, `commit_sha`, `red_sha`, `state`, and
`timing_status`. These replies fit the frame. Full JUnit and timing evidence
stays in the existing controller candidate route.

This server lists only these four tools. The worker-read server keeps its
three context tools, and the candidate-preview server keeps its own review
tools. The model cannot supply Git arguments, paths, a checkout, or an issue.
Before each operation, the broker rechecks the bound project, issue, ledger
revision, state and phase, lane, work kind, correction identity, branch,
source commit, checkout, and current HEAD. It does not pin worktree contents
when the session starts. Tracked edits can precede staging. A later stage
after a correction replaces the bound tree. The old tree no longer works.
Commit and candidate status require that session's
current 40-character lowercase hex tree. The controller decides which of those
operations its workflow route permits. After stage or status, the broker
rechecks branch and HEAD before reply. A commit reply must report
`terminal-success`, the same candidate and committed tree, and a lowercase
40-character commit SHA. The broker checks the new HEAD and binds later calls
to that commit. If a commit reply is lost, retrying the same tree in the live
session returns the same compact proof while the controller identity is valid.

For `authored_candidate_recovery`, `correction_identity.head_sha` stays frozen
to `record.source_sha`, the original authored source. A new session may start
at that exact HEAD after the controller checks the correction and Red checkpoint
identity, branch, and HEAD. This start needs no Green commit proof. A later
coding session may start at a retained Green HEAD on the exact issue branch
only when the controller proves the Red and Green commits, refs, and tree
against `correction_identity.baseline_sha`. An unrelated HEAD, including a
reset to that baseline, is denied.
Other routes use `record.source_sha` as the candidate base for the same retained
Green proof. The ledger source SHA stays bound in every route. In authored
recovery, it may differ from both the candidate base and Green HEAD.
The worker supplies no ref or commit SHA for these checks. A missing or
mismatched proof stops session creation.
An expired session or changed identity stops an operation. These tools grant
no arbitrary Git, GitLab, publication, approval, or merge access.

#### Opt-in worker validation MCP primitive

The controller can create a separate live Judge validation session through
`create_live_worker_validation_session`. It binds one staged candidate tree to
the executing worker record and checked checkout. The repository supplies the
broker and MCP server as opt-in parts. There is no installed host launch for
them; the normal worker launch remains unchanged. This session does not stage
files.

| Worker validation MCP tool | Model arguments | Result |
| --- | --- | --- |
| `worker_validation_validate` | None | Runs or returns the controller's validation result for the bound staged tree. |
| `worker_validation_status` | None | Reads that tree's saved validation status without starting hooks. |
| `worker_validation_evidence` | One fixed `selector` | Reads a bounded evidence summary or one JUnit or timing detail page. |

These tools use the existing controller route for the staged candidate.
Ordinary coding work after retained Red uses candidate commit for validation;
the separate validate and status tools apply only where the controller permits
staged-tree validation, such as a pending coding merge.

The `view: summary` selector returns `candidate_tree`, `state`, `verdict`,
`timing_status`, and the exact JUnit artifact identities:
`environment`, `sha256`, and `test_count`. Detail selectors use `view: junit`
or `view: timing`, `environment` set to `py311` or `py314`, `offset`
from 0 to 100000, and `limit` from 1 to 100. Detail replies contain `items`,
`total`, and `next_offset` for paging. Timing pages also include
`timing_complete` and page through the complete `over_100ms_cases` list;
each item contains `name` and `seconds`. An absent explicit verdict is
`unknown`; a successful state alone does not supply approval. An absent or
`null` internal timing status also appears as `unknown`. The broker projects
the saved controller status into these compact fields, even when the internal
record is large. It rejects a `raw_receipt` field. Replies omit artifact paths,
raw XML, controller state
paths, the private connection nonce, and other unlisted gateway fields.

This server lists only these three tools. The model cannot choose a command,
hook, checkout, issue, tree, path, or arbitrary evidence field. Before any hook
starts, the broker rejects a non-Judge role, stale ledger revision or workflow
state and phase, changed branch or HEAD, or an invalid selector. The controller
checks the supplied tree against its current staged candidate before running
validation. An oversized projected result fails instead of returning a partial
result. This primitive does not grant publication, approval, or merge access.

#### Opt-in worker-result MCP primitive

`create_worker_handoff_mcp_server` exposes one tool, `worker_submit_result`,
for a live worker to submit bounded intent. The host supplies the trusted
`issue_iid` when it creates the server. The tool takes one `intent` object;
the model cannot supply an issue number, nonce, project ID, checkout, source
commit, candidate tree, or protocol version. Unknown fields are rejected.

| Tool | Model arguments | Result |
| --- | --- | --- |
| `worker_submit_result` | `intent` with `workflow_lane`, `result_kind`, `change_summary`, `next_action`, and the fields needed for that result kind | A bounded acceptance response from the existing live worker-result broker. |

The tool accepts the six existing result kinds: `ready_to_publish`,
`red_checkpoint_ready`, `retryable`, `needs_human`, `split_required`, and
`suggest_close`. For `suggest_close`, the public `suggest_close_evidence`
contains only `reason` and `evidence`; the server adds the trusted issue
number before calling the broker. The server returns `accepted: true` only
after the broker accepts the result. It omits private identity and nonce from
the model-visible response. A canonical, safe broker diagnostic permits one
corrected submission while the worker is live. Noncanonical errors return a
generic failure. A request after acceptance fails closed.

This is an opt-in host primitive. A host must explicitly wire the MCP server
into the worker launch before the worker handles submissions through it. The
factory's default handoff source is `AgentInvoker` in `src/agent_work/worker.py`.
The MCP tool does not publish, approve, merge, or close anything; the
controller's existing result checks and transitions retain those decisions.

#### Candidate-preview MCP host primitive

Hosts can construct `RoleExecutionProfile.candidate_preview` for role
`candidate-preview` and pass it to `AgentInvoker.invoke` as `role_profile`.
The API has no installed command or factory caller. The ordinary factory
loop does not launch it or consume its result. The primitive supports
a separate read-only Codex profile with shell and unified execution disabled.
Its stdio server is `agent_work.mcp_server`.

| Tool | Model arguments | Result |
| --- | --- | --- |
| `candidate_status` | None | Saved status for the bound tree. |
| `candidate_changed_paths` | None | Canonical changed paths with old and new mode, OID, and byte size. |
| `candidate_read` | Either `{path, side, offset, length}` or `{reads: [{path, side, offset, length}, ...]}` | The single form returns exact range identity and base64 bytes. The batch form returns inventory, ordered bindings, and shared content bytes. |
| `candidate_submit_review` | `verdict` (`approve` or `reject`), bounded `summary`, and up to 32 findings with `severity`, `path`, and `summary` | Recorded advisory review for the bound tree. |

The controller binds the socket, nonce, issue number, and tree outside model
tool arguments. The MCP response omits the private nonce, issue number, and
project ID. The broker exposes the exact staged candidate's changed content,
including old and new sides, within its read limits. Reads may occur before
the path inventory and in any order when using the single form. They are
independent and repeatable.
The broker does not provide the full repository for a broad quality review.
A submitted review returns the candidate tree, `review-recorded` state,
verdict, summary, and findings. An approval with a major or critical finding
is rejected.

The result is advisory. The existing trusted broad Quality Judge remains the
workflow's authority. The same-UID host cannot prove reviewer authority:
another process under that user may see process-environment values, including
credentials. The pilot stays separate from the opt-in read-only role profiles
below. Track the wider MCP migration in issue #341.

##### Grouped candidate reads

The batch form uses the existing `candidate_read` tool. Its `reads` list must
be nonempty. Each entry names a canonical relative changed `path`.
Its `side` is `old` or `new`. Its `offset` is a nonnegative integer.
Its `length` is an integer from 1 to 131072.
Boolean values are invalid offsets or lengths. Extra fields and mixed single
and batch arguments are rejected. The single form keeps its existing result
and failure contract.

The server checks all selectors before calling the gateway. It then requests
a fresh changed-path inventory and each ordinary range in request order.
Every entry makes its own authorized read, including duplicate entries.
Each response must agree with the bound candidate and inventory. This includes
tree and available commit identities, ledger revision, source branch, path,
side, mode, OID, full size, and range. Private identity proof must also agree
when present. A selected side must exist and have regular-file mode `100644` or
`100755`. Returned base64 must decode to exactly the requested byte length.

| Batch result field | Meaning |
| --- | --- |
| `inventory` | Validated `changed_paths` and `files`, with old and new mode, OID, size, and explicit absent sides. An empty present file keeps size zero; an absent side has null metadata. |
| `bindings` | One entry per requested read, in request order: `request_index`, `path`, `side`, `offset`, `length`, `oid`, `size`, and `content_index`. Indexes start at zero. |
| `contents` | Entries with `data_base64`. Equal decoded range bytes share one entry. First occurrence sets its index. |
| Public identity fields | The verified candidate and base identities retained by the existing gateway. Private nonce, issue number, and project ID remain omitted. |

Each binding keeps its own context. Bytes may be shared across
paths, sides, modes, OIDs, or offsets. Duplicate requests keep separate
bindings. Sharing bytes does not grant review approval or reuse a semantic
judgment. An inventory proves neither full read completion nor comprehension.
Empty files do not gain a fabricated range read.

The existing inventory limits remain 128 unique paths, 2 MiB per blob, and
16 MiB of combined old and new blob content. The path and aggregate limits
apply to inventory, not to the number of range entries or repeated bytes.
Every gateway call still uses the broker's existing frame and session
budgets. Executed reads use those budgets. A later member's failure does not
restore them.

The batch's whole `CallToolResult` JSON body must fit 900000 UTF-8 bytes.
This includes inventory, identities, bindings, and content in both text and
`structuredContent`. The count includes JSON text escaping, field names,
wrappers, result type, error flag, and the SDK's public `serverInfo` metadata.
It excludes the outer JSON-RPC envelope and request ID. The single form keeps
its existing dictionary-only size check.

Before gateway calls, the server checks a minimum result body.
It holds each selector and request index in both text and structured content. If that body
cannot fit, it rejects the request before reading inventory or ranges.
The server checks the complete final body and returns that same checked result.
Any member refusal, invalid proof, inconsistent bytes, or oversized final
result rejects the whole batch with `candidate_broker: read failed`.
Private error detail and partial results are discarded.
A caller can use smaller independent calls.
There is no page, cursor, cache, new tool, or automatic partial delivery.

These output checks do not bound the SDK's incoming stdio line allocation.
The SDK reads a complete line before JSON validation. Grouping provides no
new process-memory, privacy, or OS-isolation guarantee. Existing tool
allowlists, unsupported-host refusals, and live-host audit requirements still
apply. This interface does not activate the factory's MCP route.

#### Opt-in read-only role profiles

A host can request a separate read-only Codex launch with
`RoleExecutionProfile.read_only` and pass that profile to
`AgentInvoker.invoke`. It requires keyword arguments `role`,
`candidate_socket`, `nonce`, `issue_iid`, and `candidate_tree`. The host binds
the socket, nonce, issue, and tree outside model tool arguments. The role
receives only its listed candidate
MCP tools. The launch requests a read-only sandbox, disables shell, unified
execution, and code-mode host tools, and ignores user configuration. It does
not inherit the GitLab token.

The host must set `AGENT_WORK_CANDIDATE_ROLE` for the MCP server to one exact
allowlisted role. A missing or invalid value rejects server startup. It never
falls back to `candidate-preview`; that pilot also needs an explicit role value.

| Role | Allowed MCP tools |
| --- | --- |
| `router` | `candidate_status`, `candidate_changed_paths`, `candidate_read` |
| `judge` | `candidate_status`, `candidate_changed_paths`, `candidate_read`, `candidate_submit_review` |
| `finalizer` | `candidate_status`, `candidate_changed_paths`, `candidate_read` |
| `supervisor-inspect` | `candidate_status` |

Only Codex supports these profiles. Claude and Pi requests fail before launch.
Before a role starts, the host audits its effective features in an isolated
Codex home that carries authentication but no user configuration. It requires
`shell_tool`, `unified_exec`, and `code_mode_host` all to report boolean `false`.
An unavailable feature, a `true` result, or an unreadable result rejects the
role before launch. The role command also uses `--strict-config`.

The host then audits the actual sandbox with the same isolated home, checkout
directory, read-only mode, and approval policy `never` used for the role launch.
This second audit must return all five fixed fields with boolean `true`:

| Sandbox audit field | Required denial |
| --- | --- |
| `write_denied` | Cannot create or change files. |
| `network_denied` | Cannot reach the network. |
| `auth_unreadable` | Cannot open the isolated home's private authentication symlink through its full alias chain. |
| `source_auth_unreadable` | Cannot open the source authentication path. |
| `controller_state_unreadable` | Cannot list private controller state or open its known `project.lock` file. |

The audit opens and closes private file descriptors. It must not read secret
file bytes. Missing target files cannot prove access is denied. Missing,
incomplete, malformed, or failed proof from either audit stops launch. Extra
fields, duplicate JSON keys, and values other than boolean `true` also fail.
There is no retry with a wider profile or the normal user configuration. A host that denies writes but still allows private auth or
controller reads fails this gate. Passing either audit grants no shell access.

The audits do not prove the live tool inventory. Before using this route, an
operator must check the selected host's actual tool list and confirm that
shell, write, and unlisted MCP calls fail. Command settings and fake command
tests do not prove that those tools are absent. Keep the normal factory route
until both audits and the live tool probe pass and the controller is wired to
the replacement. These profiles do not change human approval or merge
authority. The candidate-preview pilot stays advisory.

#### Read-only actor host

Non-preview read-only profiles use the maintained streaming Codex app-server
adapter. `AgentInvoker` and `build_runtime_dependencies` accept trusted
`actor_transport_limits` and `actor_frontend_factory` inputs. The default
factory is `create_stdio_actor_frontend`; missing limits close this route
before launch. `ActorTransportLimits` requires explicit `max_frame_bytes`,
`max_total_bytes`, `max_output_bytes`, and `cleanup_timeout_seconds`. These
values have no defaults. Test fixture values do not approve live host limits.

| Boundary | Required evidence or behavior |
| --- | --- |
| Permission plan | A host-owned plan binds the role, issue, candidate tree, checkout, isolated home, and protected path identities. It denies private auth and controller paths, disables network and execution tools, and names only the role's candidate MCP tools. |
| Before the prompt | Both independent feature and five-field privacy audits pass. The same app-server session returns complete effective security configuration and provenance, then the exact active thread permission profile. Configuration is checked again after thread creation; protected path identities are rechecked before the turn. A matching profile name alone is insufficient. |
| Streaming | The adapter handles partial nonblocking reads and writes under one invocation deadline and the supplied frame, total, and output limits. It drains both output streams. It refuses unknown protocol data and approval requests without granting access. |
| Completion | Only the expected thread and turn with completed status and no error can succeed. Owned pipes and the child process must close and be reaped before private-home cleanup. Cleanup failure prevents success. |
| Failure | Fixed safe reasons replace raw stderr, private configuration, and protocol errors. Missing or failed evidence stops the invocation without an exec or shell fallback. |

This is a closed-by-default foundation for issue #355. It has a real
adapter and runtime caller. It does not prove OS enforcement or that child
processes stay confined. Live host support and the actual tool inventory still need proof.
It does not activate a role, transport, profile, or timer, or complete the
wider migration. Ordinary writable invocations and candidate-preview retain
their existing routes and authority.

#### Committed inspection profiles

`agent_work.serial` provides a private read-only session for a fresh checkout
of a reconciled commit. It uses the existing broker and read tools. The
owner's saved worktree stays intact. Remote and local branch names may differ;
each is checked against its own source.

| Interface | Inputs and result |
| --- | --- |
| `worker.ReconcilerCandidateIdentity` | Pins `role`, `host`, `project_id`, `merge_request_iid`, `source_branch`, `source_lease_sha`, `checkout_branch`, `target_branch`, `target_sha`, `final_sha`, `committed_tree`, and `identity_kind="committed"`. It is private expected data, not proof. |
| `serial.create_reconciler_candidate_check` | Takes the identity and keyword inputs `worktree`, `record`, `ledger`, `gitlab`, and `repository`. Returns `(full_check, local_check)`. Both reject an identity that differs from the captured one. |
| `serial.create_reconciler_candidate_gateway` | Takes the identity and keyword inputs `worktree`, `record`, `check_candidate`, and `repository`. Returns the existing candidate action for status and reads only. |
| `serial.create_reconciler_candidate_session` | Takes the identity and keyword inputs `worktree`, `record`, `ledger`, `gitlab`, `repository`, `timeout_seconds`, and `nonce_factory`. Optional inputs are `temporary_directory`, `clock`, and `broker_factory`. Its context yields `(broker, profile)` and closes the broker on exit. |

The full check reads the current owner record and revision, configured project,
open merge request, remote refs, and local committed checkout. The local check
reads the current owner record and local checkout without remote or API calls.
The repository checks the exact branch, HEAD, tree, clean state, and target
ancestry. Missing facts, failed reads, or drift refuse inspection.

| Binding | Checked source |
| --- | --- |
| `source_branch`, `source_lease_sha` | Remote source ref, merge request, and current owner record. |
| `checkout_branch`, `final_sha`, `committed_tree` | Fresh inspection checkout; distinct from the owner's saved worktree. |
| `inspection_checkout` | Captured `worktree` path and actual path passed to `AgentInvoker.invoke`. Exact `Path` equality is required; alternate spellings refuse. |

The session sets all four private profile fields: `committed_identity`,
`committed_check`, `inspection_profile_check`, and `inspection_checkout`.
If any is set, all four must match the genuine session and committed role.
Changing the invocation path, replacing both paths, omitting a field, or
changing the role to `candidate-preview` refuses before preview or actor setup.
The refusal returns empty stdout and `stderr="actor_identity_changed"`.
Checks also run before the actor thread, before its turn, and at completion.
Drift at completion discards buffered output.

| Read result or limit | Contract |
| --- | --- |
| Status | `state="inspection-ready"`, `candidate_tree`, `commit_sha`, and `committed_tree`. |
| Inventory and ranges | Read from `target_sha` to `final_sha`; existing read fields and grouped-content bindings stay unchanged. Private expectation fields stay out of MCP results. |
| Broker binding | Uses the current owner's genuine `ledger_revision` and remote source lease. The first read pins the base SHA for later reads. |
| Refused actions | Stage, commit, validate, and review; inspection output is not validation proof. |

For coding and content results, the serial worker uses private `candidate`
commands in addition to its applicable checkpoint and worker-result
boundaries. Each live request includes a nonce. A `stage` request has no model
path list. The controller finds changed files, stages them outside the worker
sandbox, and returns the tree ID and path count. `read` requires that tree ID.
For ordinary coding work, `commit` uses that staged tree and the authentic Red
checkpoint to make the sole Green commit before specialist review. The
installed hook runs during that commit. A successful replay returns the same
Green identity without another hook or commit. Reads require the exact Green
head, ref, and tree. Content and remediation work keep staged-tree `validate`
and `status`.
Every read response binds `project_id`, `issue_iid`, `ledger_revision`,
`source_branch`, `base_sha`, and `nonce` to the current request. In coding
work, `read` follows the successful Green commit for that tree. For a pending
target merge, it follows successful staged-tree validation. Content work reads
after staging. A correction requires a new stage, applicable validation or
commit, and fresh reads and reviews. Earlier receipts are stale.

For an exact, authorized authored-candidate recovery, the original baseline
supplies the old tree and publication path set. The controller requires that
baseline to be an ancestor of the current source commit. Other candidates
without a pending target merge use the source commit as their read base and
require it to be the current head.
A pending target merge stages and validates against its saved Green source,
but reads the joined tree against the exact target SHA. This shows the work
being proposed to the target branch. The read response's `base_sha` names
that review base. The broker requires a
40-character lowercase hexadecimal SHA and the same value across reads.

`candidate changed-paths` returns `candidate_tree`, `base_tree`, sorted
`changed_paths`, and `files`. Each file entry has its `path` and old and new
mode, OID, and byte size; an absent side of an addition or deletion has null
metadata. A mode is `100644` or `100755`.
In ordinary serial coding work, this inventory also has `commit_sha` and
`committed_tree` for the verified Green commit.

`candidate read --path PATH --side old|new --offset N --length N` returns one
range from an existing side. The offset is nonnegative and the requested
length is 1 to 131072 bytes. The response gives `candidate_tree`, `base_tree`,
`path`, `side`, `offset`, `length`, `oid`, `size`, and `data_base64`. The returned
length equals the requested length. In ordinary serial coding work, the response also
has `commit_sha` and `committed_tree`; the latter equals the requested
`candidate_tree`. The controller verifies the Green commit, tree, and
immutable Red ref before returning those fields. Pending reconciliation,
content, and remediation reads keep the existing fields. Reads can precede
inventory, overlap,
repeat, or arrive in any order. The controller checks the exact issue,
repository, base, tree, changed path, blob, and requested range on every call.
There is no cursor, manifest state, completion call, recovery token, or local
durable read receipt. A missing, rejected, or stale read cannot support
finalization.

In ordinary coding work, `commit` requires the tree ID and runs the installed hook
while the producer is active. A failed hook or tree drift stops the commit.
The producer can fix the files and stage a new tree. A successful response
has `candidate_tree`, `committed_tree`, `commit_sha`, `red_sha`, `red_ref`,
`state="terminal-success"`, and `timing_status`. `red_ref` names the immutable
Red commit ref under
`refs/agent-work/red-commits/`; the retained checkpoint has a separate ref
under `refs/agent-work/red-checkpoints/`. The response also carries bounded
`junit_timings` when available. A missing or `OSError`-unreadable fixed JUnit
file reports timing `UNKNOWN`. A present malformed or oversized JUnit file
rejects the candidate. A valid file with more advisory slow cases than the
bounded response can carry reports timing `UNKNOWN`, without a timing pass. The
[testing strategy](testing-strategy.md#local-case-timing) defines the timing
gate; the commit response alone does not establish a timing pass.

Replaying the same commit verifies its identity without rerunning the hook.
After the worker exits, the controller checks the Green SHA, tree, and changed
files again before publication. It does not make another commit.

#### Pending target reconciliation

If the target advances after the Green commit and before the first merge
request, the pending coding merge keeps the exact Green source and target
identities. `candidate commit` rejects this state. The worker stages the
combined tree, uses `candidate validate` or `candidate status`, and reads that
same tree for review. The validation response includes `source_sha` and
`target_sha`. The controller rechecks the retained Red and Green refs, pending
merge head, current target, and reviewed tree before publication. It records
one `reconcile_commit` intent with those identities, then makes an ordered
two-parent commit with Green first and target second. The installed hook runs
on that commit. The controller verifies its parents, tree, clean worktree,
and refs before pushing its SHA. A changed source, target, tree, or ref needs
fresh validation and review.

If the target moves again after that commit but before push, the controller
blocks the handoff. It keeps the original Green source and the saved merge
proof. It does not push, reset the worktree, or start another pending merge.
Recovery needs a new merge with the exact current target and a fresh review
of the joined tree.

#### Legacy staged-tree validation and paired timing

Staged-tree `validate` and `status` remain available for content and
remediation. The paired baseline/candidate timing rules below apply only when
that legacy path requests serial timing. They do not describe the ordinary
coding Green commit. Saved staged-tree results return an opaque evidence ID,
test counts, digests, and hook time when available. For success, JUnit also gives total
test time and the three slowest safe test names per Python version. Each
supported version also has `over_100ms_cases`, a complete trusted list of safe
case names and durations strictly greater than 0.100 s, and `timing_complete`.
An unsafe JUnit parameter appears as a stable `[param_<hash>]` suffix on its
safe base test name. The hash has 64 lowercase SHA-256 hex digits. Each suffix
identifies a distinct case. Match the emitted ID with its Python environment.
It hashes the raw JUnit class and case name. An unsafe class name, malformed
case name, or duplicate emitted ID makes `timing_complete` false. Do not
request the raw parameter text. If either field is missing or
`timing_complete` is false, the timing proof is incomplete and non-functional
review rejects it when JUnit is available. The three slowest names do not
replace the complete list. These summaries remain in the saved record for
later status reads. They contain no controller state or JUnit file path.

After the full hook exits `0`, a missing fixed `py311` or `py314` JUnit file
or a read `OSError` yields terminal success with `timing_status="unknown"` and
`timing_unknown` containing only `environment` (`py311` or `py314`) and
`reason` (`missing` or `os_error`). The result has no JUnit artifacts, counts,
test times, or timing transcript and claims no performance pass; validation
and review continue. Timing `UNKNOWN` needs no timing-only recovery. Usable
JUnit evidence keeps the existing complete record shape. Malformed or oversized
XML, invalid counts, and hook failure remain failures.

For paired staged-tree diagnosis, the controller compares full-suite baseline
and candidate JUnit inventories for `py311` and `py314`, bound to exact trees.
Supplied proof must have matching environments, commands, and seeds. If paired
proof is supplied, missing parts, malformed data, or mismatches cause
rejection. A verified missing baseline JUnit artifact or a failed baseline
hook can leave baseline-relative timing `UNKNOWN` when the candidate JUnit
pair is complete. In that state, validation and saved status keep
`timing_status="complete"` for the candidate JUnit and return
`timing_comparison_status="unknown"` with
`timing_comparison_reason="baseline_unavailable"`. They have no
`timing_review` transcript. These fields do not claim a comparison pass.
An unrelated `OSError` during timing capture is terminal:
`timing_review` reports `invalid_timing` with
`timing_failure_code="capture_failed"`; private error detail is omitted.
Complete paired proof retains the existing `timing_review` result.

The worker receives bounded proof identities, an outcome, up to 128 findings,
an `omitted_findings` count, and the full candidate list above 0.100 s. The
private transcript keeps both complete inventories. Findings for new cases
strictly above 1.000 s and directly changed tests strictly above 1.000 s take
priority in the 128 slots. If more than 128 such findings exist, validation
fails with `finding_budget_exhausted`. The controller may omit report-only
findings and record their count without blocking on that omission.

Focused checks diagnose only new cases above 1.000 s and regressions in
directly changed tests. They add setup, call, and teardown deltas when proof is
available. Each invocation has at most 32 case selectors; the paired review
allows at most 32 invocations and 900 seconds. A required focused run that
exceeds its budget reports `focused_budget_exhausted`. Report-only diagnosis
does not require focused repeats and cannot exhaust that budget. Unchanged
cases linked to changed fixtures, helpers, or production code stay report-only,
even when the paired run shows a slowdown.

The outcome is `clear` with no findings, `report_only` when findings need no
correction, or `correction_required` for a new or directly changed test
strictly above 1.000 s. Cases above 0.100 s remain advice. The full candidate
JUnit and test diff set the ordinary gate; paired deltas do not add one.

While the original terminal timing failure is current, `candidate validate`
and `candidate status` report its safe `timing_failure_code` and bind it to the
evidence ID. Once a retry starts, status reports the retry state. It includes
a code only if that retry ends in a timing failure.

The reviewer records cases above 0.100 s for an owner and separate
follow-up. For each candidate case at or above 1.000 s, record its class,
owner, and reduction or exception plan in the durable audit or linked issue.
A directly changed case above 1.000 s needs correction under the ordinary
gate. An unchanged case remains report-only, even if its paired delta is
corroborated. Focused phase deltas help find a cause; one slow observation does
not prove a regression. An absent paired comparison does not waive the valid
candidate JUnit gate.

The socket has a private directory and a fixed deadline. A read permits at
most 128 paths, 2 MiB per Git blob, and 16 MiB of combined old and new
content. A session permits at most 512 requests, 4 MiB of incoming frames,
and 32 MiB of outgoing frames. Each frame must fit 1 MiB. A read rejects
symlinks, submodules, binary or invalid UTF-8 content, a changed tree or path
set, replay after restaging, malformed content, and oversized evidence.
Missing, replaced, malformed, late, or extra requests fail closed. A failed
Judge result goes back to the same live producer for a fix within the worker
budget. The worker must not run direct
`validation` commands, stage through Git, or write controller state.

A rejected candidate request can add bounded context after its existing safe
reason: `candidate_broker: <safe reason> [reason=<code> action=<action>
selector=<mode> request=<ordinal>]`. Actions are `stage`, `read`, `validate`,
and `status`. The selector mode is `none`, `changed_paths`, or `range`;
`changed_paths` and `range` apply only to `read`. The ordinal is a canonical
decimal number from 1 through 513, or `513+` after that.

| Code | Rejection |
| --- | --- |
| `invalid_request` | Another candidate request is invalid, including a safe controller callback rejection. |
| `request_limit` | The session has exceeded 512 requests. |
| `identity_mismatch`, `timeout`, `request_byte_budget`, `candidate_drift`, `invalid_candidate_identity`, `invalid_read_response`, `read_response_oversize`, `response_byte_budget` | The corresponding existing safe failure reason. |
| `candidate_rejected` | Another allowlisted safe controller callback reason. |

For example, a request past the cap reports `reason=request_limit` with its
recognized action, selector mode, and request ordinal. The suffix contains no
path, file content, nonce, or tree ID. Unparseable requests, malformed read
arguments, and unsafe callback text retain a general diagnostic. A rejection
still invalidates the session; the suffix does not authorize another request
or a finalization result. During a live session, read the failed candidate
command's diagnostic. If the worker exits with the failure, use
`loop status --issue-iid IID` or
`recovery inventory --issue-iid IID` to inspect the retained safe diagnostic
before planning recovery.

The finalizer's existing workflow receipt carries the Consistency Judge
verdict, or a specific not-applicable reason when copied facts did not change.
There is no new workflow receipt slot. A missing, failed, or stale expected
read blocks finalization; it is not a not-applicable result. Content
finalization requires the read tree to match the current candidate. Coding
requires the current Green SHA and tree, successful hook proof, and reviews
bound to that same commit. A restart recovers identity from Git and GitLab;
it does not restore role receipts from a session summary.

Without `--apply`, each candidate command previews its action on standard
output and does not contact the socket. Applied `changed-paths` and `read`
without `--json` print labeled, terminal-safe inventory or range evidence.
`--json` returns the machine response with the field names above. Live
requests need both the socket and nonce. `changed-paths` and `read` succeed
with exit code `0`, return `1` on broker rejection, and return `2` for invalid
syntax.
`validate` and `status` exit nonzero unless the saved result is terminal
success. The worker uses `--apply --json` so it can parse bounded responses,
including a failed validation result.

#### Committed candidate reads after restart

The controller-owned `candidate committed-changed-paths` and
`candidate committed-read` commands inspect an exact published commit without
a live worker socket or nonce. They require the issue number, merge request
number, commit SHA, tree ID, and a review role of `router`, `judge`, or
`finalizer`. The role argument selects the review context; it is not an
authentication credential. A bare command previews the request without
reading the ledger, GitLab, or Git. `--apply` performs the read-only checks and
returns the result; `--json` emits its fields.

For each applied request, the controller checks the durable issue record, the
current project and open merge request, its source and target branches, source
SHA, and closing relation to the issue. It checks that the commit has the
requested tree and that the saved base is its ancestor. It reads changed
paths or the requested byte range from Git objects, then rechecks the issue
and merge request identity. A mismatch rejects the read. The inventory
returns `candidate_tree`, `base_tree`, `changed_paths`, and `files`. A range
returns the requested path, side, offset, length, blob OID, size, and
`data_base64`, with `candidate_tree` and `base_tree`. Both responses also
include `candidate_sha`, `issue_iid`, `mr_iid`, and `role`.

Committed reads do not stage files, run validation, or change the index or
worktree. A dirty checkout remains intact. They add no read receipt or other
store; the finalizer uses its existing workflow receipt. The live staged
`stage`, `commit` or `validate`, and `read` sequence still governs work before
publication. These commands do not approve or merge a merge request.

### `validation window`, `validation run`, and `validation status`

`validation window --apply -- COMMAND` holds one local timing admission for
the configured GitLab project while `COMMAND` runs. It requires a bare
`GITLAB_HOST` and positive numeric `GITLAB_PROJECT_ID`. Without `--apply`, it
only previews the action and does not start the child. An interactive Judge can
use `validation window --apply -- bash` to run the Judge hook and paired local
timing repeats in one child shell. A nested `validation run` or `validation
retry` can use that live window. The window ends when its child exits, including
after a crash. Other configured projects and CI are independent. The CLI exits
`0` when the child succeeds and `1` when it does not; `--json` includes the
child's raw status as `exit_code`.

An applied window, run, or retry waits up to 30 seconds for another local
observation on the same project. If admission stays busy, it saves
`timing-contention.json` under the project's XDG state and exits `1` without
starting a hook or child. Plain output says `project timing admission busy`.
With `--json`, `validation run` and `validation retry` return
`{"candidate_tree":"TREE","state":"deferred","reason":"project_timing_admission_busy"}`.
The window returns
`{"action":"window","applied":true,"state":"deferred","reason":"project_timing_admission_busy"}`.
These results mean the local timing proof must wait; they do not claim a clean
measurement.

While an applied interactive window waits, it publishes a short-lived request
for its exact configured host and numeric project. The controller recognizes
only a live request from that window. A contention record, inherited environment
value, expired request, or request for another project cannot ask it to yield.
Malformed or unreadable retained request JSON raises an explicit error. The
controller leaves that file in place for review.
The waiting window takes the controller's project lock first. It then takes
timing admission within one bounded wait. It holds both while its child runs.
If it cannot take both locks, it marks the request unavailable. It returns the
deferred result without starting validation. A later controller run cannot
yield to that request.

After it durably binds a coding Red checkpoint, the controller checks for a live
request. If it finds one, it finishes the current worker segment and returns
`stop_reason=interactive_validation_yield`. The issue stays in `EXECUTING`
with its worktree and Red evidence. The service exits successfully, leaving
the interactive window free to take the locks. Its wrapper does not start
another applied run in that pass. If the issue remains active, the next timer
run reloads the checkpoint and resumes the worker. With no live request, the
workflow continues.

The resumed report's `validation_admission` evidence has `resolution=resumed`
and identifies the request. It records `request_at`, `yield_at`, `validation_started_at`,
`validation_finished_at`, `resume_at`, `wait_elapsed_seconds`, and
`unavailable=false` when those boundaries were observed. The elapsed value
measures the wait from request to validation start. These are controller and
admission timestamps for this handoff. They are separate from the ledger's
`phase_timings` entries for workflow phases and roles.

When the bounded wait ends before both locks are acquired, the next applied
report may instead show `resolution=unavailable` and `unavailable=true`. It
records the request, end of wait, later report time, and actual wait. JSON omits `yield_at` and
`validation_started_at` because neither event occurred.
That record does not claim a validation run.

Only one request can remain outstanding per project. A new applied window
returns the deferred `project_timing_admission_busy` result until the controller
reports the earlier request. If the yielded issue remains active, the report
records `resume_at` after the worker returns. If it is no longer active, the
report records `resolved_at` without invoking the worker. For an unavailable
window, a later applied run reports `validation_admission.unavailable=true`.
A run that exits before one of these reports leaves the request outstanding.
After the report consumes the request, a new window can start.

If the exact yielded issue is no longer active in the ledger before worker
invocation, the next applied run does not invoke that worker. It returns
`stop_reason=validation_admission_resolved` with the `issue_iid`. Its
`validation_admission.resolution` is `closed_without_resume` for `COMPLETE` or
`ineligible_without_resume` for another non-active state, such as `BLOCKED`.
The report keeps the request, yield, and validation times, sets `resolved_at`,
and leaves `resume_at=null`. This consumes the request, so a new window can
start.

The trusted controller or an interactive Judge checks one staged candidate in a
durable session keyed by `TREE`.
Use `validation run --candidate-tree TREE` to preview the action. It does not
change state or start a hook without `--apply`.

With `--apply`, `validation run` first checks that `TREE` is the current staged
tree. It runs the read-only license checker against that exact Git tree and
requires a valid, compliant result. The shared cheap gate then checks the tree
in a read-only snapshot. It runs yamllint, ShellCheck when Bash files are
present, readability for all Markdown, ty, staged Kingfisher, Ruff check, and
Ruff format check. Ruff uses no cache. A failed check or tree drift stops before
JUnit cleanup or a full-hook session. A failed check returns
`preflight-failure` with no saved session. Running the same tree again checks
the same files. After a fix, stage a new tree. Its checks run again. A preflight
failure cannot use
`validation retry` because it has no saved session. Judge records a running
session only after both checks pass. The full installed hook still decides
whether the tree passes.

The shared cheap gate can report safe locations from its original failed Ruff
check. Green and reconciliation refusals, and shared validation reports, use
the same checked facts. No extra Ruff run is needed.

| Cheap-gate diagnostic | Contract |
| --- | --- |
| Supported failure | Ruff check exits `1` with empty stderr. Its concise output contains only `I001` records and recognized Ruff summaries. Each reported location has a canonical staged relative path to a tracked regular file, with no symlink in its path, and positive line and column numbers. |
| Safe facts | The report contains only the checked path, line, column, and `I001` code. It discards Ruff messages, source, carets, context, and snapshot paths. The facts must match the candidate tree and check ID. |
| Limits | At most four location lines, each at most 256 UTF-8 bytes. Original output and the serialized facts must each fit 4096 UTF-8 bytes. Line and column values must be canonical positive decimal numbers at most `999999999`. Overflow rejects the details rather than truncating them. |
| Generic fallback | Other checks, unsupported rules, malformed or mixed output, unsafe or unknown paths, invalid positions, changed trees, and invalid facts keep the fixed generic refusal. Useful details never turn a failed check into a pass. |
| Capture limit | `run_subprocess` uses `capture_output=True`, which reads the full child output before these checks. The limits bound admitted and emitted facts only. They do not bound process memory or guarantee private-file privacy or input isolation. |

These facts add no saved session or diagnostic store. A failed cheap gate still
stops before commit, JUnit cleanup, or the full hook. Green and reconciliation
CLI refusals use stderr with exit `1` and empty stdout, including with `--json`.
Preview starts neither the cheap gate nor the hook.

After the checks pass, validation invokes exactly one
`git hook run pre-commit` command. It then atomically
records that command's terminal exit code. A terminal-failure record can also
have the existing bounded, canonical
`diagnostic` and additive structured `hook_failures` from that one invocation.
The command returns only when the hook has a terminal result.

On terminal success with complete timing, the hook saves fixed `py311` and
`py314` JUnit snapshots.
It saves them under the candidate tree in the state root. Each result gives its
environment, absolute path, SHA-256 digest, and `test_count`. Status reads this
saved evidence. It never reads checkout XML or runs the hook again.

For serial work, the controller explicitly selects `--serial-timing` and
supplies the checked baseline commit and canonical changed paths with
`--baseline-sha` and `--changed-path` on run, retry, and status. The marker
requires a baseline SHA, and a baseline SHA without the marker is rejected
before Git or state access. Applied serial validation rejects an empty
changed-path list before the hook starts.
The candidate hook uses an explicit seed. After it succeeds, the controller
runs the full baseline suite from a private detached checkout of that commit
with the same seed and prepared environments. It
compares complete JUnit case inventories for `py311` and `py314`, then runs
bounded focused checks where a case needs timing diagnosis. The private
transcript binds the source trees, JUnit digests and counts, case findings,
and setup, call, and teardown changes. A serial status read verifies the
saved transcript against its digest without rerunning tests. For complete
timing, missing or changed proof fails closed. A status read or retry cannot
bypass the timing baseline identity saved for that candidate. Status also checks
the saved timing transcript before it reports complete timing. The worker sees
bounded timing findings and proof identities, with no private paths.

Capture clears source artifacts before and after the hook. It fails closed if
cleanup fails or artifacts are bad or cannot be saved. The session
then has `terminal-artifact-failure` with CLI status `1`. A serial timing-review
failure after a successful hook retains the immutable `py311` and `py314`
snapshot identities in the original record, including their paths, SHA-256
digests, and test counts, along with the requested baseline identity. The
record and status report a safe `timing_failure_code`: `baseline_hook_failed`
for a failed baseline hook, `focused_timing_command_failed` for a failed
focused timing command, or `capture_failed` for other errors. They omit
private error text.
The record's `artifact_failure` has an allowlisted `stage` and `reason`. When
one file causes the failure, `environment` is `py311` or `py314`. Its `path`
is `test-results-py311.xml` or `test-results-py314.xml`.
For a candidate-level `immutable_snapshot` or `timing_review` failure, both
JSON fields are `null`. These fields identify the failing operation without
exposing its exception or XML content.

`exit_code` holds the actual hook exit code: `null` if cleanup failed before
the hook, `0` if the hook succeeded, or the hook's nonzero exit code. It is
not the CLI status. Artifact-failure reports and status omit captured JUnit
files, including the snapshots retained in a failed serial timing record.
Preview, running, terminal-failure, and legacy sessions also show no files.
Existing sessions are not backfilled.

The `stage` distinguishes `pre_hook_cleanup`,
`post_hook_failed_evidence_read`, `post_hook_failed_cleanup`,
`post_hook_success_evidence_read`, `post_hook_success_cleanup`, and
`immutable_snapshot`, and `timing_review`. The `reason` is one of `os_error`,
`missing`, `invalid_xml`, `oversized`, `invalid_count`, `invalid_snapshot`,
`invalid_timing`, `finding_budget_exhausted`, or `focused_budget_exhausted`.
`invalid_snapshot` applies only when snapshot validation rejects the saved
candidate evidence. The three timing reasons apply only to `timing_review`.
The failure fields contain no raw operating-system errors, arbitrary paths,
or XML data.

`validation retry --candidate-tree TREE` previews a retry without changing
the session. Its help covers cleanup and saved timing retries. A recorded
`pre_hook_cleanup` failure with `reason: os_error` and no hook exit code remains
eligible while `TREE` is the current staged tree.
With `--apply`, it saves a separate attempt tied to the original record and
runs the hook once. An interrupted or repeated retry reads that attempt.

Serial work has one additional retry: when the hook exited `0` and only the
timing review failed, `validation retry --serial-timing` can use the two saved
full-suite JUnit snapshots to rerun timing review. It rechecks the unchanged
staged and source trees, requested baseline SHA and tree, original record,
and both snapshots, including their trusted state-root paths, digests, and
counts. Missing, changed, or conflicting evidence fails closed before timing
capture. The saved reader uses `O_NOFOLLOW` and `O_NONBLOCK`, accepts regular
files only, and reads at most 32 MiB plus one byte. An existing transcript must
match the exact baseline commit and tree, candidate tree, and JUnit pair. The
retry adds one attempt bound to the original record's SHA-256 digest. It saves
`running` before capture, then saves the terminal result in that retry record.
If interrupted, status reports `running`; another apply returns that record
without capture. The timing retry never reruns the license preflight, cheap
gate, candidate hook, or candidate full suite. A normal `validation run`
still returns the original result. Other post-hook failures require a fresh
candidate tree.

When fixed supported-version JUnit artifacts are usable, the diagnostic has
only their total numeric failure and error counts. Else, it uses a fixed generic
diagnostic. That names `git hook run pre-commit` and its nonzero exit code.

`hook_failures` stores parsed failed hook IDs and short canonical lines. A
readability line has a safe relative path and grade. A Ruff or type line has a
safe relative location and code. A tox line has fixed supported-version JUnit
aggregate counts. Other hooks, and hooks without usable JUnit data, use the
fixed fallback line. The array keeps the first 16 entries. An ID is at most 128
ASCII bytes. An entry has at most four lines. A line is at most 256 UTF-8 bytes.
The serialized evidence is at most 4096 UTF-8 bytes. A terminal record stores
one snapshot. It never keeps history.

The record and command output exclude raw standard output and standard error.
They also exclude arbitrary source, messages, carets, context, exception text,
raw JUnit XML, environment values, prompts, credentials, secrets, controls,
ANSI sequences, and unrecognized data. A failed JUnit artifact may contribute
up to 16 safe `test_` names and two SHA-256 digests. The names and digests remain
in the terminal record after the source XML is removed.

Inspect an unchanged tree and JUnit evidence only after terminal zero. A
terminal nonzero blocks that check. An existing record for the same candidate
tree is trusted. Another applied run returns it without rerunning the hook or
rewriting the record. This rule also covers legacy records without `diagnostic`
or structured hook failures, or without retained success artifacts. The command
does not backfill any of those fields.
Legacy records without structured hook failures expose an empty collection.

`validation status --candidate-tree TREE` is read-only. It never starts a hook.
For an eligible same-tree retry, it reads the saved retry attempt when present.
It reports `running`, `terminal-success`, `terminal-failure`, or the distinct
terminal artifact-capture failure. With `--wait-seconds`, it polls a running
session only for that bounded interval. It stops at a terminal session. Use it
after a yielded or lost command handle. Keep querying the same `TREE`; do not
start another check. It accepts `--apply` for command consistency but remains
read-only. Human output includes the optional diagnostic. A terminal failure
with evidence also includes failed IDs and canonical lines. JSON exposes
`diagnostic` as its canonical string or `null`, plus bounded `hook_failures`
objects with `hook_id` and `diagnostic_lines`. Artifact failures expose the
allowlisted `artifact_failure` object. Terminal records also include elapsed
hook time when the hook ran. Preview, running, terminal-success, and legacy
terminal-failure results have no hook entries. Status reads legacy records that
omit either field. An absent `diagnostic` is `null`: no diagnostic is available.

Exit code `0` means a preview or terminal success. An applied running session
exits `1`. A terminal failure, terminal artifact-capture failure, missing or
malformed session, or operational failure also exits `1`. Invalid usage exits
`2`. JSON reports the candidate tree, applied state, action, state, terminal
state, final exit code, and optional diagnostic. Repeated apply or status
requests for the same candidate tree never rerun the combined hook or an
individual hook. They never rewrite the original record. An eligible pre-hook
cleanup or serial timing retry writes a separate append-only attempt.

An interrupted interactive window can leave a saved `terminal-success` result
for the exact staged tree while its focused timing is incomplete. Status still
returns the saved hook exit code `0` and immutable full-suite `py311` and
`py314` JUnit paths, SHA-256 digests, and test counts. A new window for timing
only may use that result after positive proof that the earlier factory worker
and its descendants have ended and are quiet. A free admission lock alone is
insufficient. The new window must recheck status, staged-tree and worktree
identity, and saved artifacts. Focused runs from the interrupted window are
discarded. The read-only comparator checks complete equal baseline and
candidate repetitions against the exact trees, commands, interpreter paths,
seeds, and JUnit and phase-log paths, digests, and counts. The Judge checks and
audits Python and package version parity separately; the comparator does not
accept version fields. Contention during or after the runs invalidates the
timing proof. The durable audit records the interruption, old-worker check,
new window, identities, artifacts, and contention check. Non-functional
Review and Finalizer cannot proceed on partial or contaminated proof. No
second validation run, retry, or hook is allowed for the same tree.

### `credential rotate`

Use this command after a GitLab credential changes. It safely rebinds the
username.

| Topic | Contract |
| --- | --- |
| Purpose | `credential rotate` safely rebinds the environment file’s `AGENT_WORK_FACTORY_USERNAME` after an operator changes the configured directory-scoped GitLab credential. |
| Inputs | `--environment-file PATH` identifies the existing service environment file. `--unit-name NAME` identifies its user service and timer. Both are needed. |
| Token safety | The command never accepts a token as a flag or writes one to setup, standard output, diagnostics, or logs. |
| Preview | Without `--apply`, it is a dry run. It checks the named non-secret setup, reports a redacted prospective outcome, and makes no setup or systemd change. |
| Applied stop | With `--apply`, it disables and stops the timer and stops the service before requesting credential-dependent evidence. Both remain stopped in every outcome, including success. |
| Preconditions | Applied rotation derives the authenticated username through the environment file’s configured bare GitLab host and positive numeric project endpoint. It needs exactly Developer (`30`) effective access, a protected effective default branch that Developers can neither push nor merge, and at least one global approval rule that needs at least one approval. |
| Update failure | It atomically changes only the expected username entry. Invalid, expired, unauthorized, or username-mismatched credentials, and every failed precondition before a write, leave the environment file byte-identical. |
| Applied preview | After an applied update, the command runs one bounded, isolated, scrubbed, non-claiming wrapper preview. The preview verifies the updated runtime credential route and the existing fail-closed username check without starting normal work. |
| Rollback | If the preview fails, rotation makes one bounded exact-byte rollback attempt and returns a sanitized corrective handoff. If it cannot confirm that rollback, it reports setup uncertainty for human correction. It never retries updates indefinitely or starts, enables, or re-enables the timer. |
| Results | Successful text output is a concise redacted state outcome. `--json` returns the matching redacted structured result for automation. `--debug` adds only local sanitized diagnostics. Success exits `0`. A rejected precondition, credential failure, preview failure, or rollback-needed outcome exits `1`. Argument or command-usage errors exit `2`. Output and errors never include a token, credential material, or environment-file contents. |
| Wrapper | The preview invokes the installed wrapper as an argument vector, not a shell string: `/usr/bin/env bash <checkout>/scripts/agent-work-systemd-run.sh`. The wrapper is intentionally mode `0644`. This invocation does not require an executable-bit change. |
| Operator action | Rotation stops before canary execution, re-enablement, ordered canary processing, or retained-record recovery. The operator reviews the successful result, follows the setup guide’s canary procedure, and explicitly re-enables the timer only after that review. |

### `service-environment migrate`

Use it to check one file. It can upgrade the file. It does not change other
work.

| Environment-file keys | Migration treatment |
| --- | --- |
| Command and inputs | `service-environment migrate` checks or upgrades one installed user-service environment file. Supply `--environment-file PATH` and `--unit-name NAME`. |
| Contract version | The current version is `1`. |
| Contract-version marker, `AGENT_WORK_CHECKOUT`, `AGENT_WORK_GIT`, `AGENT_WORK_GLAB`, `AGENT_WORK_JQ`, `AGENT_WORK_FLOCK`, `AGENT_WORK_TIMEOUT`, `AGENT_WORK_UV`, `AGENT_WORK_AGENT_PATH`, `AGENT_WORK_DIRENV` | Factory-managed. Apply regenerates the version, checkout, and tool-path fields. |
| `AGENT_WORK_AGENT`, `AGENT_WORK_BASE_BRANCH`, `AGENT_WORK_FACTORY_USERNAME`, `GITLAB_HOST`, `GITLAB_PROJECT_ID` | Project-owned. Migration preserves them rather than regenerating them. |

| Mode or step | Required behavior |
| --- | --- |
| Bare invocation | A non-mutating, non-claiming check and preview. It does not select an issue, acquire loop work, change an issue, ledger, worktree, branch, or label, change GitLab, or start a worker. |
| `--apply`: stop | Disables and stops the named timer, stops the service, and verifies that both are inactive. |
| `--apply`: pre-write check | Checks the configured Developer identity, numeric project, protected default branch, and needed approval safeguards through the checkout's `direnv` and password-store credential route. |
| `--apply`: write and check | Regenerates only factory-managed version, checkout, and tool-path fields with a recoverable mode-600 atomic replacement. Then performs one isolated non-claiming check. |
| Every outcome | The timer and service remain stopped for human review, canary execution, and explicit re-enablement. The command does not start either unit. It never rebinds the configured identity or puts credentials in command arguments, the environment file, output, diagnostics, logs, or the ledger. |
| Failed pre-write check | Changes no bytes. |
| Later failure | Returns one sanitized actionable error and preserves a byte-recoverable prior setup. |
| Isolated check | Invokes the same intentionally mode-`0644` wrapper with the unit-shaped argv `/usr/bin/env bash <checkout>/scripts/agent-work-systemd-run.sh --validate-service-environment`. It never uses a shell string or an executable wrapper file. |

| Result or option | Contract |
| --- | --- |
| Successful preview or apply | Exits `0`. |
| Rejected precondition or migration failure | Exits `1`. |
| Invalid command usage | Exits `2`. |
| `--json` | Returns a redacted structured result with `command`, `applied`, `outcome`, `config_updated`, `rolled_back`, `timer_stopped`, `service_stopped`, and `next_action`. |
| `outcome` | One of `current`, `stale`, `verified`, `rolled-back`, or `correction-needed`. |
| Without `--json` | Reports the same migration state as concise non-secret fields. |
| `--debug` | Adds only sanitized local diagnostics. |

| Loop command or state | Contract |
| --- | --- |
| `loop run` without `--apply` | Reports the next safe action once. It does not change GitLab, the ledger, worktrees, branches, labels, or services, simulate a complete activation, or poll. |
| `loop status` | Read-only. It reports durable ledger records with verified configured-project identity and best-effort live corroboration. Without `--issue-iid`, it returns at most 100 records in fixed order and reports the total and whether the result was truncated. With `--issue-iid`, it looks up that item exactly. |
| `loop retry` | Reuses the evidence-bound recovery checks for one recorded blocked item and also requires exactly one `safely_retryable` inventory result. A successful preview returns `retry_preview` with an evidence-bound retry plan. `--apply` needs that exact plan file and rechecks current evidence under the project lock before it returns `retry_scheduled` or, for an eligible retained correction, `correction_activated`. A valid retry needs no new human unblocking approval. |
| Immediate owner set | `PREPARING`, `EXECUTING`, `PUBLISHING`, `OBSERVING`, and `REPAIR`. |
| `BLOCKED` | Not an immediate owner. An applied activation may resume one clean, untouched ordinary pre-checkpoint record after exact proof. After genuine idleness, the narrow failed-Red dirty case may get a separate clean recovery child; other blocked work needs reviewed recovery. |
| `RETRY_WAIT` | Queued, not a competing immediate owner when an exact IID is selected. |
| Reviewed exact retry | The durable handoff. The selected IID stays pinned through a multi-segment continuation. It moves one chosen `BLOCKED` IID to `PREPARING`, or keeps `PUBLISHING` for a publication replay, without making untouched blocked or queued-retry records compete. |
| Another immediate owner | Retry fails closed. More than one true immediate owner needs reconciliation. |

| Recovery or checkpoint action | Contract |
| --- | --- |
| `loop canary` | An operational checkpoint, not an issue run. Bare invocation creates a redacted, immutable plan. The plan verifies public service and project identity, durable recovery evidence, and current pipeline evidence. It names the expected checkpoint and next action. It never selects an eligible issue, claims work, changes a lifecycle label, creates a ledger record or worktree, or invokes a worker. |
| `loop canary --apply --plan-file PATH` | After review, rechecks the plan's exact evidence while holding the project lock before taking its bounded checkpoint action. Changed identity or evidence needs a new preview. |
| `loop recover --issue-iid IID` | Targets one interrupted, ambiguous, or otherwise unsafe retained item. For a `BLOCKED` target, preview and apply reuse the recovery checks, require exactly one `safely_retryable` inventory result, and return the retry outcomes below. For a nonblocked target, bare invocation creates the usual redacted recovery plan without changing anything. |
| Stale dirty `EXECUTING`/`WORKFLOW` claim | An exact-IID preview may return `handoff_stale_dirty_claim` for an `UNKNOWN` or `CODING` lane with a stale or missing owner and verifiable dirty work. It requires the fixed issue branch and worktree, an open running issue, and no publication or merge request. The recorded source `HEAD` may differ from current main. The plan binds the ledger revision, owner, branch, `HEAD`, index, dirty snapshot, and the digest of an existing Red checkpoint or its absence. Live or unknown ownership, untracked or unmerged files, renames, missing proof, or changed evidence cannot use this handoff. |
| Exact failed current-source MR pipeline with one uniquely corroborated retained owner | Does not need `loop recover`: an applied ordinary loop activation moves that owner directly to `repair`. |
| `loop recover` apply | Needs both `--apply` and the reviewed `--plan-file`. For a blocked target, it rechecks current recovery evidence and the `safely_retryable` inventory result; ineligible evidence changes nothing. For a nonblocked target, it rechecks the configured project, default branch, authenticated username, ledger revision and state, lifecycle label, fixed branch, retained-worktree HEAD and cleanliness or exact dirty snapshot, merge-request project/source/target/source-SHA identity, and the current exact-SHA pipeline. It cannot advance another IID. |
| Dirty-claim handoff result | Apply returns `dirty_claim_handed_off`, records `BLOCKED` with `state_reconciliation_failed` and a manual recovery receipt bound to the plan and dirty snapshot, and projects `agent::blocked`. It releases the active claim. It preserves the worktree, index, branch, unstaged changes, and any existing Red checkpoint; it does not invoke a worker or publish. A failed label projection can replay the same reviewed plan without adding a ledger revision. |
| `loop recover` limits | `--max-segments` and `--max-activation-seconds` retain `loop run`'s bounded activation controls. |
| Plans | Bind the command and action to public service identity, configured project and default branch, ledger revision, and the exact target, issue, repository, merge-request, and pipeline evidence the action needs. They never contain secrets. Apply fails closed with the safe next action when the plan is stale, tampered, for another identity, or no longer matches evidence. |

### Retry outcomes

`loop retry` and `loop recover` for a blocked target report these outcomes. They
use shared recovery checks plus the `safely_retryable` inventory gate. Each
retains its invoked command name. A manual `recovery resolve --choice retry`
preview can present evidence for human judgment that these commands reject.

If automatic apply is interrupted, rerun the same original `loop retry` or
blocked `loop recover` command with `--apply --plan-file PATH` and the same
exact reviewed plan. If its matching intent remains `BLOCKED`, apply rebuilds
inventory and checks the full plan under the project lock before any effect.
If that exact intent reached `PREPARING` or authored `EXECUTING`, apply resumes
it idempotently. Stale or mismatched evidence still fails. The outcomes below
still apply.

| Outcome | Meaning | Operator action |
| --- | --- | --- |
| `retry_preview` | Current evidence is eligible and the report contains the exact retry plan. | Review and save that plan, then apply it with the same IID. |
| `retry_scheduled` | The reviewed plan passed again and the retained record returned to its resumable state. | Continue the reported retained workflow. |
| `correction_activated` | The reviewed plan activated an eligible retained correction. | Complete the reported correction and validation before publication. |
| `retry_ineligible` | The preview found known retry evidence ineligibility. It changes no ledger, GitLab, worktree, branch, or retry budget. | Correct the evidence, then use the exact `recovery resolve --choice retry` procedure. Do not apply the ineligible preview. |

`retry_ineligible` is only for known evidence ineligibility. An unavailable,
ambiguous, stale, or otherwise operationally uncertain check exits with an
error. It does not report `retry_ineligible` and changes nothing.

See [How to resolve factory issue recovery](../how-to/resolve-factory-issues.md#5-resume-a-safely-retryable-blocked-issue) for the retry procedure and its retained-correction route.

### `recovery inventory` and `recovery resolve`

`recovery inventory` is read-only. It never claims fresh work. `--apply` is
accepted for consistent invocation and changes nothing.

| Inventory area | Contract |
| --- | --- |
| Corroborated evidence | Lifecycle-projected GitLab issues; local ledger; each retained issue's passive advisory owner marker; fixed branch and retained worktree; merge request and exact-source pipeline; default-branch pipeline; configured project and credential identity; and controller, service, and short-lived project-lock evidence. |
| JSON and human aggregates | `total`, `open`, `closed`, `active`, `blocked`, `dependency_gated`, `human_action`, and `no_action`. |
| Detailed candidate | Per-issue owner state; active state; `human_action_required`; bounded blocked cause; dependency explanation naming direct open prerequisites and required open descendants of closed decomposition parents; current and intended prerequisite-label projections; exact next action. |
| Classification | Legitimately waiting, dependency-gated, safely retryable, superseded or duplicate, closure candidate, or needing a specific human decision. A retained dependent remains `dependency_gated` while any required descendant is open. After all required descendants close, preserved retained work can qualify as `safely_retryable`. Manual retry still needs an exact preview; guarded clean pre-checkpoint work may resume during an applied activation. For a blocked dirty worktree, inventory runs the retained-artifact evidence checks shared with retry preview. |
| Ineligible retained candidate | When those shared checks reject dirty retained evidence, including a legacy candidate, report `needs_human_decision` with a bounded reason. The next action is to inspect the retained worktree and ledger before choosing a reviewed resolution. Preserve both artifacts; inventory does not authorize a retry. |
| Report order | Failed default-branch and open-merge-request pipelines; retained issues by deterministic owner-evidence and IID ordering; safely retryable blocked items; dependency reconciliation; awaiting-human items; fresh ordinary work. |
| Fresh work | Reports it only. Never claims it. When direct prerequisites and required descendants clear, the saved issue may use guarded clean retry or an explicit retry; it does not start a child item. |
| `closed_no_action` | Takes precedence for a closed recordless split issue and a closed `COMPLETE` record. These terminal states need no action even when retained worktree, head, merge-request, or pipeline evidence drifted. |
| Closed records | Closed nonterminal records need human review. An untargeted inventory collapses settled closed-item detail but keeps aggregate counts. `--issue-iid IID` returns that item's exact evidence. |

`recovery resolve` plans one issue. It needs `--apply` and the exact
`--plan-file` to apply a resolution.

| Resolution action or control | Contract |
| --- | --- |
| Plan | Creates a reviewed, evidence-bound plan for one issue by default. |
| Apply | Holds the project lock, rechecks plan evidence, rejects drift, and returns a redacted before-and-after receipt. |
| Plan choice | Exactly one explicit target-issue choice: `retry`, `close`, `reopen`, `relabel`, or `supersede`. |
| Discovery-only marker retry | `retry` may reclassify one blocked `DISCOVERY` record whose only error is `remediation_marker_invalid`. Current issue metadata must now have a valid classification. The record must be untouched: it has no retained worktree or workflow evidence. Preview binds the configured project, issue, ledger revision and lifecycle label, recorded branch and worktree path, and current classification. Apply rechecks every bound value before returning the issue to `PREPARING` at `WORKFLOW`. It schedules durable preparation; it does not prepare a worktree or publish. |
| `close` and `supersede` | Need a reason and an evidence link or replacement issue. `supersede` uses `--replacement-issue-iid`. |
| `relabel` | Is limited to a canonical lifecycle label and uses `--target-label`. |
| Both commands | Never approve or merge, discard retained work, change project policy, treat issue text as authority, or claim fresh work. |
| Credential failure | Credential expiry or a configured-bot mismatch stops before a change. Use `agent-work credential rotate` and follow issue #84's credential-recovery handoff before a new resolution plan. |
| Procedure | See [How to resolve factory issue recovery](../how-to/resolve-factory-issues.md) for the reviewed operator procedure and examples. |

| Migration command or option | Contract |
| --- | --- |
| `migration preview` | Inventories legacy evidence and proposes only safe, idempotent records and label changes. |
| `migration apply` | Needs a reviewed preview file and `--apply`. It does not resolve ambiguous evidence. |
| `--json` | Returns the documented redacted JSON result. |
| `--debug` | Adds local diagnostics without credentials, issue text, prompts, or worker transcripts. |
| Invalid usage | Exits `2`. |

`upstream-finding preview` checks a manifest against an upstream GitLab target.
`--apply` is its only mutation switch.

| Upstream-finding state or outcome | Contract |
| --- | --- |
| Without `--apply` | Makes bounded GET-only requests. Returns `match` for an open issue with the exact reserved marker, or `would-create` when none exists. It never writes files or changes an issue, checkout, ledger, labels, assignments, approvals, or merge state. |
| With `--apply` | First runs that preview. On `would-create`, repeats exact-marker deduplication immediately before one possible create request. |
| Marker found during apply | Returns `match`. |
| Verified successful create | Returns `created`. |
| JSON success output | Contains only `schema_version`, `command`, `outcome`, the three fingerprints, `copied_path_count`, `evidence_link_count`, and optional `matching_iid`. |
| Human success output | Gives the same outcome, fingerprints, counts, and matching IID when present. |
| Redaction | Output, errors, and debug diagnostics never echo manifest prose, project identity, title, body, credentials, or evidence URLs. |
| Invalid manifest, unreadable credential file, access/API failure, or response-verification failure | Exits `1` with sanitized diagnostics. |
| Invalid usage | Exits `2`. |

### Slow-root reconciliation

`slow-root reconcile` reads a version 1 JSON manifest of diagnosed slow test
causes. Its top-level fields are `schema_version: 1` and `roots`, an array of
1–100 root objects. A bounded read rejects files over 256 KiB before parsing;
unknown fields are rejected. Each root has these fields:

| Field | Version 1 contract |
| --- | --- |
| `key` | Stable 1–80 character ASCII slug of lowercase letters, digits, and hyphens for the diagnosed cause. Keep environment, durations, run IDs, tree hashes, and dates out of the key. |
| `kind` | `ordinary-test` or `shared-fixture`. |
| `cases` | 1–100 distinct test selectors containing `::`, grouped by that one cause. Each is at most 512 characters and has no control characters; parametrized selectors may contain spaces and brackets. |
| `confirmed` | Caller-set Boolean, not inferred from timings or digests. An unconfirmed root stays diagnostic, even with `--apply`. |
| `evidence` | Up to 20 local records. A confirmed root needs at least two repeats in one environment; records from other environments may be included as diagnostics. Digests may match. Each record has `environment` (1–80 characters), `duration_ms` (greater than 0 and at most 3,600,000), lowercase 40-hex `tree_sha`, and lowercase 64-hex `junit_sha256`. |
| `title` | Nonempty proposed issue title of at most 255 characters. |
| `problem`, `outcome` | Nonempty issue text, each at most 5,000 characters. |
| `acceptance` | Array of 1–30 nonempty acceptance criteria. |
| `semantic_issue_iid`, `semantic_phrase` | Optional paired legacy hint: a positive exact issue number and nonempty phrase of at most 255 characters. |

The command rejects reserved `agent-slow-test-root:` text, regardless of case,
in `title`, `problem`, `outcome`, `acceptance`, `cases`, or `semantic_phrase`.

Without `--apply`, the command reads the configured project's bounded issue
inventory with GET-only requests and reports what it would do. It makes no
changes. An exact `<!-- agent-slow-test-root:<key>:v1 -->` marker in one issue
identifies that root across changed timings. An open match is `reuse`; a closed
match is `covered`. A legacy hint must name an exact issue number, and that
issue's description must contain the supplied phrase. The command checks its
current state. Ambiguous matches fail. Issue text is evidence to check, never
authority to create work or change policy.

With `--apply`, the command reads the complete inventory again. If more than
one confirmed root is still unmatched, it stops before any POST. Supply a
manifest subset with one unmatched root, then rerun after that root's issue is
verified. Reuse and covered roots may remain in the manifest. Each applied
invocation creates at most one open issue labeled `agent-slow-test`; an earlier
creation is reused on a later invocation. The command never edits, reopens, or
replaces an existing issue. It reads a new issue back and verifies its project,
issue number, title, description, labels, and state. `--json` returns `applied`
and an `items` array. Each item has `root_key`, `action`, and `issue_iid`;
`action` is `unconfirmed`, `would-create`, `reuse`, `covered`, or `created`.
If a creation's readback fails, creation may have completed. Rerun the default
preview to inspect the exact marker before applying again.

`worker-result build` validates one `WorkerResultBuildInput` object during the
live worker session and builds one controller-owned worker result.
The broker is small. Its rules are strict.
It takes one request. It may take one fix. It keeps its own state.

| Worker-result action, field, or outcome | Contract |
| --- | --- |
| Input and evidence | Reads one strict `WorkerResultBuildInput` JSON object from private scratch owned by the live workflow owner. The input is untrusted. The broker injects the record, changed paths, and checkpoint dependencies. |
| Private broker | `AgentInvoker` creates a private Unix-socket broker in a fresh `/tmp` directory for each invocation. It gives the worker the socket for the live builder request only. The worker receives no ledger directory, state-root path, or writable handoff artifact. |
| Request limit | The broker accepts one request. A standard rejection returns to the live producer for at most one corrected request in the worker budget. Acceptance is terminal. A request after acceptance or a third request after two rejected JSON objects rejects the result, including any earlier accepted receipt. |
| Builder time bound | Each request has the worker time still remaining. The builder cannot extend the worker budget. Timeout, deletion, race, oversize input, malformed framing, or an extra request fails closed. |
| Trusted result | The broker builds the accepted result in controller memory. It writes no saved acceptance record and exposes no accepted artifact to the worker. Model output and raw input are never controller state. |
| Controller verification | After exit, the controller verifies the nonce, project, issue, ledger revision, branch, source SHA, state, phase, envelope, and changed paths. Missing, malformed, stale, or mismatched evidence fails closed. |
| Cleanup | The controller removes the private scratch and socket directory for every outcome. A deletion or cleanup failure rejects the result. |
| Standard error and debug | Human status and diagnostics are sanitized. They never include credentials, environment values, worker prose, prompts, raw input, or transcripts. |
| Mutation boundary | Broker acceptance alone changes no ledger, Git, GitLab, publication, or tracked worktree content. The exact verified ledger transition remains the durable proof. |
| Preview stdout | Without `--json`, stdout is one concise human-readable status line. With `--json`, stdout is exactly one `WorkerResultEnvelope` JSON object. Preview output is diagnostic and is never controller state. |
| Applied JSON stdout | `--apply --json` emits one standard object with `accepted: true` and the broker nonce. A Red acceptance also includes exact trusted `red_checkpoint` evidence. |
| Success | Exits `0`. |
| Validation or runtime failure | Exits `1`. |
| Argparse usage failure | Exits `2`. |
| Input object | One strict `WorkerResultBuildInput` JSON object containing only bounded worker intent. The live producer stores it in private scratch and invokes the controller-checkout builder through the broker. Unknown fields are rejected. |
| Required worker intent | `workflow_lane`, `result_kind`, `change_summary`, and `next_action`. |
| Optional worker intent | `rejection_route`, `merge_request_draft`, `error_code`, `redacted_summary`, `split_plan`, `red_checkpoint_request`, and `suggest_close_evidence`. |
| Program-owned values | Protocol version; project and issue identity; ledger revision; expected state and phase; completed phase; changed paths; checkpoint dependencies; and the accepted envelope are controller-derived or controller-verified. The input must not contain them. |
| Non-authoritative discussion | Specialist receipts and audit prose may remain AI-to-AI context. They are not program authorization and `audit_summary` is not an input field. |

| Result state or receipt | Contract |
| --- | --- |
| `phase_status` | Controller-derived. The worker does not supply it. |
| Ordinary `workflow` | Every recorded result gets `complete`, including `split_required`. |
| Post-publication pipeline proof | Controller-owned. It needs the current MR source SHA, at least one current required job, and success from every non-retried job that does not allow failure. No worker result authorizes this transition. |
| Successful required-job proof | Advances the verified exact-source handoff to `awaiting-human`. |
| Missing or contradictory success proof | A terminal-success pipeline with pending, failed, canceled, skipped, or other non-success current required jobs, or with no current required jobs, stays in bounded observation. If proof is still absent after three retries, it moves to `needs-repair` without starting a worker. Source-identity mismatch blocks immediately; transport errors retry. |
| Red coding result | `red_checkpoint_ready` supplies the bounded `red_checkpoint_request`. While the producer is live, broker acceptance reruns and validates Red and returns controller-owned checkpoint evidence. It does not create a post-exit handoff. |
| Completed ordinary workflow lane | Use `coding` or `content` with that lane's applicable controller-owned checkpoints and final interface. |
| Workflow evidence | The controller verifies the applicable program-owned checkpoints and final interface before a transition. Model-authored specialist receipts and audit prose cannot authorize a result. |
| Coarse and legacy route | New coarse `WORKFLOW` results use the broker boundary. A legacy recorded phased route remains readable and finishes its recorded route. |
| `ready_to_publish` | Completes a coherent ordinary workflow. It needs the verified final interface, applicable controller-owned evidence, and a merge-request draft when retained work needs new publication metadata. |
| Draft metadata | The draft has nonempty `title` and `description`. A controversial but safe human handoff has a title beginning `Draft:`, an agreeing draft flag, and a decision review covering decisions and assumptions, alternatives, risks, reviewer checks, unresolved findings and evidence, tests, the correction-cycle ledger, and unchanged project-owned values. Existing-MR remediation is the exception: it supplies no new publication metadata. |

The other result kinds preserve their distinct controller transitions:

| Result kind | Use and needed intent |
| --- | --- |
| `red_checkpoint_ready` | A tests-only coding result. Supply the required `change_summary`, `next_action`, and exact `red_checkpoint_request`. The broker verifies and retains its checkpoint synchronously before accepting the result. Do not supply a merge-request draft, split plan, or error code. |
| `retryable` | A bounded transient outcome. Include the applicable error code and redacted summary when available. The controller schedules ordinary retained-worktree recovery. |
| `needs_human` | A blocker requiring human authority or a decision. Provide the bounded redacted summary, next action, and applicable error code. |
| `split_required` | An oversized fresh workflow. Use `workflow_lane: unknown` and a `split_plan` of 2–25 ordered, independently mergeable slices. It needs no receipts, audit, merge-request draft, or error code. |
| `suggest_close` | Recommend human closure with corroborated `suggest_close_evidence`: `reason` is `duplicate`, `superseded`, `already-satisfied`, or `no-longer-applicable`; `evidence` has 1–16 nonempty items. The worker never closes the issue. |

| Failure or output rule | Contract |
| --- | --- |
| Validation and runtime failures | Retain only a canonical field-specific diagnostic of at most 2,048 bytes and a stable reason code. |
| Protocol version | Version 1 is rejected. Fresh workers use version 2, including `split_required`. |
| Retention ban | Failures never retain request values, worker prose, prompts, chain-of-thought, credentials, environment values, or unbounded exception output. |
| Issues #40 and #78 | Protocol failures remain retryable, so retained worktrees stay in place for ordinary recovery. |
| Issue #80 | This does not claim an underlying cause for its retry exhaustion. |
| Envelope handling | The live producer stores strict `WorkerResultBuildInput` in private scratch and invokes the controller-checkout builder through the broker. Final messages, helper output, and other prose are never authoritative. |

### Worker validation diagnostics

This table gives the safe diagnostic rules.

| Diagnostic part or event | Contract |
| --- | --- |
| Contents | The diagnostic has only its stable origin, an allowlisted field path, and a stable corrective reason. |
| Exclusions and bound | It never includes a worker-supplied unknown field name, supplied value, raw output, prompts, or credentials. It is one line of at most 2,048 UTF-8 bytes. |
| No submission | If the live producer sends no broker request, the diagnostic is `worker_result_broker: no request received`. |
| Rejected JSON object without correction | `worker_result_broker: correction absent after rejected JSON object`. |
| Rejected other JSON without correction | `worker_result_broker: correction absent after rejected other JSON`. |
| Rejected invalid JSON without correction | `worker_result_broker: correction absent after rejected invalid JSON`. |
| Third request after two rejected JSON objects | `worker_result_broker: correction limit exceeded after rejected JSON object`. |
| Request after acceptance | `worker_result_broker: request after acceptance`. |
| Controller record | The controller carries the same safe diagnostic in condition history, status, recovery inventory, and the GitLab handoff. |
| Repeated automatic failure | One coarse phase with the same error code and diagnostic blocks the item. Exact clean pre-checkpoint proof may allow guarded auto-resume after a cooldown; otherwise, the operator corrects the condition and applies an exact `loop retry` plan. |
| Changed evidence | A changed error code, diagnostic, or phase is changed evidence and follows the existing bounded retry path. |

### Upstream-finding manifest v1

The YAML top-level mapping has exactly these fields.

| Field | Required v1 value |
| --- | --- |
| `schema_version` | Literal integer `1`. |
| `generated_project` | Exact mapping: public bare DNS `host`, positive integer `project_id`, and safe relative `path_with_namespace`. |
| `copier` | Exact mapping: credential-free network Git `source` and lowercase 40-hex commit `revision`. |
| `finding` | Exact mapping described below. |

| Input rule | Contract |
| --- | --- |
| Rejected documents | The command rejects unknown fields, YAML tags, anchors, aliases, duplicate keys, non-UTF-8 input, and documents larger than 64 KiB. |
| Scalar types | Scalars are strict: quoted or coercible numbers and booleans do not satisfy typed fields. |
| `finding.copied_paths` | Contains 1–100 safe relative paths. The semantic fingerprint normalizes duplicates and order. |
| `reproduction` | Contains 1–20 ordered, nonempty bounded text steps. |
| Text fields | `expected_behavior`, `observed_behavior`, and `validation_evidence` are nonempty bounded text. |
| `priority`, `severity`, and `scope` | `priority` is `low`, `medium`, `high`, or `urgent`; `severity` is `low`, `medium`, `high`, or `critical`; and `scope` is `generated-project`, `template`, or `both`. |
| `evidence_links` | Contains 0–20 exact mappings with `visibility: public` and a credential-free public HTTPS `url`. The command rejects local, private, ambiguous, insecure, userinfo-bearing, query-bearing, and fragment-bearing URLs. |

| Fingerprint or normalization | Contract |
| --- | --- |
| All fingerprints | The three lowercase SHA-256 fingerprints use canonical stable JSON. NFC normalization and harmless surrounding whitespace produce the same fingerprints. |
| Project fingerprint | Derives from normalized project identity. |
| Revision fingerprint | Adds normalized Copier source and exact revision. |
| Semantic finding fingerprint | Adds sorted, deduplicated paths, ordered reproduction, expected and observed behavior, and scope. |
| Mutable metadata | Priority, severity, check evidence, and evidence links do not affect the semantic fingerprint. |

| Marker, state, or action | Contract |
| --- | --- |
| Reserved marker | The exact reserved marker is `<!-- agent-work-upstream-finding:v1 project=<project_fingerprint> revision=<revision_fingerprint> semantic=<semantic_finding_fingerprint> -->`. It is remote recovery evidence. |
| Local state | This standalone command creates no durable local ledger or state machine. |
| Preview and deduplication | A preview returns `match` when an open issue has that exact marker and `would-create` otherwise. An applied invocation that finds the marker at either deduplication point returns `match`. |
| Verified creation | A verified new issue returns `created` with its IID. |
| Applied request | The command sends one fixed redacted title and bounded, evidence-backed body with exactly one canonical marker. It explicitly makes the issue non-confidential. |
| Body inclusions | The body can include checked finding evidence, copied paths, reproduction, priority, severity, scope, and canonical fingerprints. It can include evidence links only when their manifest entries declare `visibility: public`. |
| Body exclusions | The body omits raw generated-project host, ID, and path; raw Copier source and revision; credentials; and injected reserved markers. The command does not label or assign the issue. |
| Verified response | The returned create response identifies the configured numeric upstream project, a positive IID, open and non-confidential state, the exact sent title, and a description with exactly one canonical marker. Its URL is the canonical HTTPS issue URL for the verified upstream host, project path, and IID. |
| Response failure and retry | Any mismatch is a sanitized failure. A transport, nonzero, or malformed create response can be uncertain, so the invocation never retries its POST. Rerun explicitly after such a failure. |
| One-POST bound | The marker scan either recovers the issue as `match` or, only when no exact marker exists, makes at most one new create. This immediate repeat-deduplication and one-POST bound does not promise to resolve concurrent cross-host races. |
| Exits | Valid preview, match, and verified-created outcomes exit `0`. Sanitized check, access, API, and response failures exit `1`. Argparse usage exits `2`. |
| New issue and authority | A new issue is open, non-confidential, unlabelled, and unassigned. It enters the explicit upstream project's normal guarded factory issue workflow only if that project enables one. Neither the generated/source project nor the upstream template project or factory approves or merges. Humans retain approval and merge authority. |
| Base branch | `AGENT_WORK_BASE_BRANCH` is needed and must name a valid configured `agent/...` service branch. Setup uses the canonical `agent/work-base` branch. The runner checks it before it claims an issue, changes a label, or creates a ledger record. An omitted value fails the command. |

## Systemd wrapper environment

| Environment value or setup | Wrapper contract |
| --- | --- |
| `GITLAB_HOST`, `GITLAB_PROJECT_ID` | Need a bare host and positive numeric ID. Missing or malformed values exit `2` before the wrapper runs. |
| Mode-600 service environment file | Holds public identity values and recorded executable paths; never GitLab credentials. |
| `AGENT_WORK_DIRENV` | If set, names an executable absolute path. `${AGENT_WORK_CHECKOUT}/.envrc` must be a regular file and can provide directory-scoped credentials. |
| Captured service host and project ID | Passed to `/usr/bin/env`; `direnv` cannot change the configured target. Setup previews the wrapper in scrubbed `env -i` before enabling the timer. |
| Inherited `direnv` bookkeeping | Clear `DIRENV_DIFF`, `DIRENV_DIR`, `DIRENV_FILE`, `DIRENV_IN_ENVRC`, and `DIRENV_WATCHES` before starting the worker. |
| Kept setup | Keep normal non-bookkeeping setup, including `DIRENV_CONFIG`, captured public GitLab identity, and the scoped `GITLAB_TOKEN` exception. Remove other credential-shaped variables. |
| Nested retained-worktree command | `direnv exec .` can run without an `.envrc`; cleared stale metadata does not unload the allowed scoped token. |

Use the installed contract as it is. It keeps service setup safe.

| Setup state | Required action |
| --- | --- |
| Stale or incomplete installed contract | Before starting or enabling the timer, run `service-environment migrate`. |
| No installed contract | Use the supported setup procedure to recreate it. |
| Manual reconstruction | It is not a supported upgrade path. |
| Initial setup | It writes the fixed current service-environment contract. |

## Git authentication contract

| Authentication element or condition | Contract or outcome |
| --- | --- |
| `GITLAB_TOKEN` | Directory-scoped GitLab credential. The repository-owned Git credential-protocol helper turns it into HTTPS Basic credentials only when Git requests them at runtime. |
| Git consumers and remote | The serial controller/service and ordinary `$git-flow` publication use the helper. The remote stays a credential-free HTTPS URL; the helper neither rewrites nor persists it. |
| Prohibited locations | Never put the token or derived Basic-authentication header in a remote URL, persistent Git setup, service environment file, command arguments, process reports, logs, diagnostics, ledgers, or retained records. |
| Credential isolation | Git prompts are off. Clear inherited credential helpers and hostile ambient credential setup so nothing overrides the repository helper. |
| API route | Uses configured bare `GITLAB_HOST` and positive numeric `GITLAB_PROJECT_ID`. |
| Git readiness route | Uses the same configured token through the helper and does not change the remote. |
| Missing token | Setup failure before remote mutation. |
| Invalid or expired token | Authentication failure. |
| Authenticated identity without needed push scope | Authorization failure. |
| Known temporary transport failure | Retry only when the calling operation allows it; diagnostics report only the safe classification. |
| Interface and authority | Adds no CLI and does not change GitLab API semantics, branch protection, roles, token creation or rotation, or the human-only approval and merge boundary. |

Use this rule for Git.

## Lifecycle

GitLab holds the shared queue. The local ledger records transactions.
Project-level loop enablement lets the runner select work without attendance.
Active or resumable work comes first. The runner does not trust API order or
read issue notes or comments. Bad creation-time evidence fails safely. Labels
never change an existing issue retroactively.

For new work, the runner uses these rules.

| Check | Result |
| --- | --- |
| Issue is open and non-confidential; has no lifecycle projection, ledger record, open split prerequisite, or required open descendant of a closed decomposition parent | Eligible for fresh selection. |
| Exact `agent-priority::high` | Rank 0. |
| No recognized exact priority value, including unknown priority-like labels | Rank 1. |
| Exact `agent-priority::low` | Rank 2. |
| Same rank | Compare absolute, timezone-aware `created_at`, then numeric IID. |

At project setup, the runner creates the lifecycle and manual-tracker labels
below with fixed colors and plain descriptions. A person can use high to speed
up new work, remove either priority label for normal order, or use low to defer
work.

| GitLab label | Ledger state or outcome | Meaning |
| --- | --- | --- |
| `agent::running` | preparing, executing, retry-wait, publishing, observing, or repair | Work is active or safely resumable. |
| `agent::awaiting-human` | awaiting-human | An exact MR, its named source commit, and one factory-authored actionable issue handoff await human review. |
| `agent::blocked` | blocked | The record awaits guarded clean retry or the recorded human action. |
| `agent::failed` | failed | The runner could not safely reconcile or record an exceptional condition. |
| `agent::needs_repair` | needs-repair | Legacy or reviewed repair, exhausted required-job proof after a successful exact-source pipeline, or a canceled exact current-source pipeline for a retained observing owner awaits exact recovery. An ordinary failed pipeline uses its diagnostic repair path. |
| `agent::repair_failed` | repair-failed | An automatic or explicitly reviewed repair attempt failed or its exact evidence changed. It retains the evidence needed for safe follow-up. |
| `agent::suggest_close` | suggest-close | Corroborated duplicate, superseded, already-satisfied, or no-longer-applicable evidence recommends human closure. |

The manual marker is outside the `agent::…` lifecycle label namespace.

| Marker or label | Rule |
| --- | --- |
| `agent-work::manual` | Marks an open issue that a person keeps as a manual tracker or recovery checklist. The runner skips it during fresh selection. |
| Remove the exact `agent-work::manual` marker | Restores fresh selection for an otherwise eligible open issue. |
| `agent-work::manual` projection | It creates no ledger record or worktree, cannot feed orphan reconciliation, and does not change prerequisite projection or authority. It is not a lifecycle or error state and cannot bypass a prerequisite. |
| `agent::needs_repair` | Uses `#D4A72C`. |
| `agent::repair_failed` | Uses `#C91C00`. |
| `agent::suggest_close` | Uses `#6F42C1`. |
| The three labels above | Exclude each other and `agent::running`, `agent::awaiting-human`, `agent::blocked`, and `agent::failed`. None is eligible for fresh selection. |
| `agent::blocked` | Still projects an authority, safety, or unresolved-evidence decision. |
| `agent::failed` | Still means an exceptional or unrecoverable condition. |

| Queue label | Fresh-work rank | Control |
| --- | --- | --- |
| `agent-priority::high` | 0 | Human expedite |
| no recognized exact value | 1 | Normal. Remove a scoped label |
| `agent-priority::low` | 2 | Human defer |
| `agent-work::manual` | excluded | Human-maintained tracker or recovery checklist. Remove to restore ordinary eligibility. |

The durable flow is below.

| State or event | Next action or outcome |
| --- | --- |
| `preparing` | Moves to `executing`. |
| `executing` | Can move to `retry-wait`, return to execution, or produce `publishing`. |
| Verified publication | Moves to `observing`. The controller checks the current MR source SHA and its current jobs. |
| At least one current required job exists and all succeed | Writes the actionable handoff for the exact MR source commit, then moves to `awaiting-human`. |
| Failed pipeline with an exact retained owner | Uses the existing failed-job diagnostic and repair path when safe. |
| Canceled exact current-source pipeline for a retained observing owner | Moves to `needs-repair` through ordinary observation. It does not claim success or start failed-job repair. |
| Terminal-success pipeline with missing or contradictory required-job proof | Reobserves at most three times. If proof remains absent, moves to `needs-repair` without a worker. This includes pending, failed, canceled, skipped, or other non-success current required jobs, and empty or all-optional inventories. |
| Stale or ambiguous identity; transport error | Identity blocks the handoff; transport errors retry. Neither claims success. |
| Condition blocked by a person | Moves to `blocked`. After the recorded action, an operator explicitly retries that IID. |
| Human merge or closure | Ends an awaiting-human record. |

### Awaiting-human issue handoff

Use this note to review the shown work.

| Handoff part | Rule |
| --- | --- |
| Durable `awaiting-human` projection | Has one factory-authored issue note for its exact merge request source commit. |
| Note | Links to the merge request, names the commit, and asks a person to review that version. |
| Acceptable version | A person can approve and merge it. Only a human can approve or merge. |
| Unacceptable version | A person leaves actionable general or inline merge-request feedback. |
| Reviewer assignment | The runner never auto-assigns a reviewer. This contract has no trusted, configured reviewer identity. |
| Fixed marker | Identifies the note from the configured project, issue, merge request, and source SHA. The runner accepts it only when the configured factory identity wrote it. |
| Reconciliation | Does not duplicate a note for the same source SHA. It creates a new handoff when the verified source SHA changes. |
| Applied activation without the exact handoff | Repairs the `awaiting-human` record. |
| Temporary note-write failure | Leaves the record on durable retry and prevents a new `agent::awaiting-human` label. |
| Restart after the note is written | Reuses the note and repairs the label. Exact merge-request and worktree evidence is still needed. |

| Lifecycle point | Action or boundary |
| --- | --- |
| Newly selected issue | Attempts its entire applicable repository workflow in the same activation. |
| Durable record | Resumes at its coarse `WORKFLOW` boundary and durable worktree. |
| First rollout | Has no sidecar checkpoint for each role or workflow stage. |
| Legacy phase-ledger value | Finishes its recorded route. |
| New work | Uses `WORKFLOW`. |
| Approved design | Router gives Architect's reviewed plan to Red first. After the accepted Red checkpoint, the conditional Agent-policy Writer completes portable policy work or returns N/A. Green and the Diátaxis Writer then run concurrently. |
| Writer scope | Red changes only `tests/`. Green changes code and tests. The Diátaxis Writer changes only `docs/`, `README.md`, and their declared portable `template/` mirrors. The Agent-policy Writer changes only `AGENTS.md`, `.agents/**`, and their declared portable `template/` mirrors. |
| Before validation | Router waits for every applicable receipt, or the exact not-applicable record. Green and the Diátaxis Writer must both stop editing. Ordinary serial coding requests controller staging and the Green commit through the private candidate socket. A pending target merge validates its joined staged tree before the ordered two-parent commit. In interactive work, Main stages the combined work and records its tree ID. |
| Green | Implements code and tests, checks the final interface with the writer, and consolidates review feedback. It never changes docs, README, or agent-policy paths or their mirrors. |
| Before specialist review | Ordinary serial coding requires the verified Green SHA and committed tree. A pending target merge instead uses the validated joined tree and exact source and target identities. Each applicable specialist, including `$license-checker`, reviews that same candidate. Consistency Judge uses controller-owned reads when copied facts changed. |
| Findings | Green combines all initial findings. |
| Policy correction | Goes through the still-live Router to the Agent-policy Writer. Green cannot fix policy. |
| Final decision | Before Router makes it, every changed candidate follows the new Judge, applicable review, and Finalizer sequence. |

For content work, the Documentation Writer is the sole editor. After it stops,
the controller stages and reads the candidate. Consistency Judge uses that
evidence to check copied facts when they changed. A missing or rejected read
blocks finalization. The Documentation Finalizer records the verdict or exact
not-applicable reason in its existing receipt. Content work has no coding
Judge hook or JUnit evidence. A correction requires another stage and read
before the finalizer reviews the new tree. After other checks and corrections,
the finalizer checks readability on each changed Markdown file in the exact
staged tree. It records N/A when no Markdown changed.

### Preselection pipeline gate

Check pipelines first. Use the current SHA. Do not use an old one.

| Pipeline scope | Rule |
| --- | --- |
| Applied user-directed `agent-work loop run` and systemd wrapper next-activation path | Before new work, inspect the configured numeric project's newest default-branch pipeline and each open MR's newest pipeline. Bind each MR observation to its current source SHA, never an older source commit. |

| Gate input or state | Controller action |
| --- | --- |
| One immediate active record other than `retry-wait` | An ordinary unpinned activation resumes that record and leaves every `retry-wait` record unchanged. |
| No immediate active record | Choose only the earliest due retry by `(retry_at, issue IID)`; future or undated retries remain queued. |
| Multiple retained active records | Status and inventory report every record in deterministic order and classify each issue independently. Ordinary unpinned admission remains capacity-one and fails closed. A reviewed exact-IID recovery may advance its stale or missing target only when retained identities are disjoint and no different verified live owner exists. |
| Successful pipeline | Clear its observation. |
| Present default-branch pipeline with a valid nonterminal status | Keep checking the exact open-MR inventory. Park any exact pending MR observer, then select another eligible issue if one exists. Do not poll the default-branch pipeline in this activation; the next timer activation checks it again. |
| New exact pending MR observer | When the applied issue moves from `PUBLISHING` to `OBSERVING`, it reports `outcome=activation_complete` and `stop_reason=pipeline_wait` without polling. The wrapper previews once. |
| Retained exact nonterminal MR observer | Park it from ordinary ownership only when its SHA, retained owner, worktree, configured project, and target match. Later preview or apply may report eligible and start another item. If none exists, it may be idle and yield to the timer. Parking does not create `pipeline_wait`. |
| Missing, malformed, inaccessible, identity, or check-pipeline evidence | Fail closed. Default-branch evidence blocks ordinary work. Ownerless open-MR evidence is non-owning and item-local. A retained inexact or stale `OBSERVING` claimant stays visible for item-local `_observe` reconciliation before fresh work. It blocks or repairs when identity cannot be established. |

| Gate order | Rule |
| --- | --- |
| Before ordinary new-issue selection | Reconcile the listed work first. A corroborated failed pipeline takes priority over ordinary selection. |

| Failed-pipeline condition | Eligibility, action, and outcome |
| --- | --- |
| Exact failed MR pipeline with one retained owner | Automatic repair needs exact configured project, merge-request, branch, source-SHA, and worktree evidence. Consider exact candidates in fixed order and run at most one repair workflow per activation. |
| Canceled current-source open-MR pipeline | Keep this evidence local to its MR after checking the full pipeline inventory and eligible failed-pipeline repairs. A retained observing owner reaches `needs-repair` through ordinary observation; do not start failed-job repair or claim pipeline success. An ownerless canceled MR does not stop unrelated eligible work. |
| Scheduled repair backoff or quarantine | Skip that item; seek another eligible exact repair before ordinary work. A failed automatic repair creates a durable 20-minute per-item backoff. After three automatic repair attempts, quarantine or defer it for human review; do not select it automatically. |
| Status or preview | Retain attempt count, timing, evidence, and next action. |
| Failed default-branch pipeline, or canceled default-branch pipeline | Stop before unrelated work and report the pipeline evidence. |
| Failed ownerless MR pipeline | Keep the known current-SHA evidence non-owning and item-local. Expose its action. It cannot choose a repair owner and does not starve unrelated eligible work. A retained inexact or stale claimant instead stays visible for `_observe` before fresh work. |

| Repair inventory | Rule |
| --- | --- |
| Before an eligible repair | Read a bounded job inventory for the verified project, merge request, source SHA, and pipeline. Include retry attempts. Ignore failed `retried=true` jobs because a later attempt replaced them. Require 1–8 current failed `retried=false` jobs. More than eight stops repair before any trace read. Order the current failed jobs by job ID. |

| Evidence step | Required handling |
| --- | --- |
| Job traces | Fetch each current failed job's trace in job ID order. Strip control sequences and secret-like content, then cap each retained diagnostic. A missing or empty trace stops repair. |
| Typed evidence | Bind project, merge request, source SHA, pipeline identity, pipeline SHA and status, and each job's ID, name, stage, status, failure reason, trace provenance, and trace status. |
| Persistence and restart | Save all checked, bounded evidence before one repair workflow. The first job uses the existing `diagnostic` field; up to seven more use `additional_diagnostics`. On restart, check and reuse the same ordered, fingerprint-bound evidence. Give the workflow prompt every retained diagnostic, labeled as untrusted. |
| Excluded data | Raw job traces never enter the ledger, status report, or workflow prompt. |
| Invalid evidence | A wrong or changing identity or status, no current failed jobs, more than eight current failed jobs, any missing or empty trace, or a schema or API failure stops repair without starting a worker. The handoff reports that an exact diagnostic is unavailable. This process never retries or cancels jobs or pipelines. |

| Stage | Required order and boundary |
| --- | --- |
| Post-publication proof | Check the current MR source SHA and bounded job inventory. Require at least one current required job; every non-retried job with `allow_failure=false` must succeed. A failed pipeline keeps the exact failed-job diagnostic and repair path. A terminal-success pipeline with a non-success required job or no required job gets three bounded reobservations, then `needs-repair` without a worker if proof remains absent. Source-identity mismatch blocks immediately; transport errors retry. |
| Ordinary loop, canary checkpoint, and targeted recovery | Apply the pipeline and default-evidence gate; then an exact failed-MR pipeline with one corroborated retained owner for one eligible repair workflow; then one durable immediate record, an exact-IID reviewed reclassification for exceptional retained work, and waits. A reviewed exact-IID plan may advance only its evidence-bound stale or missing target. Ordinary `loop run` remains capacity-one and fails closed before selecting new work. |
| Repair retention and scope | A failed repair or changed exact evidence retains evidence, failure, timing, and worktree for safe later handling. Scheduled repair backoff and automatic-repair quarantine are item-local eligibility outcomes, not `retry-wait` or global blockers. |
| Reclassification and conflict | Reclassify one exact IID at a time with that issue's live owner evidence. Status and inventory remain read-only; neither creates or acquires an owner marker. Distinct IIDs with disjoint branch, worktree, and MR identity are observable records, not a shared-identity conflict. Exact-IID recovery may reconcile a stale or missing target even when another retained record is stale or missing. Unknown owner evidence, duplicated or shared branch, worktree, or MR identity, or another verified live owner fails closed with the precise next action. |

### Durable scheduler states and liveness

This table lists each state. It gives the next step for that state. One item
does not stop other work. The runner stops only when it cannot tell which
project or item is safe to run.

| State | Representative evidence | Scheduler result |
| --- | --- | --- |
| `PREPARING` | The selected record matches. The retained worktree is clean. | Resume this item when it owns the one-item advancement capacity. Repair it when safe. |
| `EXECUTING` | The issue's owner marker and retained-worktree evidence are recoverable. | Resume this item when it owns the one-item advancement capacity. Repair it when safe. |
| `RETRY_WAIT` | `retry_at` is due. No owner is immediate. | Resume the earliest due retry, including a clean pre-selection capacity wait. A future retry is deferred. Scan on. |
| `PUBLISHING` | The retained branch is exact. Publication identity is pending. | Resume publication for this item. Repair it when safe. |
| `OBSERVING` | The current source SHA, owner, worktree, project, and target are exact. Pipeline evidence exists. | Direct or recovery handling observes immediately. Ordinary admission parks only this exact nonterminal item after its current activation, then scans for other eligible work. A retained stale or inexact claimant remains visible for item-local `_observe` reconciliation before fresh work. It blocks or repairs when identity cannot be established. Later terminal or conflict evidence takes priority: a conflict or exact failure enters repair, success with required jobs reaches human review, and exact canceled, skipped, or manual evidence enters item-local `NEEDS_REPAIR`. |
| `REPAIR` | One failed current-source pipeline has a unique owner. | Run at most one repair workflow. Or resume one. Do this in the activation. |
| `NEEDS_REPAIR` | A legacy or reviewed recovery record exists, or required-job proof was exhausted after a successful exact-source pipeline. | Defer. Expose reviewed recovery for the exact IID. Scan on. |
| `REPAIR_FAILED` | Per-item repair timing and attempt evidence exists. | Resume only when it is due. It must be safe. Otherwise, defer or quarantine locally. Scan on. |
| `SUGGEST_CLOSE` | Closure evidence is corroborated. | Skip. Expose the human closure decision. |
| `BLOCKED` | A human action or unresolved item evidence is recorded. | Resume one clean pre-checkpoint issue only after exact proof; otherwise expose the needed action. |
| `AWAITING_HUMAN` | The review handoff is verified. Its MR source SHA is exact. | Skip automatic work. Preserve the human handoff. |
| `FAILED` | An item-local exceptional failure and next action are recorded. | Skip automatic work. Expose recovery action. |
| `COMPLETE` | The issue is closed or the record is complete. | No action. |

| Topic | Contract |
| --- | --- |
| Project-wide stop | Stop for project identity failure. Stop for credential failure. Stop for a held project lock. Stop for an ambiguous project lock. Stop for corrupt shared state. Stop for a truly ambiguous cross-item identity, including duplicated or shared branch, worktree, or MR identity. |
| Activation limit | Advance at most one workflow-owning item. Advance at most one repair workflow. Do both in one activation. A different verified live owner occupies that one-item capacity; multiple live owners are capacity evidence, not by themselves a shared-identity claim. The limit is per activation and is strict. |
| No-progress rule | A fingerprint binds the item, state, and phase. It also binds relevant exact branch/MR/pipeline identity and action. A material change is progress. A closed earlier outcome is progress. Otherwise, defer, quarantine, or expose the item. Do not use later activations. |
| Status and preview | Report the reason and attempt information. Report timing, an evidence summary, and the next action. Preview is read-only. It uses the same eligibility logic. It does not claim work. It does not change GitLab, the ledger, worktrees, branches, or services. |
| Controller controls | Retry counts are trusted controller constants. Repair budgets, delay bounds, priorities, and lock rules are too. Untrusted issue or merge-request text cannot set, reset, or affect them. |
| Failed-pipeline inventory | Skip blocked owners. Skip deferred, quarantined, retry-exhausted, awaiting-human, and dependency-gated owners. Then consider another exact eligible repair. If the gate permits it, consider ordinary eligible work. A local ineligibility does not stop the project. It never creates global `pipeline_blocked`. |
| Project-wide pipeline stop | Stop for a failed or canceled default-branch pipeline. Also stop for a cross-item owner. The owner must be truly ambiguous. An ownerless failed or canceled open-MR pipeline is item-local. |
| Fast-forward reconciliation | Require configured numeric project identity. Require retained MR identity and URL. Require a matching source branch, the configured target project, and the default branch. Require a clean retained worktree and no competing identity. The retained branch and `HEAD` must equal the MR source SHA. The retained SHA must be an ancestor of the MR SHA. On failure, quarantine the item locally. Then continue scanning. Never choose an MR by recency or issue text. |
| Suggested closure | `agent::suggest_close` needs structured, corroborated evidence. The evidence can show a duplicate. It can show a superseded issue. It can show an already-satisfied or no-longer-applicable issue. It is a recommendation, not a closure. A human closure alone reconciles the record to `COMPLETE`. A human closure also clears the label. The runner never approves, merges, or closes an issue. |

| Closed-issue recovery step | Outcome |
| --- | --- |
| Start an applied activation | Take the single-project lock. Reconcile intent with observed evidence. Do this before open work. Then obtain one bounded, fixed IID-ordered inventory of closed issues with lifecycle labels. |
| Listed issue with a ledger | A published candidate retains its wrapper and lifecycle label until fresh exact MR, successful exact-source pipeline, and current required-job proof permit `COMPLETE`. Other closed records normalize to `COMPLETE`; clear transient retry, intent, error, worker-exit, and pending-feedback state before authenticated label removal. |
| Listed issue without a ledger | Remove stale lifecycle labels. Do not create a ledger record. Do not create a blocked or complete state. Do not create a journal or worktree. |
| Write or label failure | A ledger-write failure leaves the label. A recorded issue's label-removal failure leaves its canonical `COMPLETE` record. It also leaves a stale label for a later activation. An orphan failure leaves only its stale label. Earlier IID successes remain reconciled. They stay reconciled. |
| Scope and next step | This housekeeping uses neither `max_segments` nor the open-work budget. It starts no worker. A published candidate reads repository configuration, MR, pipeline, and jobs to verify completion. It does not mutate Git, a worktree, branch, or MR. Open orphan projections keep the usual blocked recovery path. After inventory, advance at most one selected or resumed open issue. Recovery applies at the worktree and coarse workflow boundaries, not at every internal agent handoff. |

| Liveness event | Scheduler action |
| --- | --- |
| Active Python activation | Advance at most one issue. Keep its active-work budget. That budget is 10,800 seconds. |
| Present default-branch pipeline with a valid nonterminal status during `loop run --apply` | Do not poll or wait in the activation. Continue the exact open-MR inventory and ordinary selection. The next timer activation checks its state again. Missing, malformed, failed, or canceled default-branch evidence still blocks unrelated work. |
| New exact pending MR observer | On its `PUBLISHING` to `OBSERVING` transition, finish with `outcome=activation_complete` and `stop_reason=pipeline_wait` without polling. The wrapper then makes one fresh preview. |
| Retained exact nonterminal MR observer | Park it from ordinary ownership when its SHA, retained owner, worktree, configured project, and target match. Parking does not create `pipeline_wait`; later preview or apply may select another item or be idle. It never starts more than one worker at a time. |
| Repair uncertainty handoff | When an applied `loop.run` report has schema version `1`, `outcome=activation_complete`, and `stop_reason=needs_repair`, the wrapper emits that report and exits `0`. It does not preview eligibility or start another worker in that service pass. |
| Wrapper continuation | Continue after a valid `prerequisite_labels_reconciled`, `awaiting-human`, `blocked`, `failed`, or `complete` report. Make one fresh read-only eligibility check and one fresh read-only preselection-pipeline check. For an ordinary terminal handoff, start another applied activation only for `eligible`. Immediately after the exact `activation_complete/pipeline_wait` publication handoff, one preview may start another applied activation for `eligible`, `resume_available`, or `repair_ready`. Re-query GitLab, then fast-forward the clean service base. This housekeeping is outside the per-issue budget. |
| Exact failed current-source pipeline | Activate `repair`. Run the repair workflow. Run it in the same applied activation. |
| Handoff or idle | A future `retry-wait` remains a handoff. A future `time-limit` or `segment-limit` remains a handoff. A human or safety block remains a handoff. Missing, failed, or unrecognized evidence remains a handoff. `idle` means no eligible or resumable work exists. |
| Timer and scope | `OnUnitInactiveSec=20min` starts after the service exits, including a `service_time_limit` yield. The next timer activation checks for work and may resume retained work. It also rechecks a valid pending default-branch pipeline. A retained exact MR observer may poll within a later activation. The activation deadline and systemd outer timeout still apply. The loop is serial for each host and project. It is not multi-host coordination. |

## Ledger, worktrees, and audit

The ledger is the durable audit record. It keeps facts a restart must use.

| Audit area | Contract |
| --- | --- |
| Location and worktree | State is below the XDG state directory, keyed by configured GitLab host and numeric project ID. Each `loop status` ledger item names its durable worktree path. Each active issue has one atomic, restrictive-permission ledger record and durable worktree. |
| Version 2 fields | Bounded issue title; activation, latest-meaningful-event, and completion timestamps; main, source, publication, and merge-request identities; and bounded retry and failure history apart from the current actionable error. |
| Phase timing | `phase_timings` keeps the newest 64 controller-computed entries. Each entry has `phase_or_role`, `attempt`, `started_at`, `finished_at`, `elapsed_seconds`, `outcome`, and `classification`. Classification is `execution` or `wait`; timestamps come from controller clocks. Phase timing is the schema term for durable phases or controller-observed roles. Optional `provenance` binds project, issue, source commit, candidate tree, and `boundary_revision` to the observed controller attempt. With the verified-observation route selected, clock loss creates no timing sample. An absent observer preserves legacy unbound entries; those entries cannot prove a verified total. `phase_timing_count` tracks the full count when known: 64 retained entries with count 64 alone imply no pruning; count 65 with 64 retained means pruning. Nonempty legacy history without a known count stays unverified. |
| Verified stage report | Runtime item reports expose `stage_timing_report`; worker and Finalizer input uses `controller_stage_timing` from the validated controller record. MR descriptions and completion summaries use the same bounded controller report. Completion includes the final controller observation. The optional MR timing appendix is omitted when the final canonical description would exceed existing bounds; the original human draft stays intact. Valid rows show observed coarse execution seconds bound to the expected source and attempt. Whole-worker time cannot stand for internal role timings. `coverage` is `partial` when verified rows exist, otherwise `unavailable`. `history_complete` covers only known coarse history. Missing or foreign proof, unknown or pruned history, invalid ordering, overlap, or clock loss leaves `execution_seconds` unavailable; valid individual rows may remain. `wait_seconds` is always unavailable: no internal-role or external/human wait measurement is supplied, and gaps or polling are never inferred waits. Reports grant no workflow authority or timing threshold. Full internal-role and wait instrumentation remains work for issue 329. |
| Pipeline observation timing | A new `_observe` sample uses `phase_or_role=pipeline-poll` and classification `execution`. It measures the controller's observation call, not elapsed pipeline wait or backoff sleep. Older `pipeline-wait` entries remain in the ledger with their original `wait` classification. Status reports their retained duration as `legacy_poll`, separate from `observed_wait`. |
| Exhausted retry | The current condition is `retry_exhausted`. Bounded history keeps the latest sanitized underlying condition code and detail. Human and JSON status show the retained actionable cause, and the safe lifecycle result is `agent::blocked`. |
| Historical workflow context | Legacy records may keep a bounded workflow summary and role discussion. The runner treats that prose as non-authoritative. Current transitions use controller-observed state, phase, revision, and source identity. They also use controller-owned checkpoints and observed changed paths. |
| Excluded audit data | The ledger never stores prompts, chain-of-thought, credentials, environment values, issue bodies, comments, transcripts, raw output, or unbounded data. |
| Legacy records | Old records stay readable. A missing `phase_timings` field loads as an empty list. Other missing legacy evidence is `unknown`. The next persist adds current fields and never invents past values. |
| Local log | The application writes `agent-work.log` in that XDG project-state directory. It is application-owned evidence beside, not instead of, the systemd service journal. |
| Mutation evidence | The runner saves intent before an external mutation and observed evidence after it, so it can reconcile interrupted idempotent work. |
| Dirty worktree and stashes | A dirty durable worktree stays for its next workflow attempt. The runner inventories recovery stashes only; it never applies or drops them. |
| Ambiguity and corruption | Ambiguous worktrees, branches, labels, or stashes become blocked for human review. A corrupt record is blocked when reportable and failed only when it cannot be recorded safely. |
| Clean unpublished interruption | Before worker invocation, normalization needs agreement among the open issue and lifecycle label, fixed branch and worktree, and recorded baseline; the baseline worktree must be clean; and there must be no MR, publication, draft, intent, feedback, receipt, or decomposition evidence. The runner keeps all artifacts, changes the record to `blocked`, gives actionable retry guidance, and never deletes a ledger, branch, or worktree. |
| Dirty unpublished interruption | A reviewed `handoff_stale_dirty_claim` plan can block one stale `EXECUTING`/`WORKFLOW` claim while preserving its dirty worktree and any saved Red checkpoint. The plan binds either the checkpoint digest or its absence. The receipt binds the plan fingerprint and dirty snapshot fingerprint for a later manual decision. Independent issues may proceed after that claim becomes blocked. |

## Status projection

`loop status` projects the durable record. It adds no audit store. It shows raw
ledger state and coarse phase.

| Projected state | Durable condition |
| --- | --- |
| `selected` | Claimed and preparing before a source SHA exists. |
| `running` | Any remaining active or retry state, including `retry-wait`. |
| `awaiting-human-review` | Verified handoff is awaiting human review. |
| `blocked` | Blocked or raw failed state. |
| `complete` | Completed record. |

| Output or evidence | Contract |
| --- | --- |
| Human item line | States whether work runs, what changed, the current failure or condition, and the next action. It includes issue, projected and raw state, phase, identity facts, and available worker-exit evidence. When phase timing evidence exists, its `timing=` token gives `count`, `scope=retained`, `coverage=unknown`, `internal_roles=unavailable`, `observed_execution`, `observed_wait`, and the latest phase or role, outcome, and elapsed seconds. A subtotal without a sample is `unavailable`. It includes `legacy_poll` when old pipeline-wait samples exist and `history=possibly-truncated` at 64 entries. The subtotals do not establish full workflow coverage or total pipeline wait time. |
| JSON detail | Includes the full bounded `phase_timings` list, controller evidence, timestamps, conditions, and corroboration. Historical role and audit prose remains non-authoritative context. |
| JSON form | It is compact, versioned `CommandReport` version 2. Status records have the human view's identity, projection, current-versus-history, bounded audit, and observation data. |
| List bounds | A report gives total matching records and truncation metadata. Only an unfiltered list is capped at 100 records. |
| Observation use | Treat a missing, stale, failed, or mismatched observation as that condition, never as current truth. |
| Live corroboration | It is read-only and best effort. Injected GitLab, repository, and clock boundaries inspect the issue and retained worktree. |
| Per-issue owner field | Status and recovery inventory read each retained issue's passive advisory owner marker. The state is `active` only for that marker's verifiably live PID, `stale` for a verifiably dead PID, `missing` without a marker, and `unknown` for malformed or unverifiable evidence. They report every issue independently in deterministic order. |
| Project lock and identity | The project lock is short-lived coordinator evidence. It does not project a process state onto every retained issue. Status and inventory never acquire, create, or write either lock or owner marker. Configured project identity verification remains mandatory. If a source is unavailable, the whole report continues and reports `unknown`, `missing`, or `mismatch` for that source. |

## Worker protocol and publication

### Systemd wrapper refresh contract

The wrapper refreshes one verified worktree. It takes one advisory lock per
worktree. A refresh failure blocks UV. It checks the worktree before it starts
the worker. It gives the worker its approved time. If time runs out, systemd
stops its child tasks. The worker reports what it did. The runner checks that
report before it changes state.

| Wrapper input | Required handling |
| --- | --- |
| Tools | Record absolute paths for Git, `glab`, `jq`, `flock`, UV, and the chosen agent. |
| Identity | Check the configured GitLab project and checkout identity. |
| Lock | Take one advisory lock per worktree. |
| Git setup | Disable repository hooks for each refresh command. Allow only the strict checkout-local Git allowlist. Reject target filter attributes. Keep submodule recursion off. |
| Credentials | Use the shared Git authentication contract. Do not use ambient helpers or saved credentials. |

| Refresh control | Action and outcome |
| --- | --- |
| Preview | Use `git ls-remote`. Require `HEAD` to be the remote default-branch tip. Do not fetch or write a tracking ref. |
| Applied run | Fetch and fast-forward only that branch. A failure blocks UV. |
| First deployment | Stop the timer. Fast-forward once by hand. Preview or run a canary. Then restart it. |
| Later applied activation | Refresh itself. |

The oneshot service has a bounded outer limit. `TimeoutStartSec=190min`.

| Service control | Contract |
| --- | --- |
| Worker budget | Reserve the approved 10,800-second (180-minute) worker budget for the applied `agent-work` call. |
| Wrapper allowance | Leave 10 minutes for wrapper checkout, GitLab, publication, and reporting. |
| Housekeeping limit | Non-worker Git, GitLab, `direnv`, and bootstrap calls have a 120-second limit. |
| Service-window admission | Before each applied loop run, require 10,800 seconds for the next activation plus 120 seconds of housekeeping within the 190-minute window. The clock input defaults to `/proc/uptime`; `AGENT_WORK_UPTIME_FILE` can override it with an absolute, regular, non-symlink file. The first line must end in a newline and contain two nonnegative decimal fields separated by whitespace, each with exactly two fractional digits. An invalid input or backward reading stops with status `2`. If less time remains, print a `service_time_limit` diagnostic on stderr and exit with status `0`. Validated JSON reports remain on stdout; the ledger and worktree remain for the next timer run. |
| Applied worker limit | Give the worker the time left in its 10,800-second budget. |
| Timeout action | At the outer limit, systemd sends TERM to the full service control group. It gives TERM 30 seconds before KILL, then kills survivors. |
| Timeout evidence | The unit journal records `Result=timeout` and relevant sanitized wrapper diagnostics. The durable ledger and retained issue worktree stay in place. Normal retry and human diagnosis use that evidence. |

While the workflow owner is live, it stores one strict
`WorkerResultBuildInput` JSON object in private scratch and invokes the trusted
controller-checkout `worker-result build` helper through its private broker.
For coding work, that invocation also establishes lane authority and performs
the atomic Red checkpoint transaction. The broker returns a canonical rejection
to that same producer for one bounded correction only when controller-observed
worktree evidence has not changed. It keeps accepted results and checkpoint
evidence in controller memory. It does not write a handoff artifact. Final
messages and all model standard output, including output from a successful
worker process, are never controller state or a handoff result.

| Envelope field or check | Contract |
| --- | --- |
| Result | `red_checkpoint_ready` for the synchronous coding Red boundary, or `ready_to_publish`, `retryable`, `needs_human`, `split_required`, or `suggest_close` for a workflow-owning result. Broker acceptance adds verified checkpoint evidence before it returns `red_checkpoint_ready`. |
| Content | Include bounded worker intent, diagnostic, and next action. Specialist receipts and audit prose are discussion context only. |
| Trusted runner | Before changing state, freshly check the nonce, ledger, configured numeric project, revision, branch, source SHA, state, phase, accepted envelope, observed changed paths, and applicable controller-owned checkpoints. |
| Broker result | `AgentInvoker` accepts one request, then at most one corrected request after a canonical rejection. The broker keeps an accepted result only in controller memory. |
| Rejected request | A missing, malformed, oversized, late, deleted, raced, or extra request rejects the result. The durable diagnostic is a bounded constant that identifies the stage and category. The controller suppresses raw builder standard output and standard error. Diagnostics never retain raw exceptions, output, paths, identities, secrets, payloads, or worker data. |
| Coding final audit | Name Router, Architect, Red, Diátaxis Writer, the conditional Agent-policy Writer, Green, all applicable reviews including the initial `$license-checker`, and Finalizer. |
| License receipt | Finalizer needs a current approving license receipt for the final in-scope diff. During correction, rerun the checker only if a maintained file in its checked scope changed. A violation cannot be waived or marked not applicable. |
| Policy finding | Send it through Router to the Agent-policy Writer, not Green. |
| Content work | Keep its separate role set. |

### Live-agent capacity launch contract

Every Router launch has one fixed global requirement: `12` live slots for
coding and `5` for content. This includes terminal, normal serial,
retained-worktree retry, and systemd launches. Check before delegation, fanout,
or a worktree change.

Use a current trusted global report from the active frontend or runtime when it
is available. Admit the lane when the report meets its fixed requirement. Stop
with `needs_human` only when a current report shows fewer slots. Continue when
global capacity is unreported.

| Capacity evidence or action | Contract |
| --- | --- |
| Valid source | A current global report from the active frontend or runtime. |
| Invalid source | Repository data, issue and task text, user or provider setup, service environment values, and retained ledger or audit values do not prove live capacity. |
| Requirement | `12` total live slots for coding; `5` for content. The selected lane sets the requirement. |
| Missing report | Continue. Missing global capacity is not a shortage. |
| Reported shortage | Return `needs_human` before delegation, fanout, or a worktree change. State the lane, fixed requirement, reported total, shortfall, and an actionable frontend remedy. An eligible ordinary serial issue then enters the capacity wait below. |
| Prohibited inference | Do not request or calculate topology, widest-row, retained-object, current-use, fresh-handle, per-owner, direct-child, descendant, or owner-headroom counts. None creates another capacity gate. |
| New launch | A prior report does not authorize a new launch. Use a newer current report when one is available. |

The worker-result builder preserves its trust boundary. It derives identity,
ledger revision, state, phase, and checkpoint dependencies from the controller.
The private input supplies only bounded intent. It cannot supply
controller-derived values, role receipts, or audit authorization. Legacy ledger
records remain readable through their recorded route.
See [Configure agent
capacity](../how-to/configure-agent-capacity.md) for the operator procedure.

For a broker-bound `agent_capacity_unavailable` refusal, the controller defers
an ordinary issue only during a pre-selection `WORKFLOW` attempt whose durable
lane is `UNKNOWN`. The ordinary record must have untouched retained evidence,
an exact branch, HEAD, and source identity, no candidate paths, a clean
worktree, and no Red checkpoint. The controller records the same issue in
`RETRY_WAIT` with `retry_at` set 20 minutes after the recorded activation
time. It keeps `retry_count` unchanged, so repeated shortages do not exhaust
the three ordinary retries.
It records the elapsed refusal as `capacity-wait` timing. The controller binds
the worker's capacity result through the broker; it does not measure the
frontend's live slots itself.

When the wait is due, the same issue starts a fresh session. It checks current
capacity and delegates a fresh Architect design session. Dirty work, a saved Red
checkpoint, a changed identity, or another `needs_human` cause follows the
existing safe recovery route.

### Router launch and design

The prompt starts with `$router` and its checked skill at
`.agents/skills/router/SKILL.md`. This comes before the saved record or
untrusted issue, failed-job, and feedback data. Router owns intake, lane
choice, slot checks, writer release, fixes, and the final choice. Legacy
`$architect` receipt and advisory keys and `architect_risk_intent` keep their
protocol names.

Router checks intake and slots, then asks one read-only `$architect` for a
plan through its checked skill. Architect starts one Sol base plan and two
Luna plans at once. It keeps the Sol design and uses Luna to find gaps. Once
the authors stop, three read-only peers check the plan once. Architect fixes
clear findings. It sends Router the final plan, findings, and open blockers.
Raw drafts stay with Architect. All design children stop before a writer
starts. Safety or scope gaps block writer release.

The prompt points to the Architect skill; it does not copy its steps. Each
fresh session uses that design gate, including both coding segments and
resumed work.

### Intake sizing contract

At the start of a new coarse `workflow` session, the trusted wrapper activates
Router. Before lane selection, design, delegation, Red, documentation work,
or a worktree change, Router makes one semantic sizing decision.

The table gives the allowed results. It sets the next step. Use it before work
starts.

| Intake result | Rule |
| --- | --- |
| Suitable issue | It has one delivery outcome that can be reviewed and merged on its own. It can finish each applicable workflow stage within the effective bounded worker budget. |
| Sizing decision | It uses behavior, dependency and order, and workflow breadth. Numeric limits, such as line count, do not decide suitability. |
| Suitable work | It uses the existing coding or content workflow. |
| Split-required work | The issue is oversized or has more than one separately deliverable capability. |

A split-required issue returns one protocol-version-2 envelope:

| Field | Value |
| --- | --- |
| `result_kind` | `split_required` |
| `phase_status` | `complete` |
| `completed_phase` | `workflow` |
| `workflow_lane` | `unknown` |
| `split_plan` | 2..25 ordered slices with stable ASCII keys and lower-ordinal dependencies |

| Split route state | Required action or outcome |
| --- | --- |
| Worker result | It needs no receipts, merge-request draft, or error code. The worker makes no worktree or GitLab change, creates no child issues, and runs no proposed slice. |
| Valid split result | The controller handles it in its executing segment. For each proposal, it finds the exact durable child marker and reuses that child. Otherwise, it creates a child with a full description that links to the parent and its dependencies. |
| All children exist | The controller posts one parent note and closes the parent. It saves no decomposition lifecycle state, plan, progress ledger, checkpoint, or special queue state. |
| Retry | It reruns the whole workflow and exact-marker searches. A partly finished attempt therefore creates no duplicate children. |
| Child eligibility | Children enter ordinary eligibility. Each uses the normal workflow and merge-request path after its lower-ordinal dependencies close. |
| Versioning | `LedgerRecord.schema_version` and `WorkerResultEnvelope.protocol_version` are `2` for new persisted records and every fresh worker envelope, including `split_required`. The reader accepts only legacy v1 ledger records. Worker output is always v2. |

### Split prerequisite-label projection

Only validated split markers and live referenced-issue state control a child's
dependency gate. The runner derives one marker-based proof from a complete live
issue snapshot and shares it across selection, recovery, and label projection.
It projects the open needs to operators with non-scoped
`agent-prereq_<IID>` labels. A projection label never changes eligibility.

The table shows what labels mean. Labels show open needs. They do not set the
gate. The gate uses only trusted facts. A label gives no extra power. These
rules keep the view clear.

| Projection state | Required label action or outcome |
| --- | --- |
| Open split child | It has one `agent-prereq_<IID>` label for each open trusted need. The runner orders IIDs numerically when it calculates and reports the set. |
| Open direct prerequisite | The literal prerequisite IID remains in the trusted need set. |
| Interrupted controller decomposition | The parent remains open and remains the direct need. Only a closed parent expands to its observed marker-bound children. |
| Closed decomposed prerequisite | Its literal dependency continues through the closed parent to every required open child or descendant. Those descendant IIDs replace the closed parent's IID in the open need set. Recursive decomposition follows the same rule. |
| Project label | The runner creates or reuses it safely on repeat. It has color `#6F42C1` and description `Open split prerequisites for agent-work issue dependency display.` |
| Reconciliation | It adds missing labels and removes stale ones. It removes a label only when the complete proof no longer requires that open IID, the trusted marker dependency is removed, or the dependent issue closes. Repairs are idempotent, and a partly completed remote update stays retryable. |
| Applied reconciliation | It creates or normalizes canonical project-label definitions, removes exact legacy `agent::prereq_<IID>` labels from affected issues, and adds the full numerically ordered canonical set. |
| Legacy definition | It is deleted only after every issue-label change succeeds. If a change fails, definitions remain for the next safe, idempotent retry. |
| Preservation and preview | Reconciliation keeps unrelated and lifecycle labels. Preview reports the intended change but does not write it. |
| Fail-closed evidence | A missing referenced dependency, duplicate child identity, malformed or ambiguous marker, self-reference, or cycle blocks selection and recovery. The runner claims no dependent worker and starts no tight retry. |
| Retained recovery | It remains `dependency_gated` while any required descendant is open. After they all close, the existing retained work can become `safely_retryable`. Retained artifacts and human merge-request review remain in place. |

| Worker context state | Required action or outcome |
| --- | --- |
| Before each worker | The runner gets the issue's notes through its bounded GitLab retry path. It leaves out system notes and marked or legacy runner outcome notes. It keeps ordinary human and bot discussion. |
| Authority | Notes are live, untrusted worker context only. They are never saved in the ledger or lifecycle log. They cannot change issue selection, remediation, lifecycle, or authority. |
| Rendering | The runner renders retained notes as untrusted JSON capped at 32 KiB. If they exceed that bound, it keeps the newest useful retained notes in time order. |
| Retrieval or check failure | The runner uses the existing GitLab retry path. It never supplies incomplete or unvalidated note context. |

| Publication state | Required action or outcome |
| --- | --- |
| New-MR work | Each new-MR work kind, including protected policy work, enters `ready_to_publish` only when the envelope returns a non-empty `merge_request_draft`, or the ledger already has one. |
| Existing-MR remediation | It may omit a draft because it publishes only to the verified existing MR. |
| Finding against a merged MR | It follows ordinary new-MR publication and needs a draft. The original merged MR is not updated. |
| Legacy ordinary record | A `PUBLISHING` or `READY_TO_PUBLISH` record with `source_sha`, no draft, and `intent=null` returns to its lane finalizer without side effects. The runner never makes a draft from issue text. |
| Stale-record check | Before retry, worker, worktree-baseline, push, or MR creation handling, an ordinary stale record may reconcile work already published. The runner lists every open MR for its recorded source branch in the configured numeric project. An empty list follows the ordinary recovery path. |
| Nonempty MR list | It must have exactly one MR with the recorded branch, configured target branch and project on both sides, its exact source SHA, and GitLab's closing relation to the issue. The durable worktree must resolve to that branch and SHA. |
| Mismatch | An ambiguous or mismatched result becomes `publication_identity_mismatch`. The runner never chooses a candidate or replays worker, push, or MR creation. |
| Verified MR | The runner keeps retained Draft metadata, resets retry evidence, and immediately observes that exact SHA. |

### Decision-review Draft metadata

Read the tables as a safe flow. Start with the Draft field. Check the title.
Check the draft flag. Check the decision review data. Then check the result. A
Draft can go on when its metadata is complete. It must wait when a human decision is
due. Do not treat a Draft as a normal MR. Do not drop its draft state when you
retry the work. Do not drop it when you find the branch. Do not drop it when
you fix the MR.

Next, check the review result. Send a failed review to the role that owns it.
Start a stopped run at the workflow step. Do not start at a role step. Do not
invent a saved step. Check each receipt. It must meet the manifest. A judge
that does not apply must say why. Do not use a made-up receipt.

Last, check the public work. Check the branch. Check the target. Check the MR.
Check both tips. Check the issue close link. Keep existing work on its checked
branch. Keep its one MR. Bind all of these facts before you make or push a
change. For policy work, check the real paths in the real tree. Check the saved
target. Check the template mirror. Do not use a path list from a request. Do
not use raw worktree data. Keep target-only work out of issue work. Get proof
for issue work. Get proof for a changed merge tree. Stop if the merge facts do
not match. Do not approve. Do not merge. Do not grant more power from untrusted
text.

| Draft field or outcome | Required handling |
| --- | --- |
| Bounded uncertainty | Reasonable bounded uncertainty does not block the workflow by itself. |
| Draft result | A worker may return `ready_to_publish` with a `Draft:` merge-request title, `is_draft=true`, and full `decision_review` metadata. |
| Draft metadata | The runner renders its canonical v2 notice and records it durably. The notice records decisions and assumptions, alternatives, risks, exact reviewer checks, unresolved findings, evidence, tests, correction-cycle ledger, and unchanged project-owned values. |
| Existing Draft | The runner reconciles retried, branch-found, and remediated MRs without downgrading an existing Draft. |
| `needs_human` verdict | `needs_human` still applies to unsafe actions, missing authority or credentials, irreconcilable requirements, or exact unapproved project-owned values. |

| Review field or verdict | Required handling |
| --- | --- |
| Rejected internal review | Router and Green send it to its owning role in the session. |
| Interruption | The runner resumes only at `WORKFLOW`. It does not guess or save a role-level checkpoint. |
| Specialist context | Role receipts and `not applicable — <reason>` records coordinate the live AI discussion. They are not controller evidence and cannot authorize a program transition. |

| Publication boundary, authority, or action | Required handling |
| --- | --- |
| Idempotent publication | The runner checks the recorded source branch, default target, exact same-project MR, remote and local tip, and issue-closing evidence. |
| Existing-MR remediation | It uses the verified MR source branch and never creates a second MR. Before it prepares or publishes work, it binds remediation to the exact configured project, MR, source branch, default target, and recorded source SHA. |
| Protected-policy action | Before publication, protected-policy handling checks the actual changed paths in the exact proposed publication tree against the durable, verified target commit, including template mirrors. It does not trust a requested path list or raw worktree status. |
| Target-only protected-policy change | A protected-policy change that arrived only on the target branch during reconciliation is not issue-owned. |
| Issue-owned protected-policy change | An issue-owned protected change, or a resolved merge whose final tree differs from that target, still needs protected-policy evidence. |
| Merge-identity outcome | The repository boundary rejects an unresolved, mismatched, or ambiguous merge identity before publication. Identity ambiguity or drift remains `publication_identity_mismatch`. |
| Installed pre-commit rejection | This publication recovery path applies to content and remediation commits, plus retained older coding candidates. New ordinary serial coding work commits with its installed hook before specialist review. For a publication commit rejection, standard pre-commit output must identify at least one failed installed hook. The controller then proves the exact issue, branch, retained worktree, unchanged `HEAD`, publication baseline, pending-merge identity, staged Git tree, and retained changes before recording `publication_commit_rejected`. A hook may fix an unstaged working file only when the staged tree remains exact. The controller records an immutable durable correction identity that is independent of the current `error_code` and transition intent. It binds the exact worktree, branch, `HEAD`, merge state, read-only staged-index fingerprint, staged and unstaged paths, selected workflow lane, and the exact coding Red checkpoint. Content work binds no Red checkpoint. Worker exit, worker-protocol, and retry failures retain this identity. Each controller-authored snapshot is immutable; after an authorized worker attempt the controller may replace it only with the next fully observed exact snapshot. The controller clears the intent, `publication_sha`, old merge request draft, completed worker result, historical receipts, and final audit, then returns the record to `WORKFLOW` for fresh correction. Coding must load the exact retained Red checkpoint. Both lanes must return fresh bounded worker intent, and the controller verifies their applicable program-owned evidence. Both lanes must produce a fresh merge request draft; an unknown lane, or missing or mismatched coding Red evidence, blocks before worker invocation. The identity is cleared only after freshly validated corrected evidence supersedes it or normal publication succeeds. The rejection route does not push, create or update a merge request, or project a blocked state. Signing, identity, configuration, lock, file-system, object-database, and unknown commit failures fail closed. |
| Legacy identity-mismatch recovery | A reviewed retry may use this one-time route only for an old `publication_identity_mismatch` dirty publication candidate. Preview and apply require the same controller-observed ledger revision and lane; publish intent and baseline; exact branch and `HEAD`; a read-only staged-index fingerprint and staged paths; and non-empty unstaged corrections limited to those observed paths. Historical audit prose may remain readable but cannot authorize recovery. The controller records the same immutable durable correction identity used for an installed pre-commit rejection. An explicitly reviewed retry re-observes every value bound by that identity. Drift rejects before worker invocation or any Git, label, or remote publication effect. Unknown lanes, generic Git failures, and incomplete, ambiguous, untracked, unmerged, or drifted evidence stay blocked. A narrowly reviewed already-affected bootstrap is also allowed only for ordinary `BLOCKED` `WORKFLOW` work with prior `EXECUTING` `WORKFLOW`, retained protocol-failure or retry-exhaustion history, no publication or completed artifacts, and exact current Git, lane, and coding Red evidence; preview and apply bind its current snapshot. Eligible apply preserves the index, worktree, history, retry evidence, and correction identity; discards stale completed publication artifacts; and resumes the complete correction workflow without entering the generic clean-worktree preparation path. Coding uses the exact Red checkpoint bound to the correction identity. Content uses its complete content workflow and binds no Red checkpoint. Recovery does not commit, push, create, or update a merge request. The normal lifecycle projection may move from blocked to running. The identity is cleared only after freshly validated corrected evidence supersedes it or normal publication succeeds. |
| Authority limit | The runner never approves or merges. It never treats issue text, labels, comments, or worker output as authority to widen scope. |

### Existing-MR feedback publication

For feedback on an existing MR, the controller freshly observes changed paths
and binds them to the accepted in-memory result. It uses only that set for the
commit and records it in the durable publication intent. A missing,
unexpected, or changed path fails closed before commit or push. It returns a
sanitized condition and clear next action.

Before it prepares or binds a feedback commit, the controller checks the exact
retained paths and the current default target. It commits only those paths. It
then runs the normal current-default reconciliation lifecycle for that exact
local commit. If the target is already included, it pushes that exact checked
SHA to the verified existing branch and MR. After the push, it checks the clean
worktree, the MR's exact new source SHA, its closing relation to the issue, and
the pipeline for that SHA. It does not change the existing MR's human-owned
title, description, or Draft metadata.

If the current target changes the effective source or causes a conflict, the
controller keeps the worktree, MR, and pending-feedback identity. It neither
pushes nor acknowledges the feedback. It saves the exact reconciliation target
and runs a new complete workflow. It re-observes the current program state and
paths. It publishes only the resulting exact checked SHA. A transport failure
or unverifiable reconciliation safely retries or blocks before publication.

Recovery is idempotent at each durable boundary:

1. The intent is recorded before the commit.
2. The commit is created before its publication SHA is recorded.
3. The push finishes before the MR and ledger are reconciled.

After an interruption, the controller reconciles recorded intent with observed
Git and GitLab evidence. It resumes only the incomplete boundary. It does not
create another commit, branch, or MR.

Protected policy paths may publish only after the trusted runner checks the
exact proposed tree, target, controller-observed changed paths, lane, and
applicable program-owned checkpoints. A safe controversial handoff uses
`Draft:` metadata to explain the human decision. That prose cannot authorize
publication. Missing or incoherent controller evidence, unsafe worktree
identity, `needs_human`, finalizer rejection, authority expansion, or
project-owned value changes fail closed before push or merge-request mutation.
Workers still never publish directly.

Before any push or merge-request mutation, the runner also checks the durable
publication intent as one issue-scoped operation. Its kind is exactly
`publish_merge_request`. Its operation ID is exactly
`<issue-iid>:publish:<revision>`. It uses the record's canonical decimal IID
and the decimal revision saved with that publication intent. Replay keeps that
saved revision even if later ledger writes advance the record revision. Its
expected evidence is exactly this two-key object:
`{"branch": "<record-source-branch>", "baseline_sha": "<record-source-sha>"}`.
Missing, extra, or different keys and values are not valid evidence. A mismatch
blocks the issue before publication mutation. It preserves the original intent
with publication-identity blocker evidence for human review. It is not retried
as a new publication. A fully checked, incomplete idempotent operation remains
replayable through its recorded intent.

An immutable MR identity mismatch blocks at once. The configured project,
source branch, target branch, or recorded source SHA is not retried as an
eventual-consistency condition. In contrast, GitLab may create the otherwise
verified MR before it shows the issue-closing relation. The runner then binds
the exact MR IID and keeps its publication intent while it rereads that IID in
the same activation. A missing projection is an observation wait, not a
transport retry or identity mismatch.

The runner keeps a checked MR draft through repair and retry. Observation
rereads and rechecks the exact open MR, project identities, source branch,
target, and source SHA before it trusts pipeline evidence. A mismatch blocks
with an actionable error. A conflict enters durable repair on the same branch.

### Post-create publication reconciliation

After it creates an MR, the controller treats GitLab's first stale projections
as bounded checks in the same activation. The checks use the durable exact MR
IID, configured project on both sides of the MR, source and target branches,
issue-closing relation, and publication SHA. It checks the closing relation
once, then retries at most three times at once. That is four closing-relation
calls in total. Each retry rereads and rechecks the exact MR IID. Retries end
when the closing relation appears. This reconciliation only reads and records
the new MR. It never recreates its branch, commit, or MR.

If all three retries lack the closing relation, the controller keeps the bound
MR in `Observing` at `PipelineWait`. It does not set `retry_at` or look up a
pipeline. That activation hands off as `activation_complete/pipeline_wait`.
A later activation checks the relation again. Once the relation appears, absent or nonterminal pipeline evidence for that exact SHA leaves the
durable record in `Observing` at `PipelineWait`. For a pending pipeline, the
new observer hands off as `activation_complete/pipeline_wait` without polling.
A later activation may poll the retained observer. Terminal failed evidence
starts repair. Only a true GitLab transport or API
failure uses safe, bounded backoff. Its durable check evidence is sanitized. An
immutable identity mismatch blocks. It does not use an eventual-consistency
retry. The bounded post-create publication and closing reconciliation does not
schedule a fixed 20-minute retry. The activation deadline and outer timeout can
still hand off an active observation.

### Human merge during observation

If a human merges while the controller observes the exact source pipeline, the
controller may go straight to `COMPLETE`. It rereads fresh evidence. All of
these must agree: the configured project. Recorded MR IID. Source and target
project identities and branches. Recorded exact source SHA; MR closing relation;
closed issue. The exact source-SHA pipeline must report `success`, with at
least one current required job and every such job successful for that SHA. Unrelated, mismatched, ambiguous, missing, pending, or failed
evidence cannot complete the record.

For an open issue, the controller repairs the `OBSERVING` lifecycle-label
projection before it resumes observation. For verified completion, it writes
the durable `COMPLETE` record before clearing lifecycle labels. This makes
restarts idempotent. A label-clear failure leaves a complete record and stale
label for the next activation to remove. A `BLOCKED` or `RETRY_WAIT` wrapper
keeps verified publication identity. Later reconciliation of a closed issue can
still check this exact completion path.

## Retry, repair deferment, exits, and JSON

The first workflow attempt and at most three ordinary retries are bounded.
`retry-wait` records its deadline. Exhaustion becomes `agent::blocked`, never
`agent::failed`. Labels never make a blocked item eligible. A valid exact retry
plan lets `loop retry --issue-iid IID --apply --plan-file PATH` resume that IID
from its retained worktree and durable record without a new human unblocking
approval. It does not select a replacement issue or discard retained recovery
evidence.

A clean pre-selection capacity refusal has its own `RETRY_WAIT` deadline. It
does not increase `retry_count`. A due wait starts a fresh session for the
same issue.

An applied activation may resume one clean, untouched ordinary blocked record
after proof checks under the project and issue locks. The same failure sets a
20-minute cooldown. Dirty or unclear work and records with publication
evidence still need reviewed recovery. Exact worker protocol faults and
exhausted worker exits or timeouts can qualify before a Red checkpoint.
Cause text and a blocked label alone never grant retry authority.

Worker exits, timeouts, malformed worker protocol, and an explicit `retryable`
worker result are due at once. Legacy records may also retain an invalid-receipt
condition; its receipt prose is not authority. These conditions continue in the
same activation. Transient GitLab or transport failures keep retry timing. The XDG
`agent-work.log` records redacted outcomes. `--debug` adds local diagnostics
without credentials or transcripts. For a nonzero worker exit, optional
`items[].worker_exit` keeps its return code and a stderr-derived diagnostic.
The diagnostic is one safe line of at most 2048 UTF-8 bytes. It excludes
prompts, issue content, stdout, credentials, terminal controls, and unbounded
output. Retry exhaustion keeps it. A checked successful worker result clears
it.

| Outcome or condition | Durable handling |
| --- | --- |
| First workflow attempt plus at most three retries | Bounded; `retry-wait` records its deadline. Exhaustion becomes `agent::blocked`, never `agent::failed`. |
| Clean, broker-bound pre-selection `agent_capacity_unavailable` | Keep the same issue in `RETRY_WAIT` until its deadline, without spending an ordinary retry. Recheck current capacity and the current Architect design gate when due. |
| Worker exit, timeout, malformed worker protocol, legacy invalid-receipt condition, or explicit `retryable` result | Immediately due and continued in the same activation. |
| Transient GitLab or transport failure | Keeps retry timing. |
| Repeated `worker_protocol_invalid`, or legacy `worker_receipt_invalid`, with the same error code and diagnostic in one coarse phase | Blocks immediately without another retry; retain the worktree, phase, and safe cause. |
| Changed protocol code, diagnostic, or phase | Changed evidence; use the ordinary bounded retry path. |
| Exact-pipeline repair failure | Record `automatic_repair_count` and a 20-minute `automatic_repair_at`; after three failed attempts, quarantine or defer for reviewed recovery. |

For `worker_protocol_invalid` and legacy `worker_receipt_invalid`, the runner
compares the canonical error code and bounded, stable, redacted contract-field
or reason diagnostic with the latest automatic condition in the same coarse
phase. The first failure schedules its immediate retry. If a later automatic
attempt has the same pair in that phase, it made no progress. The runner blocks
the issue at once without using another retry. It keeps the worktree, phase, and
safe cause. It publishes the usual blocked projection and comment. A changed
code, diagnostic, or phase is changed evidence and follows the usual bounded
retry path. This does not change retries for non-protocol transient failures.
An applied activation may resume clean pre-checkpoint work after exact proof
and a repeated-failure cooldown. Otherwise, correct the condition and apply an
exact `loop retry` plan for that IID. Its preview must pass the shared recovery
checks and the `safely_retryable` inventory gate before the command resets the
automatic retry budget.

| Protocol retry evidence | Handling |
| --- | --- |
| Retained attempt | The condition history records the bounded origin and reason, its attempt number, and the controller-observed completion time. It does not retain worker output or other untrusted text. |
| Eligible replay | A due retry reuses only a clean retained checkout, or the exact retained Red checkpoint when its tests and tree still match. |
| Unsafe retained checkout | A dirty checkout without exact Red-only proof, or a changed or unclear checkout, blocks the item and preserves its checkout. Use reviewed recovery; automatic replay does not modify it. |

Automatic exact-pipeline repair has its own budget: at most three repair
workflows for one exact retained owner. After each failed attempt, the ledger
records `automatic_repair_count` and a 20-minute `automatic_repair_at` deadline.
These are separate from ordinary `retry_count` and `retry_at`. A later
activation can consider a due item again. After a third failed attempt, the
item is quarantined or deferred and needs reviewed recovery. This is separate
from ordinary `retry-wait` and its workflow retry count. It does not stop unrelated eligible work.
Only reviewed recovery is exceptional. It needs one exact IID and current
corroborated evidence. It does not use the automatic repair budget. Untrusted
issue or merge-request content cannot trigger or set it. Migration preview
follows every page of lifecycle projections. Before mutation, apply checks all
actions against unchanged evidence, project identity, and absent ledger records.
It rejects the whole plan on mismatch. After a partial GitLab label-projection
failure, apply saves a `migrate_projection` intent, safely replays that exact
projection, and clears the intent only after it checks the label projection.

Service-environment migration reports `current`, `stale`, `verified`,
`rolled-back`, or `correction-needed` without changing loop state. Its redacted
report contains only the check or migration outcome. It never contains
credential values or service-environment contents.

| Exit | Meaning |
| --- | --- |
| `0` | Idle, preview, scheduled retry, blocked, or awaiting-human was safely recorded. |
| `1` | The runner could not safely reconcile or record durable state. |
| `2` | Invalid CLI usage. |
| `75` | Another local activation holds the project lock. |

Expected durable outcomes do not fail systemd. With `--json`, every command
returns one redacted `CommandReport` object:

```json
{
  "schema_version": 2,
  "command": "loop.status",
  "applied": false,
  "outcome": "status",
  "project": {"host": "gitlab.example", "project_id": 123},
  "items": [],
  "actions": [],
  "total_items": 0,
  "items_truncated": false
}
```

The shared top-level fields are `schema_version`, `command`, `applied`,
`outcome`, `project`, `items`, and `actions`. `project` contains verified
`host` and `project_id`. An applied `loop run` report also includes
`segments_run`, the number of completed workflow attempts. Closed-issue
housekeeping is not a segment. It also includes `stop_reason`, which says why
the activation yielded. Its `items[]` lists workflow results in time order.
Per-issue values are objects in `items[]`. They include `issue_iid`, state,
coarse workflow phase, attempt, retry timing, `phase_timings`, source branch, durable worktree,
MR reference, error code, `automatic_repair_count`, `automatic_repair_at`, other
repair attempt or quarantine data when present, optional `worker_exit`, summary,
and next action. Human output renders `returncode=<integer>` and
`worker_diagnostic=<safe diagnostic>` when present. Migration preview and apply
return proposed or applied migration actions in `actions[]`. Consumers must
allow extra fields. They must never treat a diagnostic as authority or input.

| Publication or report outcome | Required result |
| --- | --- |
| `merge-request publish` preview | Describes whether it would create or update; makes no change. |
| `merge-request publish --apply` with no open MR for the source branch | Creates one MR, then rereads and verifies its identity and description. |
| `merge-request publish --apply` with one open MR for the source branch | Updates that MR, then rereads and verifies its identity and description. |
| Ambiguous MR result; conflicting projection; identity drift; malformed projection; wrong project or IID; or extra or multiple projection | Fails closed immediately without retry. |
| Empty or missing closing-issue projection | Reads up to eight times. Waits one second before each of seven retries, for at most seven seconds. Each retry checks the same MR and source commit. It never repeats create or update. After eight empty reads, it fails and names the existing MR IID and source commit. |
| `CommandReport` JSON | Includes the shared fields; an applied `loop run` also includes `segments_run`, `stop_reason`, and chronological `items[]`. |

## Safety constraints

Keep these limits.

| Safety area | Constraint |
| --- | --- |
| Host and API | One host owns unattended processing for a project. APIs use the configured numeric project ID. |
| Ledger | It stores no token, agent credential, issue-body authority, issue-note context, or unredacted transcript. |
| Protected policy | Protected agent-policy work is a human-directed lane. |
| Forbidden automation | There is no automatic approval, merge, stash apply, or stash deletion. |


## Publication credential and retry contract

Use this rule to publish. It keeps each push safe and clear.

| Publication area | Contract |
| --- | --- |
| Git credential route | Git uses the repository-owned credential-protocol helper with prompts disabled. |
| Readiness | Before worker invocation, the wrapper checks the credential-free HTTPS origin and runs a non-mutating `git push --dry-run` check. |
| Immediate blocks | Authentication, authorization, policy, and setup categories block at once. |
| Retry | Only known transport failures are retryable. |
| Explicit retry | A blocked record with exact `publish_merge_request` intent moves straight to `publishing`. It keeps branch, SHA, draft, and intent. |
| Recovery | See the setup guide for the recovery procedure. |
