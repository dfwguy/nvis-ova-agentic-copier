# Security Policy: NVIS-OVA Agentic Copier

NVIDIA is dedicated to the security and trust of our software products and services,
including all source code repositories managed through our organization.

**Please do not report security vulnerabilities through public GitLab issues or merge requests.**

## Reporting a Vulnerability

To report a potential security vulnerability in this project:

* **Web (preferred):** [NVIDIA Vulnerability Disclosure Program](https://www.nvidia.com/en-us/security/)
* **Email:** [psirt@nvidia.com](mailto:psirt@nvidia.com)
  - We encourage you to use the following PGP key for secure email communication:
    [NVIDIA public PGP Key](https://www.nvidia.com/en-us/security/pgp-key)
* **GitLab:** Use this repository's **Security** tab > **Report a vulnerability** to submit
  a report directly.

### What to Include

- Product/project name and version or branch affected
- Type of vulnerability (e.g., template injection, supply chain, privilege escalation)
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit (if available)
- Potential impact assessment

### What to Expect

NVIDIA's PSIRT will acknowledge receipt, validate severity, develop a fix, and publish a
security bulletin as appropriate.

## Security Architecture & Context

NVIS-OVA Agentic Copier is a **Copier template** — a CLI scaffolding tool that generates
new Python projects pre-configured with UV, Ruff, ty, pytest, tox, pre-commit, GitLab CI,
and a multi-agent Claude Code workflow (`CLAUDE.md`, `.claude/agents/`, `.agents/skills/`).

This software operates at the **CLI / Developer Tooling** level. Its primary security
responsibility is to ensure that the scaffolded project structure, AI agent definitions, and
CI/CD configuration do not introduce vulnerabilities into generated projects. The tool runs
locally on developer workstations and interacts with GitLab via the `glab` CLI.

**Key security boundaries:**
- **Template execution boundary**: `copier copy --trust` executes `_tasks` (shell commands:
  `git init`, `uv sync`, `uv run pre-commit install`) on the user's machine. The `--trust`
  flag is explicit user consent; without it, Copier refuses to run tasks.
- **Local hook boundary**: pre-commit invokes project-managed tools through
  `uv` from `.pre-commit-config.yaml`; their versions are resolved through the
  project's dependency metadata and lockfile.
- **AI agent control boundary**: `.claude/settings.json` restricts which shell commands the
  Claude Code AI agent may execute, including a deny list blocking `glab mr approve*` and
  `glab mr revoke*` to prevent AI from approving its own merge requests.
- **GitLab API boundary**: The repository setup procedure calls `glab api` to configure
  branch protection and approval rules on `gitlab-master.nvidia.com`.

### Threat Model

The following scenarios represent the primary security concerns for this project:

1. **AI agent MR approval bypass via `glab api`:** The `settings.json` deny list blocks
   `glab mr approve*` and `glab mr revoke*` but does not block `glab api -X POST
   projects/<id>/merge_requests/<iid>/approve`. A prompt-injected or compromised subagent
   could approve its own MR via the raw GitLab API endpoint, bypassing the deny rule.
   (Open Argus finding `0a1f365f`; confidence 0.84.)

2. **Supply chain compromise via local development tools:** Both
   `.pre-commit-config.yaml` files invoke `uv`-managed tooling. A compromised
   package or lockfile update could execute arbitrary code on a developer's
   machine during `pre-commit run`.

3. **Copier `--trust` task execution with tampered template:** The `_tasks` block in
   `copier.yaml` runs three shell commands on the user's machine after rendering. An
   attacker who can modify `copier.yaml` or the `template/` directory (e.g., via a
   compromised MR that bypasses the approval gate) could execute arbitrary shell commands
   on every developer who runs `copier copy --trust`.

4. **Jinja2 template injection in `pyproject.toml.jinja` via `project_slug`:** The
   `project_slug` variable is interpolated without a `validator` into
   `template/pyproject.toml.jinja` (lines 2 and 27). A malicious or careless slug
   containing quotes or newlines could inject arbitrary TOML into the generated project's
   `pyproject.toml`. Exploitability is low because users scaffold for themselves; risk
   increases in CI/automated pipelines passing `--data project_slug=<untrusted>`.
   *(Accepted risk: Argus finding `796e7078`; no cross-trust boundary in normal use.)*

5. **AI agent command allowlist expansion enabling unintended lateral movement:** The
   `settings.json` allow list grants broad `git` and `glab` read access (e.g.,
   `git log*`, `git diff*`, `glab mr view*`). A compromised or prompt-injected agent
   operating within these permissions could enumerate sensitive repository history, MR
   descriptions, or CI/CD secrets visible in pipeline logs without triggering a user
   approval prompt.

### Critical Security Assumptions

- **GitLab branch protection is enforced externally.** The AI agent workflow assumes
  `main` is protected (push blocked, MR approval required) via GitLab project settings.
  Until the repository setup procedure has been run and `CONTRIBUTING.md` exists, this protection is not
  guaranteed.
- **The developer workstation is trusted.** `copier copy --trust` and pre-commit hooks
  execute code on the local machine. No sandbox or verification layer protects the
  execution environment.
- **Local development tools are uncompromised.** The toolchain assumes the
  packages resolved through the project's dependency metadata and lockfile have
  not been tampered with.
- **`glab` OAuth session integrity.** The repository setup procedure and AI agent git flow
  assume the `glab` OAuth token is valid and uncompromised. A stolen token grants full
  GitLab API access at the authenticated user's permission level.
- **AI agent prompts and subagent inputs are not adversarially crafted.** The deny list
  in `settings.json` is a backstop for well-intentioned agents. It does not provide full
  protection against a prompt-injection attack that persuades the agent to call `glab api`
  or other allowed commands in unintended ways.
- **Generated projects are scaffolded by their own owners.** Template variable injection
  risks (e.g., `project_slug`) assume the person running `copier copy` controls the
  resulting project. Automated pipelines passing untrusted `--data` values are outside
  the assumed threat model.
