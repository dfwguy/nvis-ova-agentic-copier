---
title: Serial GitLab agent-loop reference
type: reference
summary: Look up loop commands, states, data, and limits.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-23
related:
  - path: ../how-to/configure-agent-capacity.md
    why: Explains how to prepare capacity evidence before a worker launch.
  - path: ../how-to/validate-template-changes.md
    why: Shows how to publish a verified template change for review.
  - path: ../how-to/setup-serial-gitlab-agent-loop.md
    why: Configures, recovers, and migrates the loop.
  - path: ../how-to/resolve-factory-issues.md
    why: Applies one reviewed decision to exceptional retained issue evidence.
  - path: ../explanation/serial-gitlab-agent-loop.md
    why: Explains the architecture behind this contract.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains the recoverable state machine in detail.
  - path: ../explanation/agentic-workflow.md
    why: Explains the Judge role that owns validation sessions.
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
agent-work loop run [--apply] [--max-segments COUNT]
                    [--max-activation-seconds SECONDS]
                    [--json] [--debug]
agent-work loop status [--issue-iid IID] [--apply] [--json] [--debug]
agent-work loop retry --issue-iid IID [--apply] [--json] [--debug]
agent-work loop canary [--apply --plan-file PATH] [--json] [--debug]
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
                               [--handoff-file PATH --handoff-nonce NONCE]
                               [--json] [--apply] [--debug]
agent-work red-checkpoint create --issue-iid IID --evidence-file PATH
                                 [--json] [--apply] [--debug]
agent-work merge-request publish --source-branch BRANCH --target-branch BRANCH
                                 --title TEXT --problem TEXT --outcome TEXT
                                 --validation TEXT [--validation TEXT ...]
                                 [--impact TEXT] [--human-action TEXT]
                                 [--tracking TEXT] [--issue-iid IID]
                                 [--apply] [--json]
agent-work issue publish --title TEXT --problem TEXT --outcome TEXT
                         --acceptance TEXT [--acceptance TEXT ...]
                         [--impact TEXT] [--additional-context TEXT]
                         [--marker TEXT] [--priority TEXT] [--severity TEXT]
                         [--label TEXT] [--issue-iid IID] [--apply] [--json] [--debug]
agent-work validation run --candidate-tree TREE [--apply] [--json]
agent-work validation status --candidate-tree TREE
                             [--wait-seconds SECONDS] [--apply] [--json]
agent-work migration preview [--json] [--debug]
agent-work migration apply --plan-file PATH --apply [--json] [--debug]
agent-work upstream-finding preview --manifest-file PATH --upstream-host HOST --upstream-project-id POSITIVE_INT --upstream-credential-file PATH [--apply] [--json] [--debug]
```

| Command | Required input | Optional control or result |
| --- | --- | --- |
| `loop run` | None | `--apply` performs one activation; segment and activation-time limits bound it. |
| `loop status` | Optional exact `--issue-iid` | Read-only status; `--apply` is accepted for consistency. |
| `loop retry` | Exact `--issue-iid` | `--apply` resets one reviewed item's retry budget. |
| `loop canary` | `--plan-file PATH` when applying | Bare invocation creates the immutable plan. |
| `loop recover` | Exact `--issue-iid`; reviewed `--plan-file PATH` when applying | Segment and activation-time limits retain `loop run` controls. |
| `recovery inventory` | Optional exact `--issue-iid` | Read-only reconciliation; `--apply` changes nothing. |
| `recovery resolve` | Exact `--issue-iid`, explicit `--choice`, and `--reason` | Applying also needs the exact reviewed `--plan-file`; some choices need evidence, a replacement IID, or a target label. |
| `credential rotate` | `--environment-file PATH`, `--unit-name NAME` | `--apply` performs the bounded rotation. |
| `service-environment migrate` | `--environment-file PATH`, `--unit-name NAME` | Bare invocation previews; choose `--apply` or `--dry-run`. |
| `worker-result build` | Exact `--issue-iid`, `--input-file PATH` | Without `--apply`, previews the validated envelope. Applied handoff creation also requires `--handoff-file PATH` and `--handoff-nonce NONCE`. |
| `red-checkpoint create` | Exact `--issue-iid` and bounded `--evidence-file` | Bare invocation previews; `--apply` verifies and retains one durable Red checkpoint. |
| `merge-request publish` | Source and target branches, title, problem, outcome, and one or more validations | `--apply` creates or updates one exact MR; `--json` returns its result. |
| `issue publish` | Title, problem, outcome, and repeated acceptance criteria | `--apply` creates or updates one exact issue in the configured checkout project; `--json` returns its result. |
| `validation run` | `--candidate-tree TREE` | `--apply` starts the one hook run. |
| `validation status` | `--candidate-tree TREE` | Optional `--wait-seconds` bounds polling; it stays read-only. |
| `migration preview` | None | Returns the planned migration actions. |
| `migration apply` | Reviewed `--plan-file PATH` and `--apply` | Applies only unchanged, preflighted actions. |
| `upstream-finding preview` | Manifest, upstream host, project ID, and credential file | `--apply` is the only mutation switch. |

| `loop run` mode or condition | Behavior |
| --- | --- |
| Applied run | Runs one bounded, recoverable activation for one issue. The worker owns the full workflow session. |
| Default limits and overrides | Allows 16 workflow attempts and one 10,800-second (three-hour) work budget. `--max-segments` and `--max-activation-seconds` override those limits. Each workflow call gets the positive, ceil-rounded time left in that budget. On expiry, normal retry and durable-worktree recovery apply. |
| Before work selection | Reconciles prerequisite-label projections for split children. If it fixes projection drift, it does only that work. It does not claim a dependent issue, create a ledger record or worktree, or start a worker. A partial remote failure is retryable, and it is safe to repeat the reconciliation. |
| Without `--apply` | Reports each current and intended prerequisite-label projection but makes no writes. |
| Open factory MR awaiting review | `AGENT_WORK_FACTORY_USERNAME` is needed and must equal the authenticated GitLab service username. The loop reads bounded general comments and inline discussion replies. New human feedback wakes only that issue for existing-MR remediation. It acknowledges no-change feedback once. Code changes reuse the same MR and preserve its title, description, and closing reference. |

The tables list the commands and their use.

### `red-checkpoint create`

`red-checkpoint create` retains the failing-test boundary from TDD Red. Its
strict JSON evidence contains only `argv`, `expected_failure`, and
`test_paths`. `argv` must begin with the exact `uv run pytest` argument vector.
The other fields name the expected failure marker and declared changed test
paths for one positive issue IID.

Without `--apply`, the command checks and previews the intended action. It does
not run the test command, create a Git ref, write a checkpoint record, change
the worktree, or change GitLab. The first `--apply` reruns the recorded command
with the exact argument vector. The command must exit nonzero and emit the
expected marker. An exact replay verifies and reuses the retained checkpoint
without rerunning the command. A passing command, missing marker, changed
input, undeclared test path, or invalid or stale evidence fails closed.

After verification, apply overlays only the declared changed tests onto the
recorded base tree. It stores the complete tree under an issue-namespaced local
ref and writes a bounded checkpoint record. The record binds the project, issue,
branch, base, ref, tree OID, paths, argument vector, nonzero exit code, expected
marker, and an output digest. It never retains raw command output or a worktree
path. Green, test review, and the final audit must use that exact identity. The
ref is local recovery evidence. It is never pushed.

An interrupted retry reuses the checkpoint only when its record, ref, tree,
paths, and recorded base identity still match. A missing or changed ref, tree
or path mismatch, stale base identity, or malformed record fails closed. The
trusted recovery prompt gives a later worker the checkpoint ref, tree, and
declared paths so it can inspect the same Red boundary. Ordinary `git diff
<ref>` can inspect the Git-native snapshot.

`--json` returns the same bounded identity and outcome for automation.
`--debug` does not weaken redaction. Checkpoint validation failures remain
sanitized and never include rejected evidence values or raw test output.

### Coding workflow Red boundary

A coarse coding workflow keeps Red tests apart from code changes. The
controller sees and checks the handoff between two worker runs.

| Condition or step | Controller contract |
| --- | --- |
| Fresh coding work without a retained checkpoint | The first worker segment may change only declared test paths. It returns `red_checkpoint_ready` with `change_summary`, `next_action`, the `$architect` and `$tdd-red` receipts, and one `red_checkpoint_request` with `argv`, `expected_failure`, and `test_paths`. It does not start Green or return the full workflow receipts. |
| Durable lane selection | Before applying the trusted checkpoint transaction, the controller persists `workflow_lane: coding` in the ledger. The applied transaction therefore cannot run against an unknown or inferred lane. |
| Trusted checkpoint transaction | The controller rejects production-path changes, validates the Red-only result, applies `red-checkpoint create`, then loads the retained evidence through the trusted checkpoint boundary. The returned and loaded evidence must match exactly. |
| Later coding segment | Only after exact checkpoint verification does the controller start a new worker invocation. Its prompt includes the retained checkpoint identity. That invocation completes Green and the remaining coding workflow and returns the full coding receipt set. |
| Restart with exact evidence | When the trusted loader finds the exact checkpoint for the project, issue, source branch, and recorded base, the controller reuses it. It does not recreate the checkpoint or repeat the Red-only segment. It starts the later full workflow with the same retained identity. |
| Failed boundary | A missing or mismatched checkpoint, a passing Red command, a missing failure marker, a failed transaction, or a production change before capture blocks the item with `red_checkpoint_failed`. The durable condition distinguishes a retained-checkpoint precheck failure, checkpoint-context preparation failure, and post-worker result or checkpoint-application verification failure. It never retains raw exceptions, command output, paths, or worker data. The controller preserves the worktree and never starts the later coding segment. Repeated observation has the same stable outcome. |
| Content lane | Content work keeps its existing single coarse workflow invocation. It does not enter the Red checkpoint transaction. |

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
projection is treated as GitLab projection lag. The publisher makes at most
four projection reads: the initial read and three retries, each separated by
exactly one second. The total intentional wait is at most three seconds. It
never repeats the create or update. An exact configured-
project issue succeeds. A conflicting projection, identity drift, malformed
projection, wrong project or IID, or extra or multiple projection fails closed
immediately without retry. An empty or missing projection after the fourth
read also fails closed.

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

Without `--apply`, the command previews the title and rendered description
locally. It makes no GitLab calls or changes. With `--apply`, it uses
`<!-- agent-work-issue-publication:v1 -->` when `--marker` is absent. It
selects one exact open-marker match. More than one exact match fails closed. It
creates only when no exact open match exists. It then reads back that issue.
The read-back must match the configured project,
the returned IID, title, and rendered description. A mismatch fails closed. A
malformed response or failed read-back does too. The command does not retry
its create request after an uncertain result.

An explicit `--issue-iid` is update-only. It must select exactly one open
issue with the supplied marker. Missing, mismatched, or ambiguous evidence
fails closed before any mutation. It never creates a replacement issue.

`issue publish` is the configured-checkout boundary for policy callers. These
include findings triage and pipeline feedback. Upstream-finding and split-child
flows keep distinct deduplication and lifecycle transports. They reuse the
shared escaped- and encoded-linebreak guard. They also reuse the exact
authoritative read-back safety contract. Both checks apply to both flows. Both
are required. Both are strict. The command applies only explicitly supplied,
already verified labels. Callers, not the CLI boundary, must verify labels
before supplying `--label`. It never assigns, approves, or merges an issue.
`--json` returns the bounded publication result.

### `validation run` and `validation status`

The Judge checks one staged candidate in a durable session keyed by `TREE`.
Use `validation run --candidate-tree TREE` to preview the action. It does not
change state or start a hook without `--apply`.

With `--apply`, `validation run` first checks that `TREE` is the current staged
tree. It records the session as running. It invokes exactly one `git hook run
pre-commit` command. It then atomically records that command's terminal exit
code. A terminal-failure record can also have the existing bounded, canonical
`diagnostic` and additive structured `hook_failures` from that one invocation.
The command returns only when the hook has a terminal result.

On terminal success, the hook saves fixed `py311` and `py314` JUnit snapshots.
It saves them under the candidate tree in the state root. Each result gives its
environment, absolute path, SHA-256 digest, and `test_count`. Status reads this
saved evidence. It never reads checkout XML or runs the hook again.

Capture clears source artifacts before and after the hook. It fails closed if
cleanup fails or the full set is missing, bad, or cannot be saved. The session
then has terminal artifact-capture failure with CLI status `1`. It shows no
files and keeps the hook exit code. Preview, running, terminal-failure, and
legacy sessions also show no files. Existing sessions are not backfilled.

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
They also exclude arbitrary source, messages, carets, context, test names,
exception text, raw JUnit XML, environment values, prompts, credentials,
secrets, controls, ANSI sequences, and unrecognized data.

Inspect an unchanged tree and JUnit evidence only after terminal zero. A
terminal nonzero blocks that check. An existing record for the same candidate
tree is trusted. Another applied run returns it without rerunning the hook or
rewriting the record. This rule also covers legacy records without `diagnostic`
or structured hook failures, or without retained success artifacts. The command
does not backfill any of those fields.
Legacy records without structured hook failures expose an empty collection.

`validation status --candidate-tree TREE` is read-only. It never starts a hook.
It reports `running`, `terminal-success`, `terminal-failure`, or the distinct
terminal artifact-capture failure. With `--wait-seconds`, it polls a running
session only for that bounded interval. It stops at a terminal session. Use it
after a yielded or lost command handle. Keep querying the same `TREE`; do not
start another check. It accepts `--apply` for command consistency but remains
read-only. Human output includes the optional diagnostic. A terminal failure
with evidence also includes failed IDs and canonical lines. JSON exposes
`diagnostic` as its canonical string or `null`, plus bounded `hook_failures`
objects with `hook_id` and `diagnostic_lines`. Fields, flags, and exit codes do
not otherwise change. Preview, running, terminal-success, and legacy
terminal-failure results have no hook entries. Status reads legacy records that
omit either field. An absent `diagnostic` is `null`: no diagnostic is available.

Exit code `0` means a preview or terminal success. An applied running session
exits `1`. A terminal failure, terminal artifact-capture failure, missing or
malformed session, or operational failure also exits `1`. Invalid usage exits
`2`. JSON reports the candidate tree, applied state, action, state, terminal
state, final exit code, and optional diagnostic. Repeated apply or status
requests for the same candidate tree never rerun the combined hook or an
individual hook. They never rewrite the record.

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
| `loop retry` | Previews one recorded blocked item. With `--apply`, it resets that item's retry budget and moves it to resumable preparation after explicit human direction. |
| Immediate owner set | `PREPARING`, `EXECUTING`, `PUBLISHING`, `OBSERVING`, and `REPAIR`. |
| `BLOCKED` | Not an immediate owner. It needs action and is not runnable. |
| `RETRY_WAIT` | Queued, not a competing immediate owner when an exact IID is selected. |
| Reviewed exact retry | The durable handoff. The selected IID stays pinned through a multi-segment continuation. It moves one chosen `BLOCKED` IID to `PREPARING`, or keeps `PUBLISHING` for a publication replay, without making untouched blocked or queued-retry records compete. |
| Another immediate owner | Retry fails closed. More than one true immediate owner needs reconciliation. |

| Recovery or checkpoint action | Contract |
| --- | --- |
| `loop canary` | An operational checkpoint, not an issue run. Bare invocation creates a redacted, immutable plan. The plan verifies public service and project identity, durable recovery evidence, and current pipeline evidence. It names the expected checkpoint and next action. It never selects an eligible issue, claims work, changes a lifecycle label, creates a ledger record or worktree, or invokes a worker. |
| `loop canary --apply --plan-file PATH` | After review, rechecks the plan's exact evidence while holding the project lock before taking its bounded checkpoint action. Changed identity or evidence needs a new preview. |
| `loop recover --issue-iid IID` | Targets one interrupted, ambiguous, or otherwise unsafe retained item. Bare invocation creates a redacted recovery plan without changing anything. |
| Exact failed current-source MR pipeline with one uniquely corroborated retained owner | Does not need `loop recover`: an applied ordinary loop activation moves that owner directly to `repair`. |
| `loop recover` apply | Needs both `--apply` and the reviewed `--plan-file`. Before changing anything, it rechecks the configured project, default branch, authenticated username, ledger revision and state, lifecycle label, fixed branch, retained-worktree HEAD and cleanliness, merge-request project/source/target/source-SHA identity, and the current exact-SHA pipeline. It cannot advance another IID. |
| `loop recover` limits | `--max-segments` and `--max-activation-seconds` retain `loop run`'s bounded activation controls. |
| Plans | Bind the command and action to public service identity, configured project and default branch, ledger revision, and the exact target, issue, repository, merge-request, and pipeline evidence the action needs. They never contain secrets. Apply fails closed with the safe next action when the plan is stale, tampered, for another identity, or no longer matches evidence. |

### `recovery inventory` and `recovery resolve`

`recovery inventory` is read-only. It never claims fresh work. `--apply` is
accepted for consistent invocation and changes nothing.

| Inventory area | Contract |
| --- | --- |
| Corroborated evidence | Lifecycle-projected GitLab issues; local ledger; each retained issue's passive advisory owner marker; fixed branch and retained worktree; merge request and exact-source pipeline; default-branch pipeline; configured project and credential identity; and controller, service, and short-lived project-lock evidence. |
| JSON and human aggregates | `total`, `open`, `closed`, `active`, `blocked`, `dependency_gated`, `human_action`, and `no_action`. |
| Detailed candidate | Per-issue owner state; active state; `human_action_required`; bounded blocked cause; dependency explanation naming open trusted prerequisites; current and intended prerequisite-label projections; exact next action. |
| Classification | Legitimately waiting, dependency-gated, safely retryable, superseded or duplicate, closure candidate, or needing a specific human decision. |
| Report order | Failed default-branch and open-merge-request pipelines; retained issues by deterministic owner-evidence and IID ordering; safely retryable blocked items; dependency reconciliation; awaiting-human items; fresh ordinary work. |
| Fresh work | Reports it only. Never claims it. When trusted split prerequisites clear, recommends an explicit retry rather than starting a child item. |
| `closed_no_action` | Takes precedence for a closed recordless split issue and a closed `COMPLETE` record. These terminal states need no action even when retained worktree, head, merge-request, or pipeline evidence drifted. |
| Closed records | Closed nonterminal records need human review. An untargeted inventory collapses settled closed-item detail but keeps aggregate counts. `--issue-iid IID` returns that item's exact evidence. |

`recovery resolve` plans one issue. It needs `--apply` and the exact
`--plan-file` to apply a resolution.

| Resolution action or control | Contract |
| --- | --- |
| Plan | Creates a reviewed, evidence-bound plan for one issue by default. |
| Apply | Holds the project lock, rechecks plan evidence, rejects drift, and returns a redacted before-and-after receipt. |
| Plan choice | Exactly one explicit target-issue choice: `retry`, `close`, `reopen`, `relabel`, or `supersede`. |
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

`worker-result build` validates a captured `WorkerResultBuildInput` object and
builds one worker result.

```text
<agent-work-result>{...}</agent-work-result>
```

| Worker-result action, field, or outcome | Contract |
| --- | --- |
| Input and evidence | Reads one strict `WorkerResultBuildInput` JSON object, captured from a supported worker's final message, with the retained ledger and worktree for the supplied issue. The object is untrusted builder input. |
| Derived fields and checks | Derives protocol-v2 identity, state, completed phase, and duplicated audit fields. Uses the trusted controller and receipt-coherence rules from a workflow transition. |
| Preview | Without `--apply`, validates and emits the result but makes no change. It does not write a handoff artifact. |
| Successful preview output | Has exactly one compact delimited result. |
| Standard error | Receives human status and sanitized failures. |
| Preview without `--json` | Success emits the delimited worker envelope. |
| `--json` | Preview emits the same compact envelope without delimiters. Applied mode emits only the bounded `action`, `applied`, and `issue_iid` receipt; it never emits the protected envelope. JSON does not relax validation. |
| `--debug` | Adds local sanitized diagnostics without credentials, environment values, worker prose, prompts, or transcripts. |
| Applied handoff | Trusted `AgentInvoker` invokes its controller-owned helper with `--apply`, controller-supplied issue, input, handoff, and nonce values. It rejects a helper inside the worker checkout. After strict validation, it atomically writes one artifact to the controller-supplied `--handoff-file`. A worker does not invoke this helper. Applied success does not emit the envelope on standard output. |
| Temporary transport | The controller creates the temporary input and handoff files below its state root, outside the retained worker checkout. It attempts to clear both before launch and after every capture outcome. Any cleanup failure rejects the capture. |
| Builder time bound | The controller gives the builder only the worker time still remaining. The builder cannot extend the worker budget. If no time remains, the capture identifies builder budget exhaustion instead of treating it as a builder validation rejection. |
| Artifact identity | The artifact binds its schema version and nonce to the project, issue, ledger revision, source branch, source SHA, sorted changed paths observed when it is built, and validated envelope. |
| Controller verification | After the worker returns, the controller observes the final changed paths and accepts only the fresh artifact whose complete identity and bound paths match. Missing, malformed, stale, or mismatched artifacts fail closed. The captured input, worker prose, and standard-output envelopes cannot replace or override it. |
| Mutation boundary | Apply writes only the transient untracked handoff artifact. It never changes tracked worktree content, the ledger, Git, GitLab, or publication state. |
| Success | Exits `0`. |
| Validation or runtime failure | Exits `1`. |
| Argparse usage failure | Exits `2`. |
| Input object | One strict `WorkerResultBuildInput` JSON object containing only worker intent. The worker returns it as its final message. Unknown fields are rejected. |
| Required worker intent | `workflow_lane`, `result_kind`, `receipts`, `change_summary`, and `next_action`. |
| Optional worker intent | `merge_request_draft`, `error_code`, `redacted_summary`, `split_plan`, `red_checkpoint_request`, and bounded `audit_summary` evidence: `capacity`, `tests`, `findings`, and `corrections`. |
| Controller-derived values | Protocol version; project and issue identity; ledger revision; expected state and phase; completed phase; audit lane; role receipts; changed paths; reviewer receipt list. The input must not contain `identity`, `state`, `phase`, audit `lane`, `role_receipts`, `changed_paths`, or `reviewers`. |

| Result state or receipt | Contract |
| --- | --- |
| `phase_status` | Controller-derived. The worker does not supply it. |
| Ordinary `workflow` | Every recorded result gets `complete`, including `split_required`. |
| `post-publication` | Gets `complete` only for `ready_to_publish`. Failed, actionable, and other non-success results remain `rejected`. |
| Successful post-publication assessment | Supplies the needed complete `$pipeline-performance-judge` receipt and advances the verified exact-source handoff to `awaiting-human`. |
| Malformed, contradictory, failed, or actionable assessment | Keeps existing rejection and feedback handling; it does not advance that handoff. |
| Red-only coding handoff | `red_checkpoint_ready` is valid only for a coarse coding `workflow` without exact retained Red evidence. It has the required `change_summary` and `next_action`, complete `$architect` and `$tdd-red` receipts, and one `red_checkpoint_request`. It has no documentation or policy receipts, audit summary, merge-request draft, split plan, or error code, and it cannot advance to publication. |
| Completed ordinary workflow lane | Use `coding` or `content` with that lane's exact `WORKFLOW_RECEIPTS` set. |
| Coding receipts | `$architect`, `$tdd-red`, `$diataxis-writer`, `$agent-policy-writer`, `$tdd-green`, `$tdd-judge`, `$nonfunctional-review`, `$diataxis-judge`, `$judge-quality`, `$judge-test`, `$judge-cicd`, `$tdd-document`, `$cli-syntax-judge`, `$filesystem-structure-judge`, `$agent-policy-judge`, `$license-checker`, and `$tdd-finalizer`. All 17 are needed for coding `ready_to_publish`. |
| Content receipts | `$architect`, `$documentation-writer`, and `$documentation-finalizer`. All three are needed for completed content `ready_to_publish`. |
| Receipt format | Every receipt starts with its exact skill name and can include full multiline nested-role evidence. A conditionally inapplicable role is exactly `$skill: not applicable — reason`. Arbitrary, omitted, duplicated, or invented receipts cannot advance the workflow. |
| Coarse and legacy route | The 17-receipt coding set applies to the completed result from a new coarse `WORKFLOW`, after the Red-only handoff when one is needed. A legacy recorded phased route remains compatible and finishes its recorded route. |
| `ready_to_publish` | Completes a coherent ordinary workflow. It needs exact receipts, a successful audit (including capacity and tests for coding), and a merge-request draft when retained work needs new publication metadata. |
| Draft metadata | The draft has nonempty `title` and `description`. A controversial but safe human handoff has a title beginning `Draft:`, an agreeing draft flag, and a decision review covering decisions and assumptions, alternatives, risks, reviewer checks, unresolved findings and evidence, tests, the correction-cycle ledger, and unchanged project-owned values. Existing-MR remediation is the exception: it supplies no new publication metadata. |

The other result kinds preserve their distinct controller transitions:

| Result kind | Use and needed intent |
| --- | --- |
| `red_checkpoint_ready` | A tests-only coding handoff for controller capture. Supply the required `change_summary` and `next_action`, exactly the `$architect` and `$tdd-red` receipts, and the exact `red_checkpoint_request`. Do not supply documentation or policy receipts, `merge_request_draft`, `audit_summary`, `split_plan`, or `error_code`. The controller verifies and retains the handoff before any later coding segment. |
| `retryable` | A bounded transient outcome. Include the applicable error code and redacted summary when available. The controller schedules ordinary retained-worktree recovery. |
| `needs_human` | A blocker requiring human authority or a decision. Provide the bounded redacted summary, next action, and applicable error code. |
| `split_required` | An oversized fresh workflow. Use `workflow_lane: unknown` and a `split_plan` of 2–25 ordered, independently mergeable slices. It needs no receipts, audit, merge-request draft, or error code. |

| Failure or output rule | Contract |
| --- | --- |
| Validation and runtime failures | Retain only a canonical field-specific diagnostic of at most 2,048 bytes and a stable reason code. |
| Protocol version | Version 1 is rejected. Fresh workers use version 2, including `split_required`. |
| Retention ban | Failures never retain request values, worker prose, prompts, chain-of-thought, credentials, environment values, or unbounded exception output. |
| Issues #40 and #78 | Protocol failures remain retryable, so retained worktrees stay in place for ordinary recovery. |
| Issue #80 | This does not claim an underlying cause for its retry exhaustion. |
| Envelope handling | Return only strict `WorkerResultBuildInput` JSON as the final message. `AgentInvoker` supplies it to the controller-owned builder. Neither helper output nor other prose is directly authoritative. |

### Worker validation diagnostics

This table gives the safe diagnostic rules.

| Diagnostic part or event | Contract |
| --- | --- |
| Contents | The diagnostic has only its stable origin, an allowlisted field path, and a stable corrective reason. |
| Exclusions and bound | It never includes a worker-supplied unknown field name, supplied value, raw output, prompts, or credentials. It is one line of at most 2,048 UTF-8 bytes. |
| Controller record | The controller carries the same safe diagnostic in condition history, status, recovery inventory, and the GitLab handoff. |
| Repeated automatic failure | One coarse phase with the same error code and diagnostic blocks the item. The operator corrects the condition, then runs `agent-work loop retry --issue-iid IID --apply`. |
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
| Issue is open and non-confidential; has no lifecycle projection, ledger record, or open split prerequisite | Eligible for fresh selection. |
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
| `agent::awaiting-human` | awaiting-human | An exact MR, its named source commit, needed receipts, and one factory-authored actionable issue handoff await human review. |
| `agent::blocked` | blocked | A human must take the recorded next action. |
| `agent::failed` | failed | The runner could not safely reconcile or record an exceptional condition. |
| `agent::needs_repair` | needs-repair | A legacy or manually checkpointed repair awaits exact recovery. Ordinary exact pipeline failures do not enter this state. |
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
| Verified publication | Moves to `observing`. The controller waits for the terminal state of the exact SHA, then a fresh Pipeline Performance Judge reviews it. |
| Healthy review | Writes the actionable handoff for the exact MR source commit, then moves to `awaiting-human`. |
| Concrete repository-actionable problem | Creates a deduplicated ordinary issue for later normal selection. It does not directly repair the published MR. |
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
| First rollout | Has no sidecar checkpoint for each role or phase. |
| Legacy phase-ledger value | Finishes its recorded route. |
| New work | Uses `WORKFLOW`. |
| Approved design | Architect gives it to Red and the Diátaxis Writer concurrently. The conditional Agent-policy Writer joins when portable agent policy is in scope. |
| Writer scope | Red changes only `tests/`. The Diátaxis Writer changes only `docs/`. The Agent-policy Writer changes only `AGENTS.md`, `.agents/**`, and their declared portable `template/` mirrors. |
| Before Green | Architect waits for every applicable receipt, or the exact not-applicable record. |
| Green | Aligns the documentation draft with the final interface. It never changes agent-policy paths or their mirrors. It then runs the serial validation TDD Judge against its staged tree. |
| Before specialist review | The Judge's unchanged tree OID and supported-version JUnit digests and counts are needed. Each applicable concurrent specialist reviewer, including `$license-checker`, then reviews the same stable post-Green diff. |
| Findings | Green combines all initial findings. |
| Policy correction | Goes through the still-live Architect to the Agent-policy Writer. Green cannot fix policy. |
| Final decision | Before Architect makes it, every changed candidate follows the new Judge, applicable review, and Finalizer sequence. |

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
| `created`, `waiting_for_resource`, `preparing`, `scheduled`, `pending`, or `running` | Wait; do not select a new issue. |
| Missing, malformed, inaccessible, identity, or check-pipeline evidence | Fail closed; do not allow unrelated work. |

| Gate order | Rule |
| --- | --- |
| Before ordinary new-issue selection | Reconcile the listed work first. A corroborated failed pipeline takes priority over ordinary selection. |

| Failed-pipeline condition | Eligibility, action, and outcome |
| --- | --- |
| Exact failed MR pipeline with one retained owner | Automatic repair needs exact configured project, merge-request, branch, source-SHA, and worktree evidence. Consider exact candidates in fixed order and run at most one repair workflow per activation. |
| Scheduled repair backoff or quarantine | Skip that item; seek another eligible exact repair before ordinary work. A failed automatic repair creates a durable 20-minute per-item backoff. After three automatic repair attempts, quarantine or defer it for human review; do not select it automatically. |
| Status or preview | Retain attempt count, timing, evidence, and next action. |
| Failed default-branch pipeline, or failed MR pipeline without one safe exact owner | Record an actionable human handoff or block. Never claim an unrelated issue, invent a repair owner, or weaken the human-only approval and merge boundary. |

| Repair inventory | Rule |
| --- | --- |
| Before an eligible repair | Read a bounded job inventory for the verified project, merge request, source SHA, and pipeline. Include retry attempts. Ignore failed `retried=true` jobs because a later attempt replaced them. Exactly one current failed `retried=false` job must remain. |

| Evidence step | Required handling |
| --- | --- |
| Job trace | Fetch only the selected job's trace. Strip control sequences and secret-like content, then cap the retained diagnostic. |
| Typed evidence | Bind project, merge request, source SHA, pipeline identity, pipeline SHA and status, and the job's ID, name, stage, status, failure reason, trace provenance, and trace status. |
| Persistence and restart | Save checked, bounded evidence before the repair workflow. On restart, check and reuse it; give the workflow prompt the same bounded diagnostic. |
| Excluded data | Raw job traces never enter the ledger, status report, or workflow prompt. |
| Invalid evidence | A wrong or changing identity or status, multiple or no current failed jobs, an empty trace, or a schema or API failure stops repair without starting a worker. This process never retries or cancels jobs or pipelines. |

| Stage | Required order and boundary |
| --- | --- |
| Post-publication assessment | Start fresh after terminal exact-SHA evidence. Do not edit the published MR or enter a repair session directly. A concrete repository-actionable finding creates or reuses a deduplicated issue for later normal reconciliation and a new applicable workflow. Unverified evidence creates no repository work. |
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
| `RETRY_WAIT` | `retry_at` is due. No owner is immediate. | Resume the earliest due retry. A future retry is deferred. Scan on. |
| `PUBLISHING` | The retained branch is exact. Publication identity is pending. | Resume publication for this item. Repair it when safe. |
| `OBSERVING` | The current source SHA is exact. Pipeline evidence exists. | Observe or resume. Nonterminal evidence waits. A safe failure must be exact. It can repair. |
| `REPAIR` | One failed current-source pipeline has a unique owner. | Run at most one repair workflow. Or resume one. Do this in the activation. |
| `NEEDS_REPAIR` | A legacy or reviewed recovery record exists. | Defer. Expose reviewed recovery for the exact IID. Scan on. |
| `REPAIR_FAILED` | Per-item repair timing and attempt evidence exists. | Resume only when it is due. It must be safe. Otherwise, defer or quarantine locally. Scan on. |
| `SUGGEST_CLOSE` | Closure evidence is corroborated. | Skip. Expose the human closure decision. |
| `BLOCKED` | A human action or unresolved item evidence is recorded. | Skip automatic work. Expose the exact action. |
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
| Project-wide pipeline stop | Stop for a failed default-branch pipeline. Also stop for a cross-item owner. The owner must be truly ambiguous. |
| Fast-forward reconciliation | Require configured numeric project identity. Require retained MR identity and URL. Require a matching source branch, the configured target project, and the default branch. Require a clean retained worktree and no competing identity. The retained branch and `HEAD` must equal the MR source SHA. The retained SHA must be an ancestor of the MR SHA. On failure, quarantine the item locally. Then continue scanning. Never choose an MR by recency or issue text. |
| Suggested closure | `agent::suggest_close` needs structured, corroborated evidence. The evidence can show a duplicate. It can show a superseded issue. It can show an already-satisfied or no-longer-applicable issue. It is a recommendation, not a closure. A human closure alone reconciles the record to `COMPLETE`. A human closure also clears the label. The runner never approves, merges, or closes an issue. |

| Closed-issue recovery step | Outcome |
| --- | --- |
| Start an applied activation | Take the single-project lock. Reconcile intent with observed evidence. Do this before open work. Then obtain one bounded, fixed IID-ordered inventory of closed issues with lifecycle labels. |
| Listed issue with a ledger | Normalize its record to `COMPLETE`. Use the authenticated GitLab identity boundary for label removal. Before removal, clear transient retry and intent. Also clear error, worker-exit, and pending-feedback state. |
| Listed issue without a ledger | Remove stale lifecycle labels. Do not create a ledger record. Do not create a blocked or complete state. Do not create a journal or worktree. |
| Write or label failure | A ledger-write failure leaves the label. A recorded issue's label-removal failure leaves its canonical `COMPLETE` record. It also leaves a stale label for a later activation. An orphan failure leaves only its stale label. Earlier IID successes remain reconciled. They stay reconciled. |
| Scope and next step | This housekeeping uses neither `max_segments` nor the open-work budget. It starts no worker. It performs no repository, branch, worktree, push, or merge-request operation. Open orphan projections keep the usual blocked recovery path. After inventory, advance at most one selected or resumed open issue. Recovery applies at the worktree boundary. It also applies at the coarse workflow boundary, not every internal agent handoff. |

| Liveness event | Scheduler action |
| --- | --- |
| Active Python activation | Advance at most one issue. Keep its active-work budget. That budget is 10,800 seconds. |
| Nonterminal default-branch or exact current-MR pipeline evidence during `loop run --apply` | Stay in the activation. Poll after 5 seconds. Poll again after 10 seconds. Then poll after 20 seconds. Cap the poll at 30 seconds. Resume promptly when the evidence becomes terminal. |
| Wrapper continuation | Continue only after `awaiting-human`, `blocked`, `failed`, or `complete`. Check that report. Make one fresh read-only eligibility check. Make one fresh read-only preselection-pipeline check. Start another activation only for `eligible`. Between issues, re-query GitLab. Then fast-forward the clean service base. This housekeeping is outside the per-issue budget. |
| Exact failed current-source pipeline | Activate `repair`. Run the repair workflow. Run it in the same applied activation. |
| Handoff or idle | A future `retry-wait` remains a handoff. A future `time-limit` or `segment-limit` remains a handoff. A human or safety block remains a handoff. Missing, failed, or unrecognized evidence remains a handoff. `idle` means no eligible or resumable work exists. |
| Timer and scope | `OnUnitInactiveSec=20min` starts only after a genuine idle service exit. It then checks for new work. It is not the active-pipeline polling cadence. The activation deadline and systemd outer timeout still apply. The loop is serial for each host and project. It is not multi-host coordination. |

## Ledger, worktrees, and audit

The ledger is the durable audit record. It keeps facts a restart must use.

| Audit area | Contract |
| --- | --- |
| Location and worktree | State is below the XDG state directory, keyed by configured GitLab host and numeric project ID. Each `loop status` ledger item names its durable worktree path. Each active issue has one atomic, restrictive-permission ledger record and durable worktree. |
| Version 2 fields | Bounded issue title; activation, latest-meaningful-event, and completion timestamps; main, source, publication, and merge-request identities; and bounded retry and failure history apart from the current actionable error. |
| Phase timing | `phase_timings` keeps the newest 64 controller-computed entries. Phase timing is the public schema term because entries identify a durable `WorkflowPhase` value or a controller-observed role; the ordered human workflow groups remain stages. Each entry has `phase_or_role`, `attempt`, `started_at`, `finished_at`, `elapsed_seconds`, `outcome`, and `classification`. Classification is `execution` or `wait`. The timestamps come from controller clock boundaries, not agent narration. The controller records only boundaries it can observe; the list does not claim timing for unobserved subagent internals. |
| Exhausted retry | The current condition is `retry_exhausted`. Bounded history keeps the latest sanitized underlying condition code and detail. Human and JSON status show the retained actionable cause, and the safe lifecycle result is `agent::blocked`. |
| Final workflow summary | A typed, bounded summary keeps selected lane, capacity decision, nested roles, changed paths, tests and JUnit evidence, reviewers, findings, and corrections. The runner checks its canonical changed-path list against the durable worktree. Checked worker receipts must contain the other entries, but the runner does not independently verify AI claims. |
| Excluded audit data | The ledger never stores prompts, chain-of-thought, credentials, environment values, issue bodies, comments, transcripts, raw output, or unbounded data. |
| Legacy records | Old records stay readable. A missing `phase_timings` field loads as an empty list. Other missing legacy evidence is `unknown`. The next persist adds current fields and never invents past values. |
| Local log | The application writes `agent-work.log` in that XDG project-state directory. It is application-owned evidence beside, not instead of, the systemd service journal. |
| Mutation evidence | The runner saves intent before an external mutation and observed evidence after it, so it can reconcile interrupted idempotent work. |
| Dirty worktree and stashes | A dirty durable worktree stays for its next workflow attempt. The runner inventories recovery stashes only; it never applies or drops them. |
| Ambiguity and corruption | Ambiguous worktrees, branches, labels, or stashes become blocked for human review. A corrupt record is blocked when reportable and failed only when it cannot be recorded safely. |
| Clean unpublished interruption | Before worker invocation, normalization needs agreement among the open issue and lifecycle label, fixed branch and worktree, and recorded baseline; the baseline worktree must be clean; and there must be no MR, publication, draft, intent, feedback, receipt, or decomposition evidence. The runner keeps all artifacts, changes the record to `blocked`, gives actionable retry guidance, and never deletes a ledger, branch, or worktree. |

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
| Human item line | States whether work runs, what changed, the current failure or condition, and the next action. It includes issue, projected and raw state, phase, identity facts, and available worker-exit evidence. When phase timing evidence exists, it includes one concise `timing=` token with the entry count, summed execution seconds, summed wait seconds, and the latest phase, outcome, and elapsed seconds. |
| JSON detail | Includes the full bounded `phase_timings` list, detailed check evidence—tests, reviewers, and audit receipts—plus timestamps, conditions, and corroboration. |
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
| Applied worker limit | Give the worker the time left in its 10,800-second budget. |
| Timeout action | At the outer limit, systemd sends TERM to the full service control group. It gives TERM 30 seconds before KILL, then kills survivors. |
| Timeout evidence | The unit journal records `Result=timeout` and relevant sanitized wrapper diagnostics. The durable ledger and retained issue worktree stay in place. Normal retry and human diagnosis use that evidence. |

A workflow-owning worker returns one strict `WorkerResultBuildInput` JSON
object as its final message. For the handoff-aware production `AgentInvoker`,
that message is untrusted builder input, not a handoff result. `AgentInvoker`
captures Codex's final message through `--output-last-message` and the
supported final-text output from Claude or Pi. It then invokes the
controller-owned `worker-result build` helper with controller-owned issue,
input, handoff, and nonce values and `--apply`. Only the resulting
controller-owned artifact can be accepted as a handoff result. Other standard
output and model prose, including output from a successful worker process,
are never accepted as a handoff result.

| Envelope field or check | Contract |
| --- | --- |
| Result | `red_checkpoint_ready` for the coding Red boundary, or `ready_to_publish`, `retryable`, `needs_human`, or `split_required` for a workflow-owning result. |
| Content | Include the coarse completed state, receipts, diagnostic, and next action. |
| Trusted runner | Before changing state, check the ledger, configured numeric project, Git evidence, and needed workflow receipts. |
| Trusted capture | `AgentInvoker` captures the supported worker's final message, supplies it to the builder, and retains one typed capture outcome: `captured`, `pre_run_cleanup_failed`, `artifact_not_written`, `artifact_unreadable`, `artifact_too_large`, `builder_rejected`, `builder_budget_exhausted`, or `post_read_cleanup_failed`. The controller accepts a result only from `captured` after its identity and changed paths match the controller's expected handoff. |
| Rejected capture | A missing, malformed, or rejected captured final message; a missing artifact after a successful worker; a malformed artifact; controller identity mismatch; changed-path mismatch; or applicable filesystem capture, cleanup, or size-limit failure rejects the result. The durable diagnostic is a bounded constant that identifies the stage and category. A builder rejection can retain the fixed `worker_result: could not be read or validated` or `worker_result_handoff: could not be written` diagnostic, or an existing canonical, allowlisted validation diagnostic. The controller suppresses all other builder standard output and standard error. Diagnostics never retain raw exceptions, output, paths, identities, secrets, payloads, or worker data. Cleanup is attempted for every captured artifact; a cleanup failure rejects the result. |
| Coding final audit | Name Architect, Red, Diátaxis Writer, the conditional Agent-policy Writer, Green, all applicable reviews including the initial `$license-checker`, and Finalizer. |
| License receipt | Finalizer needs a current approving license receipt for the final in-scope diff. During correction, rerun the checker only if a maintained file in its checked scope changed. A violation cannot be waived or marked not applicable. |
| Policy finding | Send it through Architect to the Agent-policy Writer, not Green. |
| Content work | Keep its separate role set. |

### Live-agent capacity launch contract

Every Architect launch has one fixed global requirement: `12` live slots for
coding and `3` for content. This includes terminal, normal serial,
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
| Requirement | `12` total live slots for coding; `3` for content. The selected lane sets the requirement. |
| Missing report | Continue. Missing global capacity is not a shortage. |
| Reported shortage | Return `needs_human` before delegation, fanout, or a worktree change. State the lane, fixed requirement, reported total, shortfall, and an actionable frontend remedy. |
| Prohibited inference | Do not request or calculate topology, widest-row, retained-object, current-use, fresh-handle, per-owner, direct-child, descendant, or owner-headroom counts. None creates another capacity gate. |
| New launch | A prior report does not authorize a new launch. Use a newer current report when one is available. |

The worker-result builder preserves its trust boundary. It derives identity,
ledger revision, state, phase, and duplicated audit fields from the controller.
The captured final-message input supplies only nonduplicated intent and
evidence; it must not supply those controller-derived fields. Legacy ledger
records remain readable through their recorded route.
See [Configure agent
capacity](../how-to/configure-agent-capacity.md) for the operator procedure.

### Intake sizing contract

At the start of a new coarse `workflow` session, the trusted wrapper delegates
the live Architect. Before Red, documentation work, lane selection, or a
worktree change, Architect makes one semantic sizing decision.

The table gives the allowed results. It sets the next step. Use it before work
starts.

| Intake result | Rule |
| --- | --- |
| Suitable issue | It has one delivery outcome that can be reviewed and merged on its own. It can finish each applicable workflow phase within the effective bounded worker budget. |
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
dependency gate. The runner projects this state to operators with non-scoped
`agent-prereq_<IID>` labels. A projection label never changes eligibility.

The table shows what labels mean. Labels show open needs. They do not set the
gate. The gate uses only trusted facts. A label gives no extra power. These
rules keep the view clear.

| Projection state | Required label action or outcome |
| --- | --- |
| Open split child | It has one `agent-prereq_<IID>` label for each open trusted prerequisite. The runner orders prerequisite IIDs numerically when it calculates and reports the set. |
| Project label | The runner creates or reuses it safely on repeat. It has color `#6F42C1` and description `Open split prerequisites for agent-work issue dependency display.` |
| Reconciliation | It adds missing labels and removes stale ones. It removes a label when its prerequisite closes, the trusted marker dependency is removed, or the dependent issue closes. Repairs are idempotent, and a partly completed remote update stays retryable. |
| Applied reconciliation | It creates or normalizes canonical project-label definitions, removes exact legacy `agent::prereq_<IID>` labels from affected issues, and adds the full numerically ordered canonical set. |
| Legacy definition | It is deleted only after every issue-label change succeeds. If a change fails, definitions remain for the next safe, idempotent retry. |
| Preservation and preview | Reconciliation keeps unrelated and lifecycle labels. Preview reports the intended change but does not write it. |

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
| Rejected internal review | Architect and Green send it to its owning role in the session. |
| Interruption | The runner resumes only at `WORKFLOW`. It does not guess or save a role-level checkpoint. |
| Required receipt | Receipts must still match the manifest's needed work. Required conditional judges use `not applicable — <reason>`. Arbitrary receipts cannot advance the workflow. |

| Publication boundary, authority, or action | Required handling |
| --- | --- |
| Idempotent publication | The runner checks the recorded source branch, default target, exact same-project MR, remote and local tip, and issue-closing evidence. |
| Existing-MR remediation | It uses the verified MR source branch and never creates a second MR. Before it prepares or publishes work, it binds remediation to the exact configured project, MR, source branch, default target, and recorded source SHA. |
| Protected-policy action | Before publication, protected-policy handling checks the actual changed paths in the exact proposed publication tree against the durable, verified target commit, including template mirrors. It does not trust a requested path list or raw worktree status. |
| Target-only protected-policy change | A protected-policy change that arrived only on the target branch during reconciliation is not issue-owned. |
| Issue-owned protected-policy change | An issue-owned protected change, or a resolved merge whose final tree differs from that target, still needs protected-policy evidence. |
| Merge-identity outcome | The repository boundary rejects an unresolved, mismatched, or ambiguous merge identity before publication. Identity ambiguity or drift remains `publication_identity_mismatch`. |
| Installed pre-commit rejection | Standard pre-commit output must identify at least one failed installed hook. The controller then proves the exact issue, branch, retained worktree, unchanged `HEAD`, publication baseline, pending-merge identity, staged Git tree, and retained changes before recording `publication_commit_rejected`. A hook may fix an unstaged working file only when the staged tree remains exact. The controller records an immutable durable correction identity that is independent of the current `error_code` and transition intent. It binds the exact worktree, branch, `HEAD`, merge state, read-only staged-index fingerprint, staged and unstaged paths, selected workflow lane, and the exact coding Red checkpoint. Content work binds no Red checkpoint. Worker exit, worker-protocol, and retry failures retain this identity. Each controller-authored snapshot is immutable; after an authorized worker attempt the controller may replace it only with the next fully observed exact snapshot. The controller clears the intent, `publication_sha`, old merge request draft, completed worker result, receipts, and final audit, then returns the record to `WORKFLOW` for fresh correction. Coding must load the exact retained Red checkpoint. Content must return fresh content receipts and audit evidence. Both lanes must produce a fresh merge request draft; an unknown lane, or missing or mismatched coding Red evidence, blocks before worker invocation. The identity is cleared only after freshly validated corrected evidence supersedes it or normal publication succeeds. The rejection route does not push, create or update a merge request, or project a blocked state. Signing, identity, configuration, lock, file-system, object-database, and unknown commit failures fail closed. |
| Legacy identity-mismatch recovery | A reviewed retry may use this one-time route only for an old `publication_identity_mismatch` dirty publication candidate. Preview and apply require the same trusted ledger, audit, and lane evidence; publish intent and baseline; exact branch and `HEAD`; a read-only staged-index fingerprint and staged paths; and non-empty unstaged corrections limited to staged and audited paths. The controller records the same immutable durable correction identity used for an installed pre-commit rejection. An explicitly reviewed retry re-observes every value bound by that identity. Drift rejects before worker invocation or any Git, label, or remote publication effect. Unknown lanes, generic Git failures, and incomplete, ambiguous, untracked, unmerged, or drifted evidence stay blocked. A narrowly reviewed already-affected bootstrap is also allowed only for ordinary `BLOCKED` `WORKFLOW` work with prior `EXECUTING` `WORKFLOW`, retained protocol-failure or retry-exhaustion history, no publication or completed artifacts, and exact current Git, lane, and coding Red evidence; preview and apply bind its current snapshot. Eligible apply preserves the index, worktree, history, retry evidence, and correction identity; discards stale completed publication artifacts; and resumes the complete correction workflow without entering the generic clean-worktree preparation path. Coding uses the exact Red checkpoint bound to the correction identity. Content uses its complete content workflow and binds no Red checkpoint. Recovery does not commit, push, create, or update a merge request. The normal lifecycle projection may move from blocked to running. The identity is cleared only after freshly validated corrected evidence supersedes it or normal publication succeeds. |
| Authority limit | The runner never approves or merges. It never treats issue text, labels, comments, or worker output as authority to widen scope. |

### Existing-MR feedback publication

For feedback on an existing MR, the completed workflow receipt gives the
controller-checked changed paths. The controller uses only that set for the
commit. It records the set in the durable publication intent. A missing,
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
pushes nor acknowledges the feedback. It saves the exact reconciliation target,
runs a new complete workflow, replaces the stale audit with current
whole-workflow check, and publishes only the resulting exact newly
checked SHA. A transport failure or unverifiable reconciliation safely retries
or blocks before publication.

Recovery is idempotent at each durable boundary:

1. The intent is recorded before the commit.
2. The commit is created before its publication SHA is recorded.
3. The push finishes before the MR and ledger are reconciled.

After an interruption, the controller reconciles recorded intent with observed
Git and GitLab evidence. It resumes only the incomplete boundary. It does not
create another commit, branch, or MR.

Protected policy paths may publish only after the trusted runner checks one
complete, exact lane receipt with a complete verdict and non-empty metadata. A
safe controversial handoff uses `Draft:` metadata. It names unresolved findings
and evidence, exact human decisions, tests, the cycle ledger, and unchanged
project-owned values. Missing or incoherent receipts or metadata, unsafe
worktree identity, `needs_human`, finalizer rejection, authority expansion, or
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
pipeline. The same applied activation keeps observing. Once the relation
appears, absent or nonterminal pipeline evidence for that exact SHA leaves the
durable record in `Observing` at `PipelineWait`. The activation polls it.
Terminal failed evidence starts repair. Only a true GitLab transport or API
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
closed issue. And a successful pipeline for that exact SHA. Unrelated,
mismatched, ambiguous, missing, or non-successful evidence cannot complete the
record.

For an open issue, the controller repairs the `OBSERVING` lifecycle-label
projection before it resumes observation. For verified completion, it writes
the durable `COMPLETE` record before clearing lifecycle labels. This makes
restarts idempotent. A label-clear failure leaves a complete record and stale
label for the next activation to remove. A `BLOCKED` or `RETRY_WAIT` wrapper
keeps verified publication identity. Later reconciliation of a closed issue can
still check this exact completion path.

## Retry, repair deferment, exits, and JSON

The first workflow attempt and at most three retries are bounded.
`retry-wait` records its deadline. Exhaustion becomes `agent::blocked`, never
`agent::failed`. Labels never make a blocked item eligible. After the recorded
human action, `loop retry --issue-iid IID --apply` resumes that IID from its
retained worktree and durable record. It does not select a replacement issue or
discard retained recovery evidence.

Worker exits, timeouts, malformed worker protocol, invalid receipts, and an
explicit `retryable` worker result are due at once. They continue in the same
activation. Transient GitLab or transport failures keep retry timing. The XDG
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
| Worker exit, timeout, malformed worker protocol, invalid receipt, or explicit `retryable` result | Immediately due and continued in the same activation. |
| Transient GitLab or transport failure | Keeps retry timing. |
| Repeated `worker_protocol_invalid` or `worker_receipt_invalid` with the same error code and diagnostic in one coarse phase | Blocks immediately without another retry; retain the worktree, phase, and safe cause. |
| Changed protocol code, diagnostic, or phase | Changed evidence; use the ordinary bounded retry path. |
| Exact-pipeline repair failure | Record `automatic_repair_count` and a 20-minute `automatic_repair_at`; after three failed attempts, quarantine or defer for reviewed recovery. |

For `worker_protocol_invalid` and `worker_receipt_invalid`, the runner also
compares the canonical error code and bounded, stable, redacted contract-field
or reason diagnostic with the latest automatic condition in the same coarse
phase. The first failure schedules its immediate retry. If a later automatic
attempt has the same pair in that phase, it made no progress. The runner blocks
the issue at once without using another retry. It keeps the worktree, phase, and
safe cause. It publishes the usual blocked projection and comment. A changed
code, diagnostic, or phase is changed evidence and follows the usual bounded
retry path. This does not change retries for non-protocol transient failures.
After correcting the condition, run `agent-work loop retry --issue-iid IID
--apply`. That explicit human retry resets the automatic retry budget.

Automatic exact-pipeline repair has its own budget: at most three repair
workflows for one exact retained owner. After each failed attempt, the ledger
records `automatic_repair_count` and a 20-minute `automatic_repair_at` deadline.
These are separate from ordinary `retry_count` and `retry_at`. A later
activation can consider a due item again. After a third failed attempt, the
item is quarantined or deferred and needs reviewed recovery. This is separate
from ordinary `retry-wait` and its workflow retry count. It does not change a
human-controlled pipeline-performance budget or stop unrelated eligible work.
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
| Empty or missing closing-issue projection | Treats it as projection lag and makes at most four projection reads: the initial read plus three retries, each separated by exactly one second, for at most three seconds of intentional wait. It never repeats create or update; absence after the fourth read fails closed. |
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
