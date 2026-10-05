# Reproducible verification evidence

Recorded on 2026-10-01 Australia/Sydney from local runs, a read-only cloud preflight,
and subsequent Elasticsearch and Fission/API cloud acceptance. These compact JSON files
contain no real post text, secrets or model weights. checksums.json records their
SHA-256 hashes of the JSON files with repository-standard LF line endings.
Public evidence uses repository-relative archive paths and generic historical-data
labels. These editorial changes do not alter measured counts, results or timestamps.
Full JUnit/coverage, executed notebook and HTML stay in ignored
artifacts/; commands below regenerate them.

| File | What it establishes |
|---|---|
| verification.json | 132 tests, no failures/errors/skips; 50.98% overall backend line coverage; explicit service/model flags |
| fission-smoke.json | Actual Fission image specialized the application and returned metadata, health and 180 synthetic daily-topic aggregate rows |
| live-capture.json | 1,485 live Jetstream events, 12 accepted posts, 12 raw + 12 processed documents verified; source-access limits |
| benchmark-100k.json | 100,000 synthetic documents, real local Elasticsearch bulk/query timings and exact test configuration |
| model-benchmark.json | Actual pinned RoBERTa CPU inference over 96 distinct short synthetic texts at batch sizes 1/8/16 |
| cloud-preflight-2026-10-01.json | Live DigitalOcean Kubernetes API: two Ready nodes, default CSI StorageClass, no persistent volumes yet, and missing Metrics API; no cloud workload deployment or performance claim |
| cloud-elasticsearch-2026-10-01.json | ECK-managed ES 8.19.22, verified TLS, separated identities, real 401/403 and bulk upsert checks; replacement Pod read the same probe from the same 20 GiB PVC/PV; single node, no HA or original data recovery claim |
| cloud-fission-api-2026-10-01.json | 15 route checks through the actual cloud Fission router, Ready pods, pinned runtime image IDs, available Metrics API and CPU HPA status; no load-induced scaling claim |
| cloud-api-data-2026-10-01.json | 8 data assertions over 2 temporary synthetic rows: real ES writes, Fission queries, pagination, filters, signed aggregation and exact-ID cleanup; no inference or harvesting |
| social-api-feasibility-2026-10-01.json | Bounded public Mastodon/Bluesky samples with statuses, date ranges and rule-based candidate counts; not a census, not relevance accuracy or verified residence |
| search-reuse-2026-10-01.json | Existing keyword bank adapted to public hashtags; 1,680 returns, 983 canonical-URI unique posts, 809 in-window candidates retained across all geographic-hint groups; 809 cloud raw writes/read-backs and Fission quality query, no inference |
| search-reuse-review-2026-10-01.json | 45 assistant-reviewed stratified samples with hashed document identities and paraphrased notes; no raw post text or author residence labels, not human gold labels |

## Conditions

Elasticsearch 8.19.1 ran in local Docker with a 512 MiB heap and 2 GiB container
limit. The benchmark index had one shard and zero replicas. The Windows Python
client reported 22 logical CPUs; that does not mean ES was allocated 22 CPUs.
The model benchmark explicitly used two PyTorch CPU threads.

The ES latency test uses five warm-up queries followed by 100 repeated queries
at concurrency four. Results can benefit from caches. It excludes Fission/API
routing and neural inference. Python tracemalloc measures traced Python
allocations, not process RSS or Elasticsearch memory.

The bulk speedup compares the same 200 records, with a fresh index for each
variant, in one trial. Model throughput excludes loading and clears prediction
caches. Neither comparison provides repeated-run uncertainty or a production SLA.

## Reproduce

Run from the root after dependency sync with model and notebook extras.

~~~powershell
docker compose --profile demo up -d --build --wait
$env:TEST_ES_URL='http://127.0.0.1:19200'
$env:TEST_REDIS_URL='redis://127.0.0.1:16379/0'
$env:RUN_MODEL_TESTS='1'
$env:OMP_NUM_THREADS='2'
$env:MKL_NUM_THREADS='2'
uv run --no-sync pytest --tb=short -q --junitxml=artifacts/test-results.xml --cov=backend --cov-report=xml:artifacts/coverage.xml
uv run --no-sync python -m scripts.validate_deployment
uv run --no-sync python -m scripts.audit_repository
uv run --no-sync python -m scripts.execute_notebook

$env:ES_HOST='http://127.0.0.1:19200'
$env:ES_ALLOW_ANONYMOUS='true'
uv run --no-sync python -m scripts.benchmark --records 100000 --batch-size 500 --queries 100 --concurrency 4 --output artifacts/benchmark-100k.json
uv run --no-sync python -m scripts.benchmark_model
docker build -f deploy/Dockerfile.fission -t transport-fission:review .
uv run --no-sync python -m scripts.smoke_fission
~~~

Check the benchmark CLI's output defaults when changing filenames. Benchmarks
create fresh bench_ prefixes; retain or remove only those exact owned indices
deliberately. Do not delete production indices to repeat an experiment.

Live capture is not deterministic. Future counts, accessible APIs and text
distribution will differ. The retained report describes the completed bounded
run, not a promise of the same rate or relevance in future collection.

Cloud acceptance reproduction commands are in [the Fission runbook](../CLOUD_FISSION_API.zh-CN.md).
The new cloud API unit guards ran with the existing suite: 130 passed, 10 deselected
(`pytest -m 'not integration and not model'`). The older verification.json remains
the original full local test measurement; it is not overwritten with unit-only results.

The subsequent query-reuse change passed 146 unit/real-service tests with
`pytest -m 'not model'` (one neural-model test deselected). Its cloud pilot evidence
and assistant review labels are separate from the earlier local benchmark files.

## Completed experiment and retirement evidence

- `event-experiment-2026-10-01.json`: full frozen event-window aggregate results.
- `event-api-cutover-2026-10-01.json`: historical cloud deployment/default-model checks.
- `archive-restore-verification.json`: 18 indices / 434,092 full-document fingerprints matched after local restore.
- `local-replay-api-parity.json`: 11 cloud/local aggregate responses matched.
- `local-replay-routes.json`: local pagination, baseline and route checks.
- `offline-analysis-verification.json`: no-network recomputation and unlabelled human-review task counts.
- `replay-dashboard-snapshot.json`: aggregate-only frozen browser responses; no original posts.
- `cloud-retirement.json`: current resource-cleanup status; only this distinguishes migration from stopped billing.

Private originals, provider responses and backup ZIPs stay in ignored local directories.
