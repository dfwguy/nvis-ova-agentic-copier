# Create a project non-interactively

**Diátaxis category: How-to guide.** Use this when automation or a repeatable
script needs to provide all Copier answers without prompts.

Pass values with `--data` and give Copier a target directory:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git order-service \
  --data project_name="Order Service" \
  --data project_slug="order_service" \
  --data description="Order ingestion pipeline"
```

`--trust` is still required because the template declares post-generation
tasks. After Copier finishes, verify the generated project:

```bash
cd order-service
uv run pytest
```

Use an importable Python identifier for `project_slug`; it becomes both the
package directory and the distribution name. See the
[template reference](../reference/template-contract.md#copier-inputs) for the
complete input contract.
