# Component entrypoint

The maintained deployment, configuration, metrics and test instructions are in:
- [Project README](../../README.md)
- [Operations runbook](../../docs/OPERATIONS.md)
- [Architecture](../../docs/ARCHITECTURE.md)
- [Data expansion](../../docs/DATA_EXPANSION.zh-CN.md)

Use the root Python environment and package imports. Source code is packaged in
a Fission archive or immutable Docker image. Credentials come from environment
variables / Kubernetes Secrets. Index mappings are created only by database.migrate.
