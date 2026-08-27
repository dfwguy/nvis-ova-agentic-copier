# OVA Agentic Flow copier

A [Copier](https://copier.readthedocs.io/) template for new Python projects.
It provides a modern Python toolchain and a portable, human-gated workflow for
Claude Code, Codex, and Pi. Generated projects support Python and, when
present, Bash-specific checks.

## Check and start the GitLab issue loop

Check whether the serial loop can select work without changing the repository
or GitLab:

```bash
uv run agent-work run-next --json
```

After a human has authorized unattended publication and the safeguards are in
place, process one issue:

```bash
uv run agent-work run-next --apply --json
```

For the one-time safety checks and user-level timer installation, follow
[Set up the serial GitLab agent loop](docs/how-to/setup-serial-gitlab-agent-loop.md).
The loop hands completed work to `awaiting_human` without waiting for merge and
never approves or merges a merge request. Developer access alone is not proof
of project approval or self-approval safeguards; the how-to verifies the
observable controls before `--apply` is enabled.

## Quick start

Install [UV](https://docs.astral.sh/uv/getting-started/installation/), Git,
and [`glab`](https://gitlab.com/gitlab-org/cli), then generate and test a
project:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project
cd my-project
uv run pytest
```

`--trust` permits Copier to run this template's declared post-render tasks.
Review the [template contract](docs/reference/template-contract.md) for those
tasks, inputs, generated files, and update behavior. For a guided first run,
see [Create your first project](docs/tutorials/create-your-first-project.md).

## What you get

- UV-managed Python, Ruff, ty, pytest, tox, pre-commit, and GitLab CI.
- A `src/<package>/` project layout with starter tests and documentation.
- Bash syntax and ShellCheck validation when a project contains Bash.
- A portable, test-driven, human-gated agent workflow.
- A serial GitLab issue loop that creates merge requests but never approves or
  merges them.

## Agent workflow

Generated projects include these roles. Read [how the agent workflow works](docs/explanation/agentic-workflow.md)
for its design, lifecycle, and validation boundaries.

| Role | Responsibility | Does not |
| --- | --- | --- |
| Architect | Designs modules, data structures, and dependency boundaries | Write code or tests |
| Remediator | Establishes an evidence-backed standards baseline and sequences fixes | Edit files, weaken standards, or declare skipped gates passing |
| Test Writer | Writes failing tests and stops at Red | Touch implementation |
| Documentation Writer | Creates or updates Diátaxis documentation | Touch code, tests, or README.md |
| Implementer | Makes the minimum change to reach Green | Refactor or add untested features |
| Non-functional Reviewer | Reviews test timing and performance evidence before final validation | Rerun tests, edit code, or wait for finalization |
| Implementation Judge | Runs applicable Python/Bash lint and non-test checks | Fix issues |
| Documentation Judge | Validates documentation accuracy and Diátaxis fit | Edit docs, code, tests, or README.md |
| Quality Judge | Reviews production-code design, clarity, and conventions | Run tooling |
| Test Judge | Reviews test quality and language-appropriate test practices | Run tooling or review production-code quality |
| README Reviewer | Ensures the README orients new visitors | Touch docs, code, tests, or agent definitions |
| CI/CD Judge | Checks CI configuration and pipeline health | Edit CI configuration or retry pipelines |
| CLI Judge | Checks CLI changes against safety, output, and exit-code contracts | Run tooling or edit files |
| Filesystem Judge | Checks the project layout or documented exceptions | Edit files, run tooling, or review non-layout quality |
| Finalizer | Verifies final gates and commits locally before publication handoff | Fix issues |

## Documentation

The [documentation index](docs/README.md) organizes the detailed material as
tutorials, how-to guides, reference, and explanation. It includes project
creation and updates, template validation, the loop setup, and the template
contract.

## Developing this template

Use [Validate template changes](docs/how-to/validate-template-changes.md) for
the repository's validation commands. Read [Why the template and generated
project are separate](docs/explanation/template-and-generated-project.md) for
the boundary between this repository and its generated output.
