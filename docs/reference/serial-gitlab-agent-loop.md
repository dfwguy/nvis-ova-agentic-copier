---
title: Serial GitLab agent-loop reference
type: reference
summary: Look up the recoverable loop commands, lifecycle, data, and operating constraints.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-19
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
agent-work worker-result build --issue-iid IID --input-file PATH [--json] [--apply] [--debug]
agent-work merge-request publish --source-branch BRANCH --target-branch BRANCH
                                 --title TEXT --problem TEXT --outcome TEXT
                                 --validation TEXT [--validation TEXT ...]
                                 [--impact TEXT] [--human-action TEXT]
                                 [--tracking TEXT] [--issue-iid IID]
                                 [--apply] [--json]
agent-work validation run --candidate-tree TREE [--apply] [--json]
agent-work validation status --candidate-tree TREE
                             [--wait-seconds SECONDS] [--apply] [--json]
agent-work migration preview [--json] [--debug]
agent-work migration apply --plan-file PATH --apply [--json] [--debug]
agent-work upstream-finding preview --manifest-file PATH --upstream-host HOST --upstream-project-id POSITIVE_INT --upstream-credential-file PATH [--apply] [--json] [--debug]
```

An applied `loop run` performs one bounded, recoverable activation for one
issue. The worker owns the complete applicable workflow session. The defaults
are 16 workflow attempts and one 10,800-second (three-hour) overall work
budget for the complete activation. `--max-segments` and
`--max-activation-seconds` override those respective bounds. Each workflow
invocation receives the positive, ceil-rounded time remaining from that single
budget. Exhausting the budget follows the normal retry and durable-worktree
recovery contract.

Before it selects work, an applied run reconciles prerequisite-label
projections for split children. If that pass repairs any projection drift, the
activation performs only that housekeeping: it does not claim a dependent
issue, create a ledger record or worktree, or start a worker. A partial remote
failure is retryable; repeating the same reconciliation is safe. Without
`--apply`, `loop run` reports each current and intended prerequisite-label
projection but makes no writes.

`AGENT_WORK_FACTORY_USERNAME` is required and must equal the authenticated
GitLab service username. While an exact open factory MR awaits review, the loop
reads bounded general comments and inline discussion replies. New human feedback
wakes only that issue for existing-MR remediation; no-change feedback is
acknowledged once, while code changes reuse the same MR and preserve its title,
description, and closing reference.

### `merge-request publish`

`merge-request publish` renders a reviewer-facing Markdown description with
the required Problem, Outcome, and Validation sections. It adds Impact, Human
action, and Tracking only when their matching flags are supplied. Supply one
or more `--validation` values. Literal escaped line-break payloads are
rejected before the command can change GitLab.

`--issue-iid` accepts one positive issue IID. When supplied, preview and
rendered output end with exactly one canonical `Closes #IID` line. Omit the
option for issue-less publication. `--tracking` may accompany `--issue-iid`,
but it is only for non-closing relationships. A closing directive in any
user-supplied structured text is rejected; use `--issue-iid` instead. Inputs
and merge-request text are untrusted data.

Without `--apply`, the command previews the intended description and reports
whether it would create or update, with no mutation. With `--apply`, it uses the configured numeric GitLab project to
find zero or one open merge request for `--source-branch`: it creates one when
there is none, updates it when there is one, and rejects an ambiguous result.
It then reads that exact merge request back and verifies its IID, source and
target branches, title, and description. When `--issue-iid` is supplied, it
also requires GitLab's authoritative closing-issue projection to contain
exactly that issue in the configured project. A conflicting, missing, or
otherwise mismatched projection fails closed.

The command never pushes a branch, approves a merge request, or merges it.
Push the branch first; see [how to validate template changes](../how-to/validate-template-changes.md#publish-the-verified-branch-for-review)
for a practical publication sequence. `--json` provides the corresponding
machine-readable result.

### `validation run` and `validation status`

The Judge validates one staged candidate through a durable session keyed by
its `TREE` value. Use `validation run --candidate-tree TREE` to preview the
action. It does not mutate state or start a hook without `--apply`.

With `--apply`, `validation run` first verifies that `TREE` is the current
staged tree. It records the session as running before it invokes exactly one
`git hook run pre-commit` command, then records that command's terminal exit
code atomically. It returns only after the hook reaches a terminal result.
The session retains no hook output or secrets. A terminal zero is the only
outcome that permits inspection of the unchanged tree and JUnit evidence; a
terminal nonzero blocks validation. The command never starts a second suite
for the same candidate-tree session.

`validation status --candidate-tree TREE` is read-only and never launches a
hook. It reports `running`, `terminal-success`, or `terminal-failure`. With
`--wait-seconds`, it polls a running session for at most that bounded interval
until it becomes terminal. Use it after a yielded or lost frontend command
handle, and continue to query the same `TREE` rather than starting another
validation run. It accepts `--apply` for command consistency, but remains
read-only.

Exit code `0` means a preview or terminal success. An applied run that remains
running, a terminal failure, a missing or malformed session, or an operational
failure exits `1`; invalid usage exits `2`. JSON reports at least the candidate
tree, whether the action was applied, the action, state, whether it is
terminal, and the final exit code only after the session is terminal.

### `credential rotate`

`credential rotate` safely rebinds the environment file’s
`AGENT_WORK_FACTORY_USERNAME` after an operator has changed the configured
directory-scoped GitLab credential. `--environment-file PATH` identifies the
existing service environment file; `--unit-name NAME` identifies its user
service and timer. Both are required. A token is never accepted as a flag or
written to configuration, standard output, diagnostics, or logs.

Without `--apply`, the command is a dry run: it validates the named non-secret
configuration, reports a redacted prospective outcome, and makes no
configuration or systemd change. With `--apply`, it disables and stops the
timer and stops the service before requesting credential-dependent evidence.
They remain stopped in every outcome, including success.

Before writing, applied rotation derives the authenticated username through the
environment file’s configured bare GitLab host and positive numeric project
endpoint. It requires all of the following: exactly Developer (`30`) effective
access; an effective default branch that is protected and neither pushable nor
mergeable by Developers; and at least one global approval rule requiring at
least one approval. It then atomically changes only the expected username
entry. Invalid, expired, unauthorized, or username-mismatched credentials, and
all failed preconditions before a write, leave the environment file byte-identical.

After an applied update, the command runs one bounded, isolated, scrubbed,
non-claiming wrapper preview. That preview verifies the updated runtime
credential route and the existing fail-closed username check without starting
normal work. If it fails, rotation makes one bounded exact-byte rollback
attempt and returns a sanitized corrective handoff. If that rollback cannot be
confirmed, it reports configuration uncertainty for human correction. It never
retries updates indefinitely or starts, enables, or re-enables the timer.

Successful text output is a concise redacted state outcome. `--json` produces
the corresponding redacted structured result for automation. `--debug` adds
only local sanitized diagnostics. Success exits `0`; a rejected precondition,
credential failure, preview failure, or rollback-required outcome exits `1`;
argument or command-usage errors exit `2`. Output and errors never include a
token, credential material, or environment-file contents.

The preview invokes the installed wrapper as an argument vector, not a shell
string: `/usr/bin/env bash <checkout>/scripts/agent-work-systemd-run.sh`.
The wrapper is intentionally mode `0644`; this invocation does not require an
executable-bit change.

Rotation deliberately stops before canary execution, re-enablement, ordered
canary processing, or retained-record recovery. The operator reviews the
successful result, follows the setup guide’s canary procedure, and explicitly
re-enables the timer only after that review.

### `service-environment migrate`

`service-environment migrate` validates or upgrades one installed user-service
environment file to the current factory-managed contract. `--environment-file
PATH` and `--unit-name NAME` are required. Bare invocation is a non-mutating,
non-claiming validation and preview: it does not select an issue, acquire loop
work, mutate an issue, ledger, worktree, branch, or label, change GitLab, or
start a worker.

The current contract version is `1`. Factory-managed required keys are the
contract-version marker, `AGENT_WORK_CHECKOUT`, `AGENT_WORK_GIT`,
`AGENT_WORK_GLAB`, `AGENT_WORK_JQ`, `AGENT_WORK_FLOCK`, `AGENT_WORK_TIMEOUT`,
`AGENT_WORK_UV`, `AGENT_WORK_AGENT_PATH`, and `AGENT_WORK_DIRENV`. The
project-owned keys `AGENT_WORK_AGENT`, `AGENT_WORK_BASE_BRANCH`,
`AGENT_WORK_FACTORY_USERNAME`, `GITLAB_HOST`, and `GITLAB_PROJECT_ID` are
preserved rather than regenerated.

With `--apply`, the command disables and stops the named timer, stops the
service, and verifies that both are inactive. Before any write, it validates
the existing configured Developer identity, numeric project, protected default
branch, and required approval safeguards through the checkout's `direnv` and
password-store credential route. It never rebinds that identity and never
places credentials in command arguments, the environment file, output,
diagnostics, logs, or the ledger.

After pre-write validation, applied migration regenerates only factory-managed
version, checkout, and tool-path fields using a recoverable mode-600 atomic
replacement, then performs one isolated non-claiming validation. It leaves the
timer and service stopped in every outcome for human review, canary execution,
and explicit re-enablement. A failed pre-write check changes no bytes. A later
failure returns one sanitized actionable error while preserving a
byte-recoverable prior configuration; it does not start either unit. Successful
preview or apply exits `0`; a rejected precondition or migration failure exits
`1`; invalid command usage exits `2`. `--json` produces a redacted structured
result with `command`, `applied`, `outcome`, `config_updated`, `rolled_back`,
`timer_stopped`, `service_stopped`, and `next_action`. The outcome is one of
`current`, `stale`, `verified`, `rolled-back`, or `correction-needed`.
Without `--json`, the command reports the same migration state as concise
non-secret fields. `--debug` adds only sanitized local diagnostics.

That validation invokes the same intentionally mode-`0644` wrapper through
the unit-shaped argv `/usr/bin/env bash <checkout>/scripts/agent-work-systemd-run.sh
--validate-service-environment`, never through a shell string or an executable
wrapper file.

Without `--apply`, `loop run` reports the next safe action once without
changing GitLab, the ledger, worktrees, branches, labels, or services; it does
not simulate a complete activation or poll. `loop status` is read-only and reports
durable ledger records together with verified configured-project identity and
best-effort live corroboration. Without `--issue-iid`, it returns at most 100
records in deterministic order and reports the total and whether the result was
truncated. With `--issue-iid`, it performs an exact lookup for that item.
`loop retry`
previews one recorded blocked item; `--apply`
resets its retry budget and moves it to resumable preparation after explicit
human direction.

The operator-visible immediate owner set is `PREPARING`, `EXECUTING`,
`PUBLISHING`, `OBSERVING`, and `REPAIR`. `BLOCKED` is excluded
from that set: it remains action-required and is not runnable. `RETRY_WAIT` is
queued, not a competing immediate owner when an exact IID is already selected.
That selected IID stays pinned across a multi-segment continuation. A reviewed
exact retry is the durable handoff: it moves one chosen `BLOCKED` IID to
`PREPARING` (or retains `PUBLISHING` for a publication replay) without making
untouched blocked or queued-retry records compete. Retry fails closed when a
different immediate owner exists. More than one true immediate owner remains a
reconciliation-required condition.

`loop canary` is an operational checkpoint, not an issue run. Bare invocation
creates a redacted, immutable plan that verifies public service and project
identity, durable recovery evidence, and current pipeline evidence, then names
the expected checkpoint and next action. It never selects an eligible issue,
claims work, changes a lifecycle label, creates a ledger record or worktree,
or invokes a worker. After review, `loop canary --apply --plan-file PATH`
rechecks the plan's exact evidence while holding the project lock before it
performs its bounded checkpoint action. A changed identity or evidence requires
a new preview.

`loop recover --issue-iid IID` is a targeted counterpart for one interrupted,
ambiguous, or otherwise unsafe retained item. Bare invocation creates a
redacted recovery plan without mutation. It is not required for an exact failed
current-source MR pipeline with one uniquely corroborated retained owner: an
applied ordinary loop activation transitions that owner directly to `repair`.
Apply requires both `--apply` and the reviewed `--plan-file`; it rechecks the
configured project, default branch, authenticated username, ledger revision
and state, lifecycle label, deterministic branch, retained-worktree HEAD and
cleanliness, merge-request project/source/target/source-SHA identity, and the
current exact-SHA pipeline before changing anything. It cannot advance another
IID. `--max-segments` and `--max-activation-seconds` retain the bounded
activation controls from `loop run`.

Plans bind the command and action to public service identity, configured
project and default branch, ledger revision, and the exact target, issue,
repository, merge-request, and pipeline evidence needed by that action. They
never contain secrets. Apply fails closed, with the safe next action, when the
plan is stale, tampered, for another identity, or no longer matches evidence.

### `recovery inventory` and `recovery resolve`

`recovery inventory` is an operator-facing, read-only reconciliation view. It
does not claim fresh work, change a lifecycle projection, create a worktree, or
apply a decision; `--apply` is accepted for uniform command invocation but has
no mutating effect. It corroborates lifecycle-projected GitLab issues with the
local ledger, deterministic branch and retained worktree, merge request and
exact-source pipeline, default-branch pipeline, configured project and
credential identity, and controller, service, and project-lock evidence. The
JSON and human views include aggregate `total`, `open`, `closed`,
`active`, `blocked`, `dependency_gated`, `human_action`, and `no_action`
counts. A detailed candidate identifies its active state,
`human_action_required`, bounded blocked cause, dependency explanation naming
open trusted prerequisites, current and intended prerequisite-label
projections, and exact next action.

The inventory classifies each retained item as legitimately waiting,
dependency-gated, safely retryable, superseded or duplicate, a closure
candidate, or requiring a specific human decision. It reports failed
default-branch and open-merge-request pipelines first, then stale running
items, safely retryable blocked items, dependency reconciliation,
awaiting-human items, and finally fresh ordinary work. Fresh work is reported
only; inventory never claims it. When trusted split prerequisites have cleared,
the inventory recommends an explicit retry rather than starting a child item.

`closed_no_action` takes precedence for a closed recordless split issue and a
closed `COMPLETE` record. Those terminal states are no-action even when their
retained worktree, head, merge-request, or pipeline evidence has drifted.
Closed nonterminal records still require human review. An untargeted inventory
collapses settled closed-item detail while retaining its aggregate counts;
`--issue-iid IID` returns that item's exact evidence.

`recovery resolve` creates an evidence-bound, reviewed per-issue plan by
default. An applied resolution requires `--apply` and the exact `--plan-file`.
It holds the project lock, rechecks the plan's evidence, rejects drift, and
returns a redacted before-and-after receipt. A plan contains exactly one
explicit choice for its target issue: `retry`, `close`, `reopen`, `relabel`, or
`supersede`. `close` and `supersede` require a reason and an evidence link or
replacement issue; `supersede` uses `--replacement-issue-iid`. `relabel` is
limited to a canonical lifecycle label and uses `--target-label`.

Neither command approves or merges, discards retained work, changes project
policy, treats issue text as authority, or claims fresh work. Credential expiry
or a configured-bot mismatch stops before mutation; use `agent-work credential
rotate` and follow issue #84's credential-recovery handoff before making a new
resolution plan. See [How to resolve factory issue recovery](../how-to/resolve-factory-issues.md)
for the reviewed operator procedure and examples.

`migration preview` inventories legacy evidence and proposes only safe,
idempotent records and label changes. `migration apply` requires a reviewed
preview file and `--apply`; it does not resolve ambiguous evidence. `--json` emits
the documented redacted JSON result. `--debug` adds local diagnostics without
credentials, issue text, prompts, or worker transcripts. Invalid usage exits `2`.

`upstream-finding preview` validates a manifest against an explicit upstream
GitLab target. Without `--apply`, it makes bounded GET-only requests and returns
`match` for an open issue with the exact reserved marker or `would-create` when
none exists; it never writes files or changes an issue, checkout, ledger,
labels, assignments, approvals, or merge state. With `--apply`, the command
first performs that same preview. On `would-create`, it repeats exact-marker
deduplication immediately before one possible create request. A newly found
marker returns `match`; otherwise a verified successful create returns
`created`. `--apply` is the sole mutation switch.

Success output remains redacted: JSON contains only `schema_version`,
`command`, `outcome`, the three fingerprints, `copied_path_count`,
`evidence_link_count`, and optional `matching_iid`. Human output contains the
same outcome, fingerprints, counts, and matching IID when present. Output,
errors, and debug diagnostics never echo manifest prose, project identity,
title, body, credentials, or evidence URLs. Invalid manifests, unreadable
credential files, access/API failures, and response-verification failures exit
`1` with sanitized diagnostics; invalid usage exits `2`.

`worker-result build` is a read-only worker helper. It reads one strict,
structured JSON request and the retained ledger and worktree for the supplied
issue, then derives the protocol-v2 identity, state, completed phase, and
duplicated audit fields. It validates the result with the same authoritative
controller and receipt-coherence rules used before a workflow transition. On
success, standard output contains exactly one compact delimited result:

```text
<agent-work-result>{...}</agent-work-result>
```

Human status and sanitized failures go to standard error. Without `--json`,
success emits the delimited worker envelope; `--json` emits the same compact
protocol object without delimiters for direct JSON consumers and does not relax
validation. `--debug` adds local sanitized diagnostics without credentials,
environment values, worker prose, prompts, or transcripts. `--apply` is accepted for
CLI consistency and is a no-op: this command never changes the ledger,
worktree, GitLab, or publication state. Success exits `0`; validation or
runtime failures exit `1`; argparse usage failures exit `2`.

The input file is one strict JSON object containing only worker intent. It
provides `workflow_lane`, `result_kind`, `receipts`, `change_summary`, and
`next_action`; it can also provide `merge_request_draft`, `error_code`,
`redacted_summary`, `split_plan`, and bounded `audit_summary` evidence
(`capacity`, `tests`, `findings`, and `corrections`). Unknown fields are
rejected. The controller derives protocol version, project and issue identity,
ledger revision, expected state and phase, completed phase, audit lane, role
receipts, changed paths, and the reviewer receipt list. In particular, do not
put the derived `identity`, `state`, `phase`, audit `lane`,
`role_receipts`, `changed_paths`, or `reviewers` fields in the input.

`phase_status` is controller-derived rather than worker input. Every recorded
ordinary `workflow` result receives `complete`, including `split_required`.
A `post-publication` result receives `complete` only when its result kind is
`ready_to_publish`; failed, actionable, and other non-success results remain
`rejected`. In particular, a successful post-publication assessment supplies
the required complete `$pipeline-performance-judge` receipt. It then advances
the verified exact-source handoff to `awaiting-human`; malformed, contradictory,
failed, or
actionable assessments retain their existing rejection and feedback handling
instead of advancing that handoff.

For a completed ordinary workflow, use `coding` or `content` as the lane and
provide the exact `WORKFLOW_RECEIPTS` set for that lane. The coding set is
`$architect`, `$tdd-red`, `$diataxis-writer`, `$agent-policy-writer`, `$tdd-green`,
`$tdd-judge`, `$nonfunctional-review`, `$diataxis-judge`, `$judge-quality`,
`$judge-test`, `$judge-cicd`, `$tdd-document`, `$cli-syntax-judge`,
`$filesystem-structure-judge`, `$agent-policy-judge`, `$license-checker`, and
`$tdd-finalizer`; all 17 are required for a coding
`ready_to_publish` result. The content set is `$architect`,
`$documentation-writer`, and `$documentation-finalizer`; all three are
required for a completed content `ready_to_publish` result. Every receipt starts with its exact skill name
and may include full multiline nested-role evidence. A conditionally
inapplicable role uses exactly
`$skill: not applicable — reason`. Arbitrary, omitted, duplicated, or invented
receipts cannot advance the workflow.

The 17-receipt coding set applies to new coarse `WORKFLOW` results. A legacy
recorded phased route remains compatible and finishes its recorded route.

`ready_to_publish` completes a coherent ordinary workflow and needs the exact
receipts, a successful audit (including capacity, and tests for coding), and a
merge-request draft when the retained work requires new publication metadata.
That draft has nonempty `title` and `description`. A controversial but safe
human handoff must be explicitly marked: its title begins `Draft:`, its draft
flag agrees, and its decision review records decisions and assumptions,
alternatives, risks, reviewer checks, unresolved findings and evidence, tests,
the correction-cycle ledger, and unchanged project-owned values. Existing-MR
remediation is the exception: it does not supply new publication metadata.

The other result kinds preserve their distinct controller transitions:

| Result kind | Use and required intent |
| --- | --- |
| `retryable` | A bounded transient outcome; include the applicable error code and redacted summary when available. The controller schedules ordinary retained-worktree recovery. |
| `needs_human` | A blocker requiring human authority or a decision; provide the bounded redacted summary, next action, and applicable error code. |
| `split_required` | An oversized fresh workflow; use `workflow_lane: unknown` and a `split_plan` of 2–25 ordered, independently mergeable slices. It needs no receipts, audit, merge-request draft, or error code. |

Validation and runtime failures retain only a canonical field-specific
diagnostic of at most 2,048 bytes and a stable reason code. Protocol version 1
is rejected; fresh workers use version 2, including `split_required`. Failures
never retain
request values, worker prose, prompts, chain-of-thought, credentials,
environment values, or unbounded exception output. Protocol failures for
issues #40 and #78 remain retryable: their
retained worktrees stay in place for ordinary recovery. This does not make a
claim about the underlying cause of issue #80's retry exhaustion. Keep the
helper's single output separate from surrounding human prose: return its
delimited standard output unchanged to the controller, and put any explanatory
status outside that envelope.

### Worker validation diagnostics

Worker protocol validation reports one safe diagnostic when the builder input
is malformed, the final worker envelope is malformed, or a controller-derived
invariant fails. The diagnostic contains only its stable origin, an allowlisted
field path, and a stable corrective reason. It never includes an unknown
field name supplied by the worker, any supplied value, raw output, prompts, or
credentials. It is one line of at most 2,048 UTF-8 bytes.

The controller carries the same safe diagnostic in the condition history,
status, recovery inventory, and GitLab handoff. On a repeated automatic
failure in one coarse phase with the same error code and diagnostic, it blocks
the item and tells the operator to run `agent-work loop retry --issue-iid IID
--apply` after correcting the condition. A changed error code, diagnostic, or
phase is changed evidence and follows the existing bounded retry path.

### Upstream-finding manifest v1

The required YAML top-level mapping has exactly these fields; unknown fields,
YAML tags, anchors, aliases, duplicate keys, non-UTF-8 input, and documents
larger than 64 KiB are rejected. Scalars are strict: quoted or coercible
numbers and booleans do not satisfy typed fields.

| Field | Required v1 value |
| --- | --- |
| `schema_version` | Literal integer `1`. |
| `generated_project` | Exact mapping: public bare DNS `host`, positive integer `project_id`, and safe relative `path_with_namespace`. |
| `copier` | Exact mapping: credential-free network Git `source` and lowercase 40-hex commit `revision`. |
| `finding` | Exact mapping described below. |

`finding.copied_paths` contains 1–100 safe relative paths; its duplicates and
order are normalized for the semantic fingerprint. `reproduction` contains
1–20 ordered, nonempty bounded text steps. `expected_behavior`,
`observed_behavior`, and `validation_evidence` are nonempty bounded text.
`priority` is `low`, `medium`, `high`, or `urgent`; `severity` is `low`,
`medium`, `high`, or `critical`; and `scope` is `generated-project`,
`template`, or `both`. `evidence_links` contains 0–20 exact mappings with
`visibility: public` and a credential-free public HTTPS `url`; local, private,
ambiguous, insecure, userinfo-bearing, query-bearing, and fragment-bearing
URLs are rejected.

The three lowercase SHA-256 fingerprints use canonical stable JSON. The
project fingerprint derives from normalized project identity; the revision
fingerprint adds normalized Copier source and exact revision; the semantic
finding fingerprint adds sorted, deduplicated paths, ordered reproduction,
expected and observed behavior, and scope. Priority, severity, validation
evidence, and evidence links are mutable metadata and do not affect the
semantic fingerprint. NFC normalization and harmless surrounding whitespace
produce the same fingerprints.

The exact reserved marker is
`<!-- agent-work-upstream-finding:v1 project=<project_fingerprint> revision=<revision_fingerprint> semantic=<semantic_finding_fingerprint> -->`.
It is the remote recovery evidence; this standalone command creates no durable
local ledger or state machine. A preview returns `match` when an open issue has
that exact marker and `would-create` otherwise. An applied invocation that
finds the marker at either deduplication point returns `match`; a verified new
issue returns `created` with its IID.

When applying, the command sends one deterministic redacted title and bounded,
evidence-backed body containing exactly one canonical marker and explicitly
sets the issue non-confidential. The body may include validated finding evidence,
copied paths, reproduction, priority, severity, scope, and canonical
fingerprints. It may include evidence links only when their manifest entries
declare `visibility: public`; it omits raw generated-project host, ID, and path,
raw Copier source and revision, credentials, and injected reserved markers.
The command does not label or assign the issue.

The returned create response must identify the configured numeric upstream
project, a positive IID, open and non-confidential state, the exact sent title,
and a description with exactly one canonical marker. Its URL must be the
canonical HTTPS issue URL for the verified upstream host, project path, and IID.
Any mismatch is a sanitized failure. A transport, nonzero, or malformed create
response can be uncertain, so the invocation never retries its POST. Rerun
explicitly after such a failure: the marker scan either recovers the issue as
`match` or, only when no exact marker exists, makes at most one new create.
This immediate repeat-deduplication and one-POST bound does not promise to
resolve concurrent cross-host races.

Valid preview, match, and verified-created outcomes exit `0`; sanitized
validation, access, API, and response failures exit `1`; argparse usage exits
`2`. A newly created issue is open, non-confidential, unlabelled, and
unassigned. It enters the explicit upstream project's normal guarded factory
issue workflow only when that upstream project has one enabled; neither the
generated/source project nor the upstream template project or factory approves
or merges. Humans retain approval and merge authority.

`AGENT_WORK_BASE_BRANCH` is required and must name a valid configured
`agent/...` service branch; the setup uses the canonical `agent/work-base`
branch. The runner validates it before it claims an issue, changes a label, or
creates a ledger record; an omitted value fails the command instead.

## Systemd wrapper environment

The systemd wrapper requires a bare `GITLAB_HOST` and positive numeric
`GITLAB_PROJECT_ID`; malformed or missing values exit `2` before execution. The
mode-600 service environment file stores those public identity values and the
recorded executable paths, never GitLab credentials. A configured
`AGENT_WORK_DIRENV` requires a regular `${AGENT_WORK_CHECKOUT}/.envrc` and an
executable absolute path. It may provide directory-scoped credentials, but the
wrapper invokes `/usr/bin/env` with the captured service host and project ID so
that `direnv` cannot replace the configured target. Setup runs the wrapper's
preview under a scrubbed `env -i` environment before enabling the timer.

Before it launches the worker process, the controller removes inherited
parent-checkout `direnv` bookkeeping: `DIRENV_DIFF`, `DIRENV_DIR`,
`DIRENV_FILE`, `DIRENV_IN_ENVRC`, and `DIRENV_WATCHES`. It preserves ordinary
non-bookkeeping configuration such as `DIRENV_CONFIG`, the captured public
GitLab identity, and the scoped `GITLAB_TOKEN` exception; it excludes other
credential-shaped variables.
Nested workflow commands may later run `direnv exec .` in a retained worktree
that has no `.envrc`; without stale parent-checkout metadata, that command does
not unload the permitted scoped token.

Initial setup emits the deterministic current service-environment contract.
Repository upgrades must use `service-environment migrate` for a stale or
incomplete installed contract before an operator starts or enables the timer.
A missing installed contract requires the supported setup procedure to recreate
it; manual reconstruction is outside the supported upgrade path.

## Git authentication contract

`GITLAB_TOKEN` is the configured directory-scoped GitLab credential. The
repository-owned Git credential-protocol helper translates it to GitLab HTTPS
Basic credentials only when Git requests them at runtime. The serial
controller/service and ordinary `$git-flow` publication use that same helper.
The remote remains a credential-free HTTPS URL; the helper does not rewrite or
persist the remote.

The token and its derived Basic-authentication header must not appear in a
remote URL, persistent Git configuration, the service environment file, command
arguments or process reports, logs, diagnostics, ledgers, or retained records.
Git prompts are disabled. The route clears inherited credential helpers and
hostile ambient credential configuration so they cannot override the helper.

The API check and Git check are independent. The API route continues to target
the configured bare `GITLAB_HOST` and positive numeric `GITLAB_PROJECT_ID`.
The Git readiness check uses the same configured token through the helper and
does not mutate the remote. A missing token is a configuration failure before
remote mutation; an invalid or expired token is an authentication failure; and
an authenticated identity without required push scope is an authorization
failure. Known temporary transport failures are retryable where the calling
operation permits retry. Diagnostics report only that safe classification.

This contract adds no CLI and does not change GitLab API semantics, branch
protection, roles, token creation or rotation, or the human-only approval and
merge boundary.

## Lifecycle

GitLab is the shared queue projection; the local ledger is the transaction
record. Project-level loop enablement authorizes unattended selection. Existing
active or resumable work always wins. For a fresh issue, the runner first
filters to open, non-confidential issues with no lifecycle projection, ledger
record, or open split prerequisite. It then ranks exact `agent-priority::high`
first, every issue without a recognized exact priority label next (including
unknown priority-like labels), and exact `agent-priority::low` last. Within a
rank it compares absolute timezone-aware `created_at`, then numeric IID; it
does not trust API ordering or query issue notes/comments. Malformed creation
time evidence fails safely. Labels do not retroactively change existing issues.

Project setup provisions the lifecycle and manual-tracker label specifications
below with their fixed colors and plain descriptions. Humans can assign high to
expedite fresh work, remove either priority label for normal ordering, or
assign low to defer it.

| GitLab label | Ledger state or outcome | Meaning |
| --- | --- | --- |
| `agent::running` | preparing, executing, retry-wait, publishing, observing, or repair | Work is active or safely resumable. |
| `agent::awaiting-human` | awaiting-human | An exact MR, its named source commit, required receipts, and one factory-authored actionable issue handoff await human review. |
| `agent::blocked` | blocked | A human must take the recorded next action. |
| `agent::failed` | failed | The runner could not safely reconcile or record an exceptional condition. |
| `agent::needs_repair` | needs-repair | A legacy or manually checkpointed repair awaits exact recovery; ordinary exact pipeline failures do not enter this state. |
| `agent::repair_failed` | repair-failed | An automatic or explicitly reviewed repair attempt failed or its exact evidence changed; it retains the evidence needed for safe follow-up. |
| `agent::suggest_close` | suggest-close | Corroborated duplicate, superseded, already-satisfied, or no-longer-applicable evidence recommends human closure. |

`agent-work::manual` is an exact marker for an open issue that a human keeps as
a manual tracker or recovery checklist. It is deliberately outside the
`agent::…` lifecycle-label namespace. Fresh selection ignores a marked issue;
removing the exact marker returns an otherwise eligible open issue to normal
fresh selection. The marker is not a lifecycle projection: it creates no
ledger record or worktree, cannot become orphan-reconciliation input, and does
not change prerequisite projection or authority. Do not use it as a lifecycle
or error state, or to bypass a prerequisite.

`agent::needs_repair` uses `#D4A72C`, `agent::repair_failed` uses `#C91C00`,
and `agent::suggest_close` uses `#6F42C1`. These three labels are mutually
exclusive with each other and with `agent::running`, `agent::awaiting-human`,
`agent::blocked`, and `agent::failed`; none is eligible for fresh selection.
`agent::blocked` remains the projection for an authority, safety, or unresolved
evidence decision, and `agent::failed` remains exceptional or unrecoverable.

| Queue label | Fresh-work rank | Control |
| --- | --- | --- |
| `agent-priority::high` | 0 | Human expedite |
| no recognized exact value | 1 | Normal; remove a scoped label |
| `agent-priority::low` | 2 | Human defer |
| `agent-work::manual` | excluded | Human-maintained tracker or recovery checklist; remove to restore ordinary eligibility. |

The durable sequence is `preparing → executing`; execution may enter
`retry-wait`, resume execution, or produce `publishing`. Verified publication
enters `observing`, where the controller waits for the exact-SHA terminal state
before a fresh Pipeline Performance Judge assesses it. A healthy result writes
the actionable handoff for the exact MR source commit and only then finishes at
`awaiting-human`; a concrete repository-actionable problem is recorded as a
deduplicated ordinary issue for later normal selection, not direct repair of the
published MR. A human-blocked condition becomes `blocked`; after the
recorded action is complete, an operator explicitly retries that IID. Human
merge or closure ends an awaiting-human record.

### Awaiting-human issue handoff

Every durable `awaiting-human` projection has one factory-authored issue note
for its exact merge request source commit. The note links to that merge request,
names the commit, and asks a person to review that version. If it is acceptable,
the person may approve and merge it; otherwise, the person leaves actionable
general or inline merge-request feedback. Only a human may approve or merge.
The runner does not auto-assign a reviewer because no trusted configured
reviewer identity is part of this contract.

The note is identified by a deterministic marker composed from the configured
project, issue, merge request, and source SHA, and the runner accepts it only
when its author is the configured factory identity. Reconciliation therefore
does not duplicate a note for the same source SHA, but writes a new handoff when
the verified source SHA changes. In an applied activation, a record already
projected as `awaiting-human` is still repaired when that exact handoff is
missing. A transient note-write failure leaves the record on durable retry and
prevents a new `agent::awaiting-human` label. If a restart finds the note already
written, it reuses that note and repairs the label; exact merge-request and
worktree evidence remain required throughout.

A newly selected issue attempts its complete applicable repository workflow in
the same activation. A durable record resumes from its coarse `WORKFLOW`
boundary and durable worktree; the first rollout does not add a sidecar
checkpoint for every role or phase. Existing legacy phase-ledger values finish
their recorded route; new work uses `WORKFLOW`. Within that session, Architect
hands the approved design concurrently to Red and the Diátaxis Writer. When
portable agent policy is in scope, the conditional Agent-policy Writer joins
them. Red exclusively mutates `tests/`; the Diátaxis Writer exclusively mutates
`docs/`; and the Agent-policy Writer exclusively mutates `AGENTS.md`,
`.agents/**`, and their declared portable `template/` mirrors. Architect waits
for every applicable receipt, or its exact not-applicable record, before Green
starts. Green reconciles the documentation draft with the final interface but
never edits agent-policy paths or their mirrors. It then runs the serial Phase-4
TDD Judge against its staged tree. The Judge's unchanged tree OID and
supported-version JUnit digests and counts are prerequisites for every
applicable concurrent Phase-5 reviewer, including `$license-checker`, against
the same stable post-Green diff. Green consolidates every initial finding.
Policy-path corrections return through the still-live Architect to the
Agent-policy Writer; Green cannot repair policy. Every changed candidate follows
the fresh Judge, applicable review, and Finalizer sequence before Architect
makes the final decision.

### Preselection pipeline gate

Before selecting fresh work, both an applied user-directed `agent-work loop
run` and the systemd wrapper's next-activation path inspect pipeline evidence
for the configured numeric project. They inspect the newest pipeline for the
default branch and the newest pipeline for every open merge request, binding
each merge-request observation to that merge request's current source SHA.
The controller does not infer merge-request status from a pipeline belonging to
an older source commit.

Existing active, safe resumable work remains the first priority. Only after it

When an ordinary unpinned activation sees one non-`retry-wait` active record,
it resumes that one record and leaves every retry-wait record unchanged. With
no such record, it selects only the earliest due retry by `(retry_at, issue
IID)`; future or undated retries remain queued. Multiple non-retry active
records still fail closed as concurrent ownership ambiguity.
has been reconciled may the pipeline gate decide whether ordinary fresh-issue
selection is safe. A successful pipeline clears the corresponding observation;
`created`, `waiting_for_resource`, `preparing`, `scheduled`, `pending`, and
`running` are observation waits and yield without selecting a fresh issue.
Missing, malformed, inaccessible, identity, or validation pipeline evidence
fails closed rather than allowing unrelated work to proceed.

A corroborated failed pipeline takes precedence over ordinary issue selection.
An exact failed merge-request pipeline with one retained owner having exact
configured project, merge-request, branch, source-SHA, and worktree evidence
is an eligible automatic repair. The controller runs at most one repair
workflow in an activation. It considers exact repair candidates in deterministic
order and scans past an item in a scheduled repair backoff or quarantine to
another eligible exact repair before considering ordinary work. A failed
automatic repair records a 20-minute durable, per-item backoff. After three
automatic repair attempts, it is quarantined or deferred for human review and
is not selected automatically. Status and preview retain the item, its attempt
count, timing, evidence, and next action. A failed default-branch pipeline, or
a failed merge-request pipeline without one safe exact owner, records an
actionable human handoff or block instead. It does not claim an unrelated issue,
invent a repair owner, or weaken the human-only approval and merge boundary.

Before it starts an eligible repair, the controller reads a bounded job
inventory for the already verified project, merge request, source SHA, and
pipeline. It includes retry attempts but ignores failed jobs marked
`retried=true`, because those attempts have been superseded. Exactly one current
failed job (`retried=false`) must remain. The controller fetches only that job's
trace, removes control sequences and secret-like content, and caps the retained
diagnostic. It validates typed evidence that binds the project, merge request,
source SHA, pipeline identity, pipeline SHA and status, and selected job's ID,
name, stage, status, failure reason, and trace provenance and status.

The controller persists that validated bounded evidence before invoking the
repair workflow. A restart reuses it after validation, and the workflow prompt
receives the same bounded diagnostic. Raw job traces never enter the ledger,
status report, or workflow prompt. A wrong or drifting identity or status,
multiple or no current failed jobs, an empty trace, or a schema or API failure
stops repair without starting a worker. The controller does not retry or cancel
jobs or pipelines as part of this process.

Post-publication pipeline assessment is a fresh context after terminal
exact-SHA evidence. It does not edit the published MR or directly enter a
repair session. A concrete repository-actionable finding creates or reuses a
deduplicated issue, which later normal reconciliation may select for a new
applicable workflow; unverified evidence produces no repository work.

The ordinary loop, canary checkpoint, and targeted recovery use the same
recovery order: the pipeline and default-evidence gate comes first; an exact
failed-MR pipeline with one corroborated retained owner is considered for one
eligible repair workflow; then comes the sole active resumable retained record;
then an
exact-IID reviewed reclassification for exceptional retained work; then waits;
and only then may ordinary `loop run` select fresh work. A repair that fails,
or whose exact evidence changes, retains the evidence, failure, timing, and
worktree needed for safe subsequent handling. Scheduled repair backoff and
automatic-repair quarantine are item-local eligibility outcomes, not
`retry-wait` and not global blockers. Existing blocked records may be
reclassified only one IID at a time with live corroboration. Bulk status does
not corroborate each item; targeted status may. Two active retained records, a
competing owner, or a higher-priority owner is an identity conflict: the
command fails closed and reports the exact safe next action.

### Durable scheduler states and liveness

This is the complete durable-state lookup. One item's state must not prevent
consideration of an unrelated eligible item. Only configured project identity
or credential failure, held or ambiguous project-lock ownership, corrupt shared
durable state, or a truly ambiguous cross-item identity stops the activation.
Those cases are project-wide because the controller cannot safely identify the
project or the sole item to advance.

| State | Representative evidence | Scheduler result |
| --- | --- | --- |
| `PREPARING` | Matching selected record and clean retained worktree | Resume or repair this item. |
| `EXECUTING` | One recoverable workflow owner | Resume or repair this item. |
| `RETRY_WAIT` | Due `retry_at`, with no immediate owner | Resume earliest due retry; defer a future retry and scan on. |
| `PUBLISHING` | Exact retained branch and pending publication identity | Resume or repair publication for this item. |
| `OBSERVING` | Exact current source SHA and pipeline evidence | Observe or resume; nonterminal evidence waits and an exact safe failure can repair. |
| `REPAIR` | One uniquely owned failed current-source pipeline | Run or resume at most one repair workflow in this activation. |
| `NEEDS_REPAIR` | Legacy or reviewed recovery record | Defer; expose exact-IID reviewed recovery; scan on. |
| `REPAIR_FAILED` | Per-item repair timing and attempt evidence | Resume only when due and safe; otherwise defer or quarantine locally and scan on. |
| `SUGGEST_CLOSE` | Corroborated closure recommendation | Skip; expose the human closure decision. |
| `BLOCKED` | Recorded human action or unresolved item evidence | Skip automatic work; expose the exact action. |
| `AWAITING_HUMAN` | Verified review handoff for an exact MR source SHA | Skip automatic work; preserve the human handoff. |
| `FAILED` | Item-local exceptional failure and next action | Skip automatic work; expose recovery action. |
| `COMPLETE` | Closed issue or completed record | No action. |

The controller advances at most one active owner and at most one repair
workflow in an activation. A stable no-progress fingerprint binds the item,
state and phase, exact branch/MR/pipeline identity where applicable, and the
resulting action. A materially changed fingerprint or closed earlier outcome is
progress. Unchanged evidence and fingerprint are no progress, so the item stays
durably deferred, quarantined, or operator-visible instead of consuming later
activations. Status and preview report the bounded reason, attempt information,
timing, evidence summary, and next action. Preview is non-mutating and uses the
same eligibility logic without claiming work or changing GitLab, ledger,
worktrees, branches, or services.

Retry counts, repair budgets, delay bounds, priorities, and lock rules are
trusted controller constants. Untrusted issue or merge-request text cannot set,
reset, or otherwise influence those control values.

For failed-pipeline inventory, the controller skips an owner that is blocked,
deferred, quarantined, retry-exhausted, awaiting human action, or
dependency-gated. It continues to another exact eligible repair, then ordinary
eligible work when the remaining gate permits it. One locally ineligible owner
does not produce a global `pipeline_blocked` result. Only a failed default-branch
pipeline or genuinely ambiguous cross-item owner remains a project-wide stop.

Reconciliation may accept ordinary branch advancement only through a narrow
fast-forward predicate: configured numeric project identity; retained MR
identity and URL when recorded; matching source branch; configured target
project and default branch; clean retained worktree; exact retained branch and
`HEAD` equal to the MR source SHA; proof that retained SHA is an ancestor of MR
SHA; and no competing identity. A failed predicate quarantines the retained
item locally while inventory scanning continues. It never chooses an MR by
recency or issue text.

`agent::suggest_close` is a recommendation only. It requires structured,
corroborated duplicate, superseded, already-satisfied, or no-longer-applicable
evidence and never closes an issue. Only human closure reconciles the record to
`COMPLETE` and clears that label. The runner never approves, merges, or closes
an issue.

Each timer activation acquires the single-project lock and reconciles intent
and observed evidence. At the start of an applied activation, before it resumes
or selects open work, it obtains one bounded deterministic IID-ordered inventory
of closed issues carrying lifecycle labels. For each inventoried issue with an
existing ledger, it durably normalizes the record to `COMPLETE` and clears
transient retry, intent, error, worker-exit, and pending-feedback state before
removing lifecycle labels through the existing authenticated GitLab identity
boundary. For an issue without a ledger, it removes stale lifecycle labels
without creating a ledger record, blocked or complete state, journal, or
worktree. A ledger-write failure leaves the label in place; a recorded issue's
label-removal failure leaves the canonical `COMPLETE` record and stale label
for a later activation, while an orphan failure leaves only its stale label.
Earlier IID successes remain reconciled. This closed-issue housekeeping does
not consume `max_segments` or the open-work budget, invoke a worker, or perform
repository, branch, worktree, push, or merge-request operations. Open orphan
projections retain the ordinary blocked recovery path. After that inventory,
the activation advances at most one selected or resumed open issue. Recovery
applies at the worktree and coarse workflow boundary, not at every internal
agent handoff.

Each Python activation advances at most one issue and retains its 10,800-second
active-work budget. An applied `loop run --apply` stays in that activation when
default-branch or exact current-MR pipeline evidence is nonterminal. It polls
the same evidence after 5, 10, then 20 seconds, capped at 30 seconds, and
resumes the state machine promptly when the evidence becomes terminal. The
systemd wrapper continues only after `awaiting-human`, `blocked`, `failed`, or
`complete`: it validates that report, makes one fresh read-only eligibility and
preselection-pipeline check, and begins another activation only for `eligible`.
Between issues it re-queries GitLab and fast-forwards the clean service base;
this bounded housekeeping is outside the per-issue budget. A sole exact failed
current-source pipeline activates `repair` and runs its workflow in the same
applied activation. A future `retry-wait`, `time-limit`, `segment-limit`, human
or safety block, or missing, failed, or unrecognized evidence remains a
handoff. `idle` means
there is no eligible or resumable work. `OnUnitInactiveSec=20min` schedules a
newly arrived-work check only after that genuine idle service exit; it is not
the cadence for active pipeline polling. The activation deadline and the
systemd outer timeout remain in force. The loop is serial per host and project,
not multi-host coordination.

## Ledger, worktrees, and audit

State is below the XDG state directory, keyed by configured GitLab host and
numeric project ID. Each `loop status` ledger item identifies its durable
worktree path. Every active issue has one atomic, restrictive-permission ledger
record and durable worktree. Version 2 adds the bounded issue title;
activation, latest-meaningful-event, and completion timestamps; main, source,
publication, and merge-request identities; and bounded retry and failure
history separated from the current actionable error. When the fixed automatic
retry budget is exhausted, the current condition is `retry_exhausted`; the bounded
condition history retains the latest sanitized underlying condition code and
detail. Human and JSON status communicate that exhaustion together with the
retained actionable cause, and the safe issue lifecycle outcome is
`agent::blocked`. It also retains a typed,
bounded final AI workflow summary: selected lane, capacity decision, nested
roles, changed paths, tests and JUnit evidence, reviewers, and findings and
corrections. The runner corroborates its canonical changed-path list against
the durable worktree and requires the remaining summary entries to be present
in the validated worker receipts; it does not independently verify AI claims.

The ledger is the only durable concise audit record. It never stores prompts,
chain-of-thought, credentials or environment values, issue bodies or comments,
transcripts, or unbounded output. Old records remain readable; evidence missing
from a legacy record is `unknown`, and a record receives current fields on its
next persist rather than by inventing historical values.

The application also writes its local lifecycle log to `agent-work.log` in
that XDG project-state directory. This is application-owned evidence alongside,
not a replacement for, the systemd service journal.

Intent is persisted before an external mutation and observed evidence after it,
so reconciliation can safely complete interrupted idempotent operations. A
dirty durable worktree remains for its next workflow attempt. Recovery stashes are
inventoried only: the runner never applies or drops them. Ambiguous worktrees,
branches, labels, or stashes become blocked for human review. A corrupt record
is blocked where reportable, and failed only where it cannot be safely recorded.

A clean, unpublished interruption before worker invocation has a narrow
normalization path. It is eligible only when the open issue and lifecycle label,
deterministic branch and worktree, and recorded baseline all agree; the
worktree is clean at that baseline; and there is no MR, publication, draft,
intent, feedback, receipt, or decomposition evidence. The runner retains every
artifact, converts the record to `blocked`, and gives actionable retry guidance.
It never deletes a ledger, branch, or worktree.

## Status projection

`loop status` projects the durable record into an operational state; it does
not create a second audit store. The raw ledger state and coarse phase remain
visible alongside the projection.

| Projected state | Durable condition |
| --- | --- |
| `selected` | Claimed and preparing before a source SHA exists. |
| `running` | Any remaining active or retry state, including `retry-wait`. |
| `awaiting-human-review` | Verified handoff is awaiting human review. |
| `blocked` | Blocked or raw failed state. |
| `complete` | Completed record. |

For each item, human output provides one concise line answering whether work is
running, what changed, the current failure or condition, and the next action.
It includes the issue, projected and raw state, phase, identity facts, and
worker-exit evidence when present. Detailed validation evidence (tests,
reviewers, and audit receipts), timestamps, conditions, and corroboration are
available in JSON.

JSON output is a compact, versioned `CommandReport` version 2. Its status
records contain the same identity, projection, current-versus-history, bounded
audit, and observation information as the human view. A status report includes
the total matching records and truncation metadata; only the unfiltered list is
capped at 100 records. Consumers must treat a missing, stale, failed, or
mismatched observation as that condition, not as current truth.

Live corroboration is best effort and read-only. The current implementation
uses injected GitLab, repository, and clock boundaries to inspect the issue and
retained worktree; the project-wide process field is read once from the passive
activation-owner marker. It is `active` only when the marker PID is verifiably
alive, `stale` when it is verifiably dead, `missing` when no marker exists, and
`unknown` for malformed or unverifiable legacy evidence. Status never acquires,
creates, or writes a lock. Configured project identity verification remains mandatory. A failure to
obtain one source does not abort the whole report: status reports `unknown`,
`missing`, or `mismatch` for that source.

## Worker protocol and publication

### Systemd wrapper refresh contract

The systemd wrapper requires recorded absolute paths for Git, `glab`, `jq`,
`flock`, UV, and the selected agent. It acquires a per-worktree advisory lock,
verifies the configured GitLab project and checkout identity, and disables
repository hooks for every refresh command. Preview uses `git ls-remote` to
require that `HEAD` is the remote default-branch tip without fetching or
writing a tracking ref. Applied execution fetches only that default branch and
fast-forwards it; any failure blocks UV. Checkout-local Git configuration is
strictly allowlisted, target filter attributes are rejected, and submodule
recursion is disabled. The shared Git authentication contract controls
credential handling instead of ambient helpers or persisted credentials. After
first deployment, stop the timer,
manually fast-forward once, preview/canary, then restart it; later applied
activations self-refresh.

The oneshot service provides a bounded outer envelope for that work:
`TimeoutStartSec=190min`. It reserves the approved 10,800-second (180-minute)
worker budget for the applied `agent-work` invocation and leaves a 10-minute
safety margin for wrapper checkout, GitLab, publication, and reporting work.
Non-worker Git, GitLab, `direnv`, and bootstrap calls have a finite 120-second
housekeeping deadline; the applied worker instead receives and remains bound
by the time remaining from its 10,800-second budget. On outer-envelope expiry,
systemd sends TERM to the whole service control group, allows a 30-second
TERM-to-KILL grace period, and then kills any survivors. It records
`Result=timeout` and the relevant sanitized wrapper diagnostics in the unit
journal. The durable ledger and retained issue worktree are not removed;
normal retry and human diagnosis use that evidence.

A workflow-owning worker returns a versioned, redacted envelope for its
expected ledger transition. Its result is `ready_to_publish`, `retryable`,
`needs_human`, or `split_required`, with the coarse completed state, receipts,
diagnostic, and next action. The trusted runner validates it against the
ledger, configured numeric project, Git evidence, and required workflow
receipts before changing state.
For coding work, the final audit names every mandatory role: Architect, Red,
Diátaxis Writer, the conditional Agent-policy Writer, Green, all applicable
reviews including the initial `$license-checker`, and Finalizer. The finalizer
requires a current approving license receipt for the final in-scope diff. On
correction, the checker reruns only if maintained files in its checked scope
changed; a violation cannot be waived or marked not applicable. Policy-path
findings return through Architect to the Agent-policy Writer, not Green.
Content work retains its separate role set.

### Live-agent capacity launch contract

Every newly launched workflow-owning worker checks live-agent capacity before
it delegates a nested role or mutates the retained worktree. The evidence must
come from trusted frontend or runtime session metadata for that active launch.
Repository data, issue and task text, user or provider configuration, service
environment values, and retained ledger or audit values are not capacity
evidence.

The admission check records the exact applicable manifest topology and its
widest concurrent row. For coding, Main owns Architect; Architect owns the
Phase-2 writers, Green, and Finalizer; Green owns Judge and every applicable
Phase-5 reviewer; and Judge owns Non-functional Reviewer. A conditional role
reduces this topology only when its condition is demonstrably false.

The check reports one coherent set of values:

| Value | Contract |
| --- | --- |
| Lane | `coding` or `content`, according to the whole proposed change |
| Topology and widest row | The applicable ownership tree and the exact row that sets the requirement |
| Required total | `12` for fully applicable coding; `3` for content |
| Available total | Current total live slots from trusted launch metadata, or `unknown` |
| Shortfall | `max(required - available, 0)`, or `unknown` when availability is not trustworthy |
| Retained-object admission | Global object ceiling, current use, and fresh-object headroom for the planned topology |
| Owner admission | Direct-child and descendant headroom for every planned owner |
| Handle plan | A fresh handle for each role, or a verified same-owner, same-tier, non-overlapping reuse plan that preserves skill, parentage, concurrency, and receipt |
| Decision | Run only when current evidence is internally consistent and available is at least required |

The conditional Agent-policy Writer makes the widest Phase-2 row five live
slots: Main, Architect, Red, Diátaxis Writer, and Agent-policy Writer. The
nine-reviewer Phase-5 row remains wider, so the fully applicable coding
requirement stays 12. Architect and Green are fresh per issue unless the
verified handle plan proves that reuse is safe.

Evidence is fresh only when it describes the active worker session. Evidence
from an earlier invocation is stale even when that invocation used the same
issue, retained worktree, controller process, service, or frontend
configuration. Conflicting current observations are inconsistent and make the
available total and shortfall unknown. A changed, internally consistent
observation for the active launch replaces an earlier observation; values are
never averaged or recovered from durable state.

Absent, stale, inconsistent, or insufficient live-slot, object, or owner
evidence returns `needs_human` before nested delegation, fanout, or worker
mutation. The handoff names the lane, topology and widest row, required total,
available total or `unknown`, shortfall or `unknown`, and an actionable remedy
for the active frontend. The runner preserves the retained worktree and
existing recovery evidence. It does not serialize a parallel phase, weaken a
gate, change a model tier, or infer authority from the capacity failure.

Ordinary interactive launches, retained-worktree retries, and systemd-started
workers use the same contract. Each new session rechecks the runtime metadata;
a prior successful audit cannot authorize it. A successful worker result
retains the checked lane, topology, widest row, live-slot and object evidence,
owner headroom, any reuse plan, and run decision as one coherent capacity audit.
See [Configure agent
capacity](../how-to/configure-agent-capacity.md) for the operator procedure.

### Intake sizing contract

At the start of a fresh coarse `workflow` session, the trusted wrapper first
delegates the live Architect. Before Red, the documentation writer, lane
selection, or any worktree mutation, Architect makes one semantic sizing
decision. A suitable issue has one independently reviewable and independently
mergeable delivery outcome that can reasonably complete every applicable
workflow phase inside the effective bounded worker budget. The decision uses
behavioral capability boundaries, dependency and ordering boundaries, and
workflow breadth; numeric content thresholds such as line count do not decide
suitability.

Suitable work continues through the existing coding or content workflow. An
oversized issue, or one containing multiple separately deliverable
capabilities, returns one protocol-version-2 envelope with these values:

| Field | Value |
| --- | --- |
| `result_kind` | `split_required` |
| `phase_status` | `complete` |
| `completed_phase` | `workflow` |
| `workflow_lane` | `unknown` |
| `split_plan` | 2..25 ordered slices with stable ASCII keys and lower-ordinal dependencies |

This route requires no receipts, merge-request draft, or error code. The worker
makes no worktree or GitLab mutation, creates no child issues, and executes no
proposed slice. The controller handles a valid split result directly in its
executing segment. For every proposal, it searches for the exact durable child
marker and reuses that child when found; otherwise it creates the child with a
full description that links to the parent and its dependencies. After all
children exist, it posts one parent note and closes the parent. It does not
persist a decomposition lifecycle state, plan, or progress
ledger, checkpoint, or special queue state. A retry reruns the complete
workflow and repeats the exact-marker searches, so a partially completed prior
attempt does not create duplicate children. Children enter ordinary eligibility
and each uses the normal workflow and merge-request path when its lower-ordinal
dependencies have closed. `LedgerRecord.schema_version` and
`WorkerResultEnvelope.protocol_version` are `2` for newly persisted records
and every fresh worker envelope, including `split_required`. The reader accepts
legacy v1 ledger records only; worker output is always v2.

### Split prerequisite-label projection

Validated split markers and the live state of their referenced issues are the
only authority for a child's dependency gate. The runner projects that state
for operators with non-scoped `agent-prereq_<IID>` labels. A projection label
never makes an issue eligible or ineligible.

For every open split child, the intended labels are exactly one
`agent-prereq_<IID>` label for each currently open trusted prerequisite. The
runner orders prerequisite IIDs numerically when it calculates and reports the
set. It creates or reuses each project label idempotently with its stable color
`#6F42C1` and the plain description `Open split prerequisites for agent-work
issue dependency display.`, then adds missing labels and removes stale ones. A
label is removed when its prerequisite closes, the trusted marker dependency is
removed, or the dependent issue closes. These repairs are idempotent and a
partially completed remote update remains retryable.

Applied reconciliation creates or normalizes the canonical project-label
definitions, removes exact legacy `agent::prereq_<IID>` labels from affected
issues, and adds the complete numerically ordered canonical set. It deletes an
exact legacy project-label definition only after every issue-label transition
succeeds. If a transition fails, the definitions remain for the next safe,
idempotent retry. Reconciliation preserves unrelated labels and lifecycle
labels. Preview reports the intended change without writing it.

Immediately before each worker execution, the runner retrieves that issue's
notes through its bounded GitLab retry path. It excludes system notes and both
marked and legacy runner outcome notes, while retaining ordinary human and bot
discussion. The notes are live, untrusted worker context only: they are never
persisted in the ledger or lifecycle log, and cannot change issue selection,
remediation, lifecycle, or authority.

The runner renders the retained notes as untrusted JSON capped at 32 KiB. When
the notes exceed that bound, it keeps the newest useful retained notes and
orders that retained subset chronologically. Retrieval or validation failure
uses the existing GitLab retry path rather than supplying incomplete or
unvalidated note context.

Every new-MR work kind, including protected policy work, may enter
`ready_to_publish` only when the envelope returns, or the ledger already
retains, a non-empty `merge_request_draft`. Existing-MR remediation may omit a
draft because it publishes only to the verified existing MR. A legacy ordinary
`PUBLISHING` or `READY_TO_PUBLISH` record with `source_sha`, no draft, and
`intent=null` is recovered side-effect-free to its lane finalizer; the runner
never synthesizes a draft from issue text.

Before retry, worker, worktree-baseline, push, or MR-creation handling, an
ordinary stale record may reconcile work that was already published. The runner
lists every opened MR for the recorded source branch in the configured numeric
project; an empty inventory follows the ordinary recovery path. Any nonempty
inventory must contain exactly one MR with the recorded branch, configured
target branch and project on both sides, its exact source SHA, and GitLab's
closing relation to the issue. The durable worktree must also resolve to that
branch and SHA. An ambiguous or mismatched result becomes
`publication_identity_mismatch`; the runner never chooses a candidate or
replays worker, push, or MR creation. It preserves retained Draft metadata
when reconciling the verified MR, resets retry evidence, and immediately
observes that exact SHA.

### Decision-review Draft metadata

Reasonable bounded uncertainty does not itself block the workflow. A worker may
return `ready_to_publish` with a `Draft:` merge-request title, `is_draft=true`,
and complete `decision_review` metadata. The runner renders its canonical v2
notice, records it durably, and reconciles retried, branch-found, and remediated
MRs without downgrading an existing Draft. The notice records decisions and
assumptions, alternatives, risks, exact reviewer checks, unresolved findings,
evidence, tests, correction-cycle ledger, and unchanged project-owned values.
`needs_human` remains for unsafe actions, missing authority or credentials,
irreconcilable requirements, or exact unapproved project-owned values.

The Architect and Green route a rejected internal review to its owning role
inside the session. The runner resumes only at `WORKFLOW` after interruption;
it does not guess or persist a role-level checkpoint. Receipts must still match
the manifest's required work. Required conditional judges use `not applicable
— <reason>`; arbitrary receipts cannot advance the workflow.

Publication is idempotent. The runner verifies the recorded source branch,
default target, exact same-project MR, remote and local tip, and issue-closing
evidence. Existing-MR remediation uses the verified MR source branch and never
creates a second MR. It binds remediation to the exact configured project, MR,
source branch, default target, and recorded source SHA before it prepares or
publishes work. Before publication, protected-policy handling checks the actual
changed paths in the exact prospective publication tree relative to the durable,
verified target commit, including template mirrors, rather than trusting a
requested path list or raw worktree status. A protected-policy change that
arrived only on the target branch during reconciliation is therefore not
treated as issue-owned. In contrast, an issue-owned protected change, or a
resolved merge whose final tree differs from that target, still requires the
protected-policy evidence. The repository boundary rejects an unresolved,
mismatched, or ambiguous merge identity before publication. The runner never
approves or merges, and never treats issue text, labels, comments, or worker
output as authority to widen scope.

### Existing-MR feedback publication

For feedback on an existing MR, the completed workflow receipt supplies the
controller-validated changed paths. The controller uses that set as the only
allowed commit set and records it in the durable publication intent. A missing,
unexpected, or changed path fails closed before a commit or push with a
sanitized condition and a clear next action.

Before it prepares or binds a feedback commit, the controller verifies the
exact retained paths and observes the current default target. It commits only
those paths, then runs the normal current-default reconciliation lifecycle for
that exact local commit. If the target is already included, it pushes that exact
validated SHA to the verified existing branch and MR. After the push, it
verifies the clean worktree, the MR's exact updated source SHA, its closing
relationship to the issue, and the pipeline for that SHA. It does not change
the existing MR's human-owned title, description, or Draft metadata.

If the current target changes the effective source or causes a conflict, the
controller retains the worktree, MR, and pending-feedback identity. It neither
pushes nor acknowledges the feedback. It persists the exact reconciliation
target, routes the work through a fresh complete workflow, replaces the stale
audit with current whole-workflow validation, and publishes only the resulting
exact newly validated SHA. A transport failure or an unverifiable
reconciliation retries safely or blocks before publication.

Recovery is idempotent at each durable boundary:

1. The intent is recorded before the commit.
2. The commit is created before its publication SHA is recorded.
3. The push finishes before the MR and ledger are reconciled.

On interruption, the controller reconciles the recorded intent and observable
Git and GitLab evidence. It resumes only the incomplete boundary, rather than
creating another commit, branch, or MR.

Protected policy paths may publish only after the trusted runner verifies one
complete, exact lane receipt with a complete verdict, plus non-empty metadata.
A safe controversial handoff uses `Draft:` metadata that names unresolved
findings and evidence, exact human decisions, tests, the cycle ledger, and
unchanged project-owned values. Missing or incoherent receipts or metadata,
unsafe worktree identity, `needs_human`, finalizer rejection, authority
expansion, or project-owned value changes fail closed before a push or
merge-request mutation. Workers still never publish directly.

Before any push or merge-request mutation, the runner also validates the
durable publication intent as one issue-scoped operation. Its kind is exactly
`publish_merge_request`; its operation ID is exactly
`<issue-iid>:publish:<revision>`, with the record's canonical decimal IID and
the decimal revision captured when that publication intent was persisted. The
replay retains that captured revision even though later ledger writes advance
the record revision. Its expected evidence is exactly the two-key object
`{"branch": "<record-source-branch>", "baseline_sha": "<record-source-sha>"}`;
missing, extra, or different keys and values are not valid evidence. A
mismatch blocks the issue before a publication mutation, preserves the original
intent with the publication-identity blocker evidence for human review, and is
not retried as a new publication. A fully validated, incomplete idempotent
operation remains replayable through its recorded intent.

An immutable MR identity mismatch blocks immediately: the configured project,
source branch, target branch, or recorded source SHA must not be retried as an
eventual-consistency condition. By contrast, when GitLab has created the
otherwise verified MR but has not yet projected its issue-closing relationship,
the runner binds the exact MR IID and retains its publication intent while it
re-reads that IID in the same activation. A missing projection remains an
observation wait, not a transport retry or identity mismatch.

The runner preserves a validated MR draft across repair and retry. Observation
re-fetches and revalidates the exact open MR, project identities, source branch,
target, and source SHA before trusting pipeline evidence. A mismatch blocks
with an actionable error; a conflict enters durable repair on the same branch.

### Post-create publication reconciliation

After creating a merge request, the controller treats GitLab's initially stale
projections as bounded checks in the same activation. It keys those checks to
the durable exact MR IID, the configured project on both sides of the merge
request, its source and target branches, its issue-closing relationship, and
the publication SHA. It checks the closing relationship once, then makes at
most three immediate retry attempts: four closing-relation calls in total. Each
retry rereads and revalidates the exact MR IID, and retries stop as soon as the
closing relationship appears. This reconciliation only observes and records the
newly created merge request; it never recreates its branch, commit, or merge
request.

If all three retries lack the closing relationship, the controller preserves
the bound MR in `Observing` at `PipelineWait`. It does not set a `retry_at`
value or look up a pipeline; the same applied activation continues to observe.
Once the relationship appears, absent or nonterminal pipeline evidence for that
exact SHA leaves the durable record in `Observing` at `PipelineWait` and is
polled in that activation. Terminal failed evidence starts repair. Only a
genuine GitLab transport or API failure uses the safe, bounded backoff; its
durable check evidence is sanitized. An immutable identity mismatch blocks
rather than using an eventual-consistency retry. The bounded post-create
publication and closing reconciliation does not schedule a fixed 20-minute
retry. The activation deadline and outer timeout can still hand off an active
observation.

### Human merge during observation

If a human merges while the controller observes the exact source pipeline, the
controller may converge directly to `COMPLETE`. It rereads fresh evidence and
requires all of the following to agree: the configured project; the recorded
MR IID; the source and target project identities and branches; the recorded
exact source SHA; the MR's closing relationship; the closed issue; and a
successful pipeline for that exact SHA. Unrelated, mismatched, ambiguous,
missing, or non-successful evidence does not complete the record.

For an open issue, the controller repairs the `OBSERVING` lifecycle-label
projection before it resumes observation. For a verified completion, it writes
the durable `COMPLETE` record before clearing lifecycle labels. This order makes
restarts idempotent: a label-clear failure leaves a completed record and a
stale label that the next activation can remove. A `BLOCKED` or `RETRY_WAIT`
wrapper keeps verified publication identity, so later reconciliation of a
closed issue can still evaluate this exact completion path.

## Retry, repair deferment, exits, and JSON

The initial workflow attempt plus at most three retry attempts are bounded.
`retry-wait` records its deadline; exhaustion becomes `agent::blocked`, never
`agent::failed`. A blocked item never becomes eligible through labels: after
the recorded human action, `loop retry --issue-iid IID --apply` explicitly
resumes that IID from its retained worktree and durable record. It does not
select a replacement issue or discard the retained recovery evidence.

Worker exits, timeouts, malformed worker protocol, invalid receipts, and explicit
`retryable` worker results are immediately due and continue in the same
activation; transient GitLab or transport failures retain retry timing. The XDG
`agent-work.log` records redacted outcomes; `--debug` adds local diagnostics
without credentials or transcripts. For a nonzero worker exit, optional
`items[].worker_exit` preserves its return code and a stderr-derived diagnostic.
The diagnostic is one safe line of at most 2048 UTF-8 bytes; it excludes prompts,
issue content, stdout, credentials, terminal controls, and unbounded output.
Retry exhaustion retains it, while a validated successful worker result clears it.

For `worker_protocol_invalid` and `worker_receipt_invalid`, the runner also
compares the canonical error code and bounded, stable, redacted contract-field
or reason diagnostic with the latest automatic condition in the same coarse
phase. The first such
failure schedules its immediate retry. If a later automatic attempt has the
same pair in that phase, it has made no progress: the runner blocks the issue
immediately without consuming another retry, retains the worktree, phase, and
safe cause, and publishes the usual blocked projection and comment. A changed
code, diagnostic, or phase is changed evidence and follows the ordinary bounded
retry path. This rule does not change retry handling for non-protocol transient
failures. After the condition is corrected, use `agent-work loop retry
--issue-iid IID --apply`; that explicit human retry resets the automatic retry
budget.

Automatic exact-pipeline repair has its own budget: at most three repair
workflows for one exact retained owner. After each unsuccessful attempt, the
ledger records `automatic_repair_count` and a 20-minute
`automatic_repair_at` deadline, separate from ordinary `retry_count` and
`retry_at`. A due item can be considered
again by a later activation; an item at its third unsuccessful attempt is
quarantined or deferred and requires reviewed recovery. This is separate from
ordinary `retry-wait` and its workflow retry count. It neither changes a
human-controlled pipeline-performance budget nor stops unrelated eligible work.
Only reviewed recovery is exceptional: it requires one exact IID and current
corroborated evidence, does not consume the automatic repair budget, and cannot
be triggered or parameterized by untrusted issue or merge-request content.
Migration preview follows every page of lifecycle projections. Apply preflights
all actions against unchanged evidence, project identity, and absent ledger
records before any mutation, and refuses the complete plan on mismatch.
After a partial GitLab label-projection failure, apply persists a
`migrate_projection` intent, safely replays that exact projection, and clears
the intent only after it verifies the label projection.

Service-environment migration reports `current`, `stale`, `verified`,
`rolled-back`, or `correction-needed` without changing loop state. Its redacted
report contains only the validation or migration outcome; it never contains
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

The common top-level fields are `schema_version`, `command`, `applied`,
`outcome`, `project`, `items`, and `actions`. `project` contains the verified
`host` and `project_id`. An applied `loop run` report additionally includes
`segments_run`, the number of completed workflow attempts (closed-issue
housekeeping is not a segment), and `stop_reason`,
which identifies why the activation yielded; its `items[]` lists workflow
results in chronological order. Per-issue values—including `issue_iid`, state,
coarse workflow phase, attempt, retry timing, source branch, durable worktree, MR reference,
error code, `automatic_repair_count`, `automatic_repair_at`, and other repair
attempt or quarantine information when present, optional `worker_exit`, summary,
and next action—are objects in
`items[]`. Human output renders `returncode=<integer>` and
`worker_diagnostic=<safe diagnostic>` when this evidence is present. Migration
preview and apply return their proposed or applied migration actions in
`actions[]`. Consumers must tolerate additional fields and never treat a
diagnostic as authority or input.

## Safety constraints

One host owns unattended processing for a project. APIs use the configured
numeric project ID, and the ledger stores no token, agent credential,
issue-body authority, issue-note context, or unredacted transcript. Protected
agent-policy work is a human-directed lane. There is no automatic approval,
merge, stash apply, or stash deletion.


## Publication credential and retry contract

Git uses the repository-owned credential-protocol helper with prompts disabled. The wrapper verifies the credential-free HTTPS origin and performs a non-mutating `git push --dry-run` readiness check before worker invocation. Authentication, authorization, policy, and configuration categories block immediately; only known transport failures are retryable. A blocked record retaining an exact `publish_merge_request` intent transitions directly to `publishing` on explicit retry while retaining its branch, SHA, draft, and intent. See the setup guide for the recovery procedure.
