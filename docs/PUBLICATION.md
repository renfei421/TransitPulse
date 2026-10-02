# Public portfolio release

Repository: https://github.com/renfei421/TransitPulse

Interactive demo: https://renfei421.github.io/TransitPulse/

## Included

- Application, ingestion, model adapters, infrastructure specifications and tests.
- Reproducible analysis and restore scripts with preserved team provenance.
- Aggregate experimental evidence and charts, with uncertainty and coverage limits.
- A standalone frozen dashboard and an engineering evidence page.

## Kept private

Original posts, authors' raw content, cached provider responses, collection
SQLite databases, inference budget state, access credentials, kubeconfig,
complete recovery archives and the original Git history stay outside this repo.
Do not commit a recovery ZIP: it contains private source data and historical code.

The working archive is required to reproduce the exact 90,576-record cohort.
Public readers can explore its aggregate results without credentials, run the
synthetic demonstration and tests, or supply their own permitted data and keys.
The synthetic demo is explicitly separate from the real experiment.

## Validation

The CI workflow runs lint, declaration checks, credential regression checks,
unit tests, and real Elasticsearch/Redis integration tests in isolated namespaces.
It does not run paid model inference or provision cloud resources. Historical
cloud and model measurements remain in `docs/evidence`; CI results are separate.

Pages uploads only a temporary directory containing `index.html`,
`engineering.html` and `.nojekyll`. No raw-data directory or recovery package is
published as a Pages artifact.
