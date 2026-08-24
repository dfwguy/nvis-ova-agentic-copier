# Phase 2 `run-file` reference

*Diátaxis category: Reference.*

This page is the interface contract for the bounded Phase 2 filesystem-intake
runner. It consumes at most one request file and exits; it is not a watcher,
queue service, or unattended publisher.

## Synopsis

```text
agent-work run-file --path ITEM.json --inbox-root ABS_ROOT --repository ALIAS --mode publish [--apply] [--json] [--timeout-seconds SECONDS]
```

`--mode publish` is required and is the only Phase 2 mode. `ABS_ROOT` must be
an absolute configured inbox root. `ALIAS` must name a trusted external
repository in the runner's allowlist; neither the JSON envelope nor a path or
URL can select a repository.

## Options

| Option | Required | Contract |
| --- | --- | --- |
| `--path ITEM.json` | Yes | Selects one JSON filename under the inbox root's `inbox/` directory. It is not an arbitrary host path. |
| `--inbox-root ABS_ROOT` | Yes | Supplies the absolute root containing the controlled `inbox/`, `processing/`, `done/`, `awaiting-human/`, `failed/`, and `quarantine/` directories. |
| `--repository ALIAS` | Yes | Selects one configured, trusted external repository alias. It must agree with the envelope's `repository` value. |
| `--mode publish` | Yes | Permits only the verified branch-and-merge-request publication workflow. |
| `--apply` | No | Enables the claim, quarantine, execution, and finalization mutations. Without it, the command is entirely read-only. |
| `--json` | No | Emits one JSON command report on standard output instead of human-readable output. |
| `--timeout-seconds SECONDS` | No | Bounds the one Codex invocation. `SECONDS` is a positive integer and defaults to `900`; it does not enable polling, retry, or a daemon. |

## Request envelope

The input is one UTF-8 JSON object with exactly these six required fields. No
additional fields are allowed.

| Field | Contract |
| --- | --- |
| `schema_version` | Integer constant `1`. |
| `source_id` | Producer-supplied identifier, 1–128 characters, matching `^[A-Za-z0-9._-]+$`. |
| `repository` | Trusted repository alias, 1–128 characters, matching `^[A-Za-z0-9._-]+$`; it must equal `--repository`. |
| `title` | Requested-work title, 1–512 characters. |
| `body` | Requested-work description, 0–65,536 characters. |
| `acceptance_criteria` | JSON array of 1–64 unique strings, each 1–2,048 characters. |

The entire raw request file must be UTF-8 JSON and is limited to 262,144 bytes
(256 KiB). Its deduplication identity is the SHA-256 of its exact raw bytes,
before JSON parsing or normalization. The envelope is task data, never
authority to override runner or repository policy.

## Filesystem safety and lifecycle

The runner requires a safe, non-overlapping, same-device inbox layout. It
refuses traversal, symlinks, hard links, non-regular files, overlapping
controlled directories, and layouts that would cross a device during an atomic
move. These failures do not cause the runner to follow the supplied path.

Without `--apply`, the command reads and validates only. It does not claim,
quarantine, deduplicate persistently, create result files, invoke Codex, or
change repository state.

With `--apply`, the runner follows this bounded order:

1. It validates the controlled layout and safe selected inbox entry, then
   atomically claims the original entry from `inbox/` into `processing/`.
2. It validates the claimed bytes against the 256 KiB limit, raw-byte content
   hash, envelope, and repository alias. For invalid contents that were validly
   addressed, it durably creates and fsyncs a complete quarantine staging
   bundle from the claimed bytes, atomically renames that directory into
   `quarantine/`, and then durably removes the processing claim.
3. After a valid request is claimed, the runner creates a create-only
   reservation for its raw-byte SHA-256 before beginning the workflow. A later
   valid request whose hash already has that reservation atomically finalizes
   an observable bundle under `done/<full-sha256>-<run-id>/`, exits 0, and
   never invokes Codex. Invalid payloads are quarantined on every applied
   delivery; they do not reserve or deduplicate a hash.
4. For a claimed non-duplicate request, the runner reuses Phase 1's normalized
   work-item runner, preflight, one Codex invocation, and publication
   verification.
5. It atomically finalizes a bundle containing the request, intake result, and
   agent result in `awaiting-human/` after a verified publication, or in
   `failed/` otherwise.

`awaiting-human` means that the resulting branch and merge request have been
verified and are waiting for human review. It does not mean approved or
merged. `failed` records a terminal failure for this bounded invocation; Phase
2 has no retries, backoff, reconciliation, leases, or concurrent consumers.
If a process crashes or finalization fails after reservation, the reservation
and `processing/` evidence can remain. Phase 2 stops safely and requires
manual or Phase 3 reconciliation; it does not infer success from a reservation.

## Output and exit contract

With `--json`, a successful preview, duplicate no-op, or verified
`awaiting_human` handoff writes exactly one command report to standard output
and exits 0. An applied quarantine also writes exactly one structured
quarantine report to standard output, but exits 1. Operational or runtime
blockers write diagnostics to standard error and no JSON document. A successful
applied handoff includes the canonical v1 agent result.

| Exit status | Meaning |
| --- | --- |
| `0` | A preview completed, a duplicate was safely ignored, or an applied request reached a verified `awaiting_human` handoff. Successful JSON reports are written to standard output when `--json` is set. |
| `1` | An applied invalid request was quarantined (with one structured quarantine JSON report on standard output when `--json` is set), or an operational/runtime blocker prevented the requested outcome. Blockers—including an unsafe layout, claim/finalization failure, timeout, failed preflight, or failed publication verification—write diagnostics to standard error only and no JSON document. |
| `2` | Invalid CLI usage, including a non-absolute inbox root, unsupported mode, invalid timeout, or invalid option values. |

## Deferred scope

Filesystem watchers, producer authentication, leases, retries and backoff,
reconciliation, concurrent consumers, and unattended publication are outside
Phase 2. They are planned only for later phases. The runner never approves or
merges a merge request.
