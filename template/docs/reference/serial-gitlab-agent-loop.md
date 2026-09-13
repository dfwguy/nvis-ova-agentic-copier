---
title: Serial GitLab agent-loop reference
type: reference
summary: Look up the recoverable loop commands, lifecycle, data, and operating constraints.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-13
related:
  - path: ../how-to/setup-serial-gitlab-agent-loop.md
    why: Configures, recovers, and migrates the loop.
  - path: ../explanation/serial-gitlab-agent-loop.md
    why: Explains the architecture behind this contract.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains the recoverable state machine in detail.
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
agent-work loop status [--json] [--debug]
agent-work loop retry --issue-iid IID [--apply] [--json] [--debug]
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

Without `--apply`, `loop run` reports the next safe action once without
changing GitLab, the ledger, worktrees, branches, labels, or services; it does
not simulate a complete activation. `loop status` reports durable ledger
records together with verified configured-project identity. `loop retry`
previews one recorded blocked item; `--apply`
resets its retry budget and moves it to resumable preparation after explicit
human direction.

`migration preview` inventories legacy evidence and proposes only safe,
idempotent records and label changes. `migration apply` requires a reviewed
preview file and `--apply`; it does not resolve ambiguous evidence. `--json` emits
the documented redacted JSON result. `--debug` adds local diagnostics without
credentials, issue text, prompts, or worker transcripts. Invalid usage exits `2`.

`upstream-finding preview` validates the local manifest, then performs GET-only
reads against the explicit upstream target using its dedicated credential file.
It never posts, updates, or closes an issue, and failures leave no ledger,
remote, or checkout mutation. Successful output contains only structural fields,
stable fingerprints, outcome, and a positive matching IID when one exists; it
never prints project identity, finding text, validation evidence, or evidence URLs.
Invalid manifests, credentials, access, API responses, and markers exit `1`
with sanitized diagnostics. Correct the boundary input and safely rerun;
`--apply` remains equally read-only.

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

JSON success output contains only `schema_version`, `command`, `outcome`, the
three fingerprints, `copied_path_count`, and `evidence_link_count`. Human
success output contains the outcome, three fingerprints, and the two counts.
No manifest content is retained or printed. Valid
previews exit `0`; sanitized read or validation failures exit `1`; argparse
usage failures exit `2`.

`AGENT_WORK_BASE_BRANCH` is required and must name a valid configured
`agent/...` service branch; the setup uses the canonical `agent/work-base`
branch. The runner validates it before it claims an issue, changes a label, or
creates a ledger record; an omitted value fails the command instead.

## Systemd wrapper environment

The systemd wrapper requires a bare `GITLAB_HOST` and positive numeric
`GITLAB_PROJECT_ID`; malformed or missing values exit `2` before execution. The
mode-600 service environment file stores those public identity values and the
recorded executable paths, never GitLab credentials. The wrapper requires an
absolute executable `AGENT_WORK_DIRENV` and a regular, non-symlink
`${AGENT_WORK_CHECKOUT}/.envrc`. The deterministic controller, AI workflow,
and required nested agents use the same scoped Developer credential. Humans
provide product and policy inputs and alone approve or merge. The canonical
ignored `.envrc` exports the configured
host, numeric project ID, locally configured `GITLAB_TOKEN_PASS_PATH`, and
`GITLAB_TOKEN="$(pass show "$GITLAB_TOKEN_PASS_PATH")"`. The wrapper runs the
API, preview/apply HTTPS Git, readiness proof, and Python worker through that
same `direnv exec` boundary, then restores the captured target and absolute
`glab` Git-helper controls. Setup runs its preview under scrubbed `env -i`
before enabling the timer.

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

Create the two scoped labels during project setup. Humans can assign high to
expedite fresh work, remove either label for normal ordering, or assign low to
defer it.

| GitLab label | Ledger state or outcome | Meaning |
| --- | --- | --- |
| `agent::running` | preparing, executing, retry-wait, publishing, observing, or repair | Work is active or safely resumable. |
| `agent::awaiting-human` | awaiting-human | Exact MR evidence and required receipts await human review. |
| `agent::blocked` | blocked | A human must take the recorded next action. |
| `agent::failed` | failed | The runner could not safely reconcile or record an exceptional condition. |

| Queue label | Fresh-work rank | Control |
| --- | --- | --- |
| `agent-priority::high` | 0 | Human expedite |
| no recognized exact value | 1 | Normal; remove a scoped label |
| `agent-priority::low` | 2 | Human defer |

The durable sequence is `preparing → executing`; execution may enter
`retry-wait`, resume execution, or produce `publishing`. Verified publication
enters `observing`, which can enter `repair`, return to `publishing`, or finish
at `awaiting-human`. A human-blocked condition becomes `blocked`; after the
recorded action is complete, an operator explicitly retries that IID. Human
merge or closure ends an awaiting-human record.

A newly selected issue attempts its complete applicable repository workflow in
the same activation. A durable record resumes from its coarse `WORKFLOW`
boundary and durable worktree; the first rollout does not add a sidecar
checkpoint for every role or phase. Existing legacy phase-ledger values finish
their recorded route; new work uses `WORKFLOW`. Within that session, the Architect hands the approved design to
Red, Red returns failing-test evidence to Green, Green fans out applicable
read-only judges and repairs what it can, then returns consolidated feedback to
the Architect for the final decision.

Before a prepared commit is pushed, the runner fetches the current default
branch and proves it is already an ancestor. If it is not, the runner retains
a `git merge --no-ff --no-commit` in the durable worktree, clears the prepared
publication binding, and reruns the complete workflow; it never rebases or
force-pushes. A pending retained merge is recoverable only when it targets an
ancestor of the freshly fetched default, while unrelated dirty state blocks.
Resolving that merge creates one ordered merge commit whose first parent is the
previous candidate and whose second parent is the retained merge target, which
the runner verifies is an ancestor of the freshly fetched default.

An exact-MR conflict uses that reconciliation before it re-enters `WORKFLOW`
using the durable worktree. If GitLab still reports a conflict but no
default-branch merge is needed, the runner keeps the existing bounded
merge-conflict retry instead. Pipeline rejection enters its normal repair
workflow; its later publication is protected by the same pre-push ancestry
fence. Green performs any safe repair inside its workflow session, consolidates
the follow-up reviews, and returns the result to the Architect. Immediately
after post-publication observation, the runner revalidates exact MR evidence
again before it records `awaiting-human`.

Each timer activation acquires the single-project lock and reconciles intent
and observed evidence. It then advances only the selected or resumed issue.
Recovery applies at the worktree and coarse workflow boundary, not at every
internal agent handoff.

Each Python activation advances at most one issue and retains its 10,800-second
active-work budget. The systemd wrapper continues only after `awaiting-human`,
`blocked`, `failed`, or `complete`: it validates that report, makes one fresh
read-only eligibility check, and begins another activation only for `eligible`.
Between issues it re-queries GitLab and fast-forwards the clean service base;
this bounded housekeeping is outside the per-issue budget. An `idle`,
`pipeline-wait`, a future `retry-wait`, `repair`, `time-limit`, or `segment-limit`
result yields immediately. `OnUnitInactiveSec=20min` schedules both true empty-queue
idle checks and yielded pipeline/future-external-retry checks. The service has no sequence-wide
timeout, so it cannot truncate an otherwise unbounded sequence of individually
bounded activations. The loop is serial per host and project, not multi-host
coordination.

## Ledger and worktrees

State is below the XDG state directory, keyed by configured GitLab host and
numeric project ID. Each `loop status` ledger item identifies its durable
worktree path. Every active issue
has one atomic, restrictive-permission ledger record and durable worktree. A
record holds its schema and project identity; IID and work kind; state, coarse
workflow phase,
attempt, and retry deadline; branches and commits; MR and publication evidence;
worker protocol, result, and receipts; redacted error and next action; and
durable transition intent.

The application also writes its local lifecycle log to `agent-work.log` in
that XDG project-state directory. This is application-owned evidence alongside,
not a replacement for, the systemd service journal.

Intent is persisted before an external mutation and observed evidence after it,
so reconciliation can safely complete interrupted idempotent operations. A
dirty durable worktree remains for its next workflow attempt. Recovery stashes are
inventoried only: the runner never applies or drops them. Ambiguous worktrees,
branches, labels, or stashes become blocked for human review. A corrupt record
is blocked where reportable, and failed only where it cannot be safely recorded.

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
recursion is disabled. System/global transport and credential configuration is
the operator-managed service boundary. After first deployment, stop the timer,
manually fast-forward once, preview/canary, then restart it; later applied
activations self-refresh.

A workflow-owning worker returns a versioned, redacted envelope for its
expected ledger transition. Its result is `ready_to_publish`, `retryable`, or
`needs_human`, with the coarse completed state, receipts, diagnostic, and next
action. The trusted runner validates it against the ledger, configured numeric
project, Git evidence, and required workflow receipts before changing state.

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
capabilities, returns one protocol-version-1 envelope with these values:

| Field | Value |
| --- | --- |
| `result_kind` | `split_required` |
| `phase_status` | `complete` |
| `completed_phase` | `workflow` |
| `workflow_lane` | `unknown` |
| `split_plan` | 2..25 ordered slices with stable ASCII keys and lower-ordinal dependencies |

This route requires no receipts, merge-request draft, or error code. The worker
makes no worktree or GitLab mutation, creates no child issues, and executes no
proposed slice. The trusted runner persists `decomposing`, derives a durable
marker for each child, recovers crash-created children by that marker, creates
missing children in dependency order, posts one decomposition note, and closes
the parent. Children whose prerequisites remain open are not eligible until
those dependencies close. `LedgerRecord.schema_version` and
`WorkerResultEnvelope.protocol_version` remain `1`; older records and envelopes
remain valid.

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
and complete `decision_review` metadata. The runner renders its canonical v1
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
changed paths, including template mirrors, rather than trusting a requested
path list. The runner never approves or merges, and never treats issue text,
labels, comments, or worker output as authority to widen scope.

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
the runner retains the validated draft and uses its existing bounded GitLab
retry path. It blocks only if that projection remains absent when that retry
path is exhausted.

The runner preserves a validated MR draft across repair and retry. Observation
re-fetches and revalidates the exact open MR, project identities, source branch,
target, and source SHA before trusting pipeline evidence. A mismatch blocks
with an actionable error; a conflict enters durable repair on the same branch.

## Retry, exits, and JSON

The initial workflow attempt plus at most three retry attempts are bounded.
`retry-wait` records its deadline; exhaustion becomes `agent::blocked`, never
`agent::failed`. A blocked item never becomes eligible through labels: after
the recorded human action, `loop retry --issue-iid IID --apply` explicitly
resumes it.

Worker exits, timeouts, malformed worker protocol, invalid receipts, and explicit
`retryable` worker results are immediately due and continue in the same
activation; transient GitLab or transport failures retain retry timing. The XDG `agent-work.log` records redacted
outcomes; `--debug` adds local diagnostics without credentials or transcripts.
For a nonzero worker exit, the optional `items[].worker_exit` object preserves
the exact signed `returncode` and a `diagnostic` derived only from stderr. A
negative return code denotes POSIX signal termination; positive values are not
otherwise classified. The diagnostic is one safe line of at most 2048 UTF-8
bytes: it excludes the worker prompt, issue content, stdout, credentials, raw
terminal controls, and unbounded output. Legacy schema-v1 records omit the
field. A fourth failed exit retains this evidence when retry exhaustion changes
the error code to `retry_exhausted`; a validated successful worker result clears
stale exit evidence. The systemd wrapper writes the same validated JSON report
to stdout, making this bounded evidence visible in its journal.
Migration preview follows every page of lifecycle projections. Apply preflights
all actions against unchanged evidence, project identity, and absent ledger
records before any mutation, and refuses the complete plan on mismatch.
After a partial GitLab label-projection failure, apply persists a
`migrate_projection` intent, safely replays that exact projection, and clears
the intent only after it verifies the label projection.

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
  "schema_version": 1,
  "command": "loop.status",
  "applied": false,
  "outcome": "status",
  "project": {"host": "gitlab.example", "project_id": 123},
  "items": [],
  "actions": []
}
```

The common top-level fields are `schema_version`, `command`, `applied`,
`outcome`, `project`, `items`, and `actions`. `project` contains the verified
`host` and `project_id`. An applied `loop run` report additionally includes
`segments_run`, the number of completed workflow attempts, and `stop_reason`,
which identifies why the activation yielded; its `items[]` lists workflow
results in chronological order. Per-issue values—including `issue_iid`, state,
coarse workflow phase, attempt, retry timing, source branch, durable worktree, MR reference,
error code, optional `worker_exit`, summary, and next action—are objects in
`items[]`. When present, human output names its fields as `returncode=<signed
integer>` and `worker_diagnostic=<safe diagnostic>`; JSON uses
`{"returncode": <signed integer>, "diagnostic": <safe string>}`. Migration
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

Git uses only the configured absolute `glab` credential helper with prompts disabled. The wrapper verifies the credential-free HTTPS origin and performs a non-mutating `git push --dry-run` before worker invocation. Authentication, authorization, policy, and configuration categories block immediately; only known transport failures are retryable. A blocked record retaining an exact `publish_merge_request` intent transitions directly to `publishing` on explicit retry while retaining its branch, SHA, draft, and intent. See the setup guide for the recovery procedure.
