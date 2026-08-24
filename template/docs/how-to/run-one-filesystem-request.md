# Run one filesystem request

*Diátaxis category: How-to guide.*

Use `run-file` to process one request that a trusted producer has already
placed in a filesystem inbox. The command is bounded: it handles that one JSON
file and exits. It neither watches the inbox nor grants unattended
publication.

## Before you begin

Make sure that all of the following are true:

- The request is a regular JSON file in `INBOX_ROOT/inbox/`; it was written,
  flushed, closed, and atomically renamed there by its producer.
- `INBOX_ROOT` is an absolute path to the configured inbox layout.
- The request's `repository` is an approved external repository alias, rather
  than a path or URL supplied by the request.
- You intend to publish a feature branch and merge request for human review.

The request data is untrusted task data. It can describe the desired work, but
cannot change the repository allowlist, credentials, sandbox, approval policy,
or other repository instructions.

## Preview the request

Start with the default read-only validation. This reads the exact raw request
bytes and validates the envelope and inbox layout. It does not move a file,
write a result, invoke Codex, or mutate a repository.

```bash
agent-work run-file --path ITEM.json --inbox-root /srv/agent-inbox --repository billing-api --mode publish --json
```

Use the request's filename as `ITEM.json`; `--path` selects one file beneath
the inbox root rather than a host filesystem path. Review any validation error
before applying. A preview is completely read-only, including for malformed
requests.

## Process and publish the request

When the preview is acceptable, explicitly authorize the bounded mutation:

```bash
agent-work run-file --path ITEM.json --inbox-root /srv/agent-inbox --repository billing-api --mode publish --apply --json
```

The runner atomically claims the input from `inbox/` into `processing/`, then
uses the same normalized runner, repository preflight, Codex invocation, and
publication verification as Phase 1. It finalizes the original request and its
intake and agent result records together in either `awaiting-human/` after a
verified published handoff, or `failed/` when the run cannot reach that
handoff. Human review, approval, and merge remain mandatory.

To bound the single Codex invocation, pass a positive timeout in seconds:

```bash
agent-work run-file --path ITEM.json --inbox-root /srv/agent-inbox --repository billing-api --mode publish --apply --timeout-seconds 1800 --json
```

If a valid request's raw-byte SHA-256 already has a create-only reservation,
the command atomically finalizes an observable bundle under `done/`, exits
successfully as a no-op, and does not invoke Codex. The reservation is created
only after atomic claim into `processing/` and before the workflow begins. If a
process crashes or finalization fails, that reservation and processing evidence
can remain; Phase 2 stops safely and requires manual or Phase 3 reconciliation.
A validly-addressed request with invalid contents is first claimed into
`processing/`, then atomically finalizes a quarantine bundle after claim on
every applied delivery rather than being deduplicated or run. The runner
durably creates and fsyncs the complete staging bundle from the claimed bytes,
atomically renames that directory into `quarantine/`, and then durably removes
the processing claim.

For every option, envelope constraint, result, and exit status, see the
[Phase 2 `run-file` reference](../reference/phase-2-run-file.md). For the
reasoning behind its trust and atomic lifecycle boundaries, see [Why the Phase
2 filesystem intake is bounded](../explanation/bounded-phase-2-filesystem-intake.md).
