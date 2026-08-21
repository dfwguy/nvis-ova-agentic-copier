# Customize a generated project's workflow

**Diátaxis category: How-to guide.** Use this when a generated project needs
project-specific conventions or tooling after its initial creation.

1. Change the generated project's own configuration, rather than editing this
   template repository. For example, adjust dependency groups or tool settings
   in `pyproject.toml`, and update the project's `README.md` to describe its
   domain.
2. Keep the project's checks aligned. Run the relevant commands after a change:

   ```bash
   uv run ruff check --fix
   uv run ruff format
   uv run ty check
   uv run tox
   ```

3. If you change the agent workflow or coding conventions, update the
   generated project's `CLAUDE.md` and the referenced `.claude/` guidance at
   the same time. Those files are the source of truth for contributors and
   agents working in that project.
4. Commit the customization through the generated project's required feature
   branch and merge-request flow.

Do not expect `uvx copier update --trust` to overwrite the generated project's
`pyproject.toml`, `README.md`, `.gitlab-ci.yml`, or package initializer: the
template deliberately preserves those files. For the reasoning, see
[why the template separates source from generated output](../explanation/template-and-generated-project.md).
