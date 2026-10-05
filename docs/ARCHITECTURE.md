# Architecture and operational boundaries

> Historical implementation-stage document. For the completed experiment and actually executed architecture, see [TransitPulse case study](PROJECT_CASE_STUDY.zh-CN.md) and [archive/replay runbook](ARCHIVE_AND_REPLAY.zh-CN.md). Earlier pending items and counts are not current status.


## Data flow

~~~mermaid
flowchart LR
  A[Bluesky search / Mastodon] --> H[Bounded Fission harvest functions]
  J[Jetstream legacy v1] --> L[Cursor-based capture]
  F[Licensed historical NDJSON] --> I[Resumable importer]
  G[GDELT attention and article discovery] --> N[Budgeted news collection]
  E[EIA Brent observations] --> B[Daily ingestion]
  H --> R[(Elasticsearch raw indices)]
  L --> R
  I --> R
  N --> R
  B --> R
  R --> S[Scheduled Fission-triggered processing Jobs]
  R --> P[Optional overlap-window publisher]
  P --> Q[(Redis Streams + AOF PVC)]
  Q --> W[Model workers]
  W --> D[Retry / dead-letter stream]
  D --> Q
  S --> O[(Versioned processed indices)]
  W --> O
  O --> C[Calendar-aligned correlation CronJob]
  R --> C
  C --> X[(Derived results)]
  O --> API[Fission API v1]
  X --> API
  R --> API
  API --> V[Notebook / aggregate HTML report]
  Q -. backlog metrics .-> K[KEDA 1–4 workers]
~~~

The daily oil/news alignment remains a scheduled batch operation. Queueing is
optional for bursty, expensive social inference. Enable queue mode in the
renderer to remove the daily social processing trigger and avoid duplicate
schedules. News continues as a daily Job. A manual full reprocessing command
remains available for schema/rule changes and old records outside the overlap.

## Runtime and storage

| Component | Local verification | Cluster design / prerequisites |
|---|---|---|
| Elasticsearch | One Docker node, 512 MiB JVM heap, 2 GiB container limit, named volume | Existing TLS-enabled ES service; migrate v2_ indices; default 1 shard and 1 replica; at least two data nodes needed for green replica allocation |
| Redis | Redis 7.4.5, AOF, named volume | Optional single-replica StatefulSet, 5 GiB RWO PVC, password Secret; no HA claim |
| Fission API | Actual Fission Python image specialized and queried in Docker | Two environments separate API/harvest from job-creation service account; API min 1/max 3 with CPU HPA |
| Model | CPU PyTorch, pinned CardiffNLP weights baked into image | Job/worker request 0.5 CPU/1 GiB, limit 2 CPU/3 GiB; reserve capacity before using four workers |
| Migration | Explicit database.migrate | Separate release Job with schema privileges; ordinary runtime must not create mappings |
| Model cache | Host HF cache and image layer | Readable immutable /opt/model-cache; offline inference after image build |
| Imported data/checkpoints | Local ignored data/ directory | Mount persistent storage for recurring imports/capture; do not use an ephemeral pod filesystem for durable cursors |

Node names, actual cluster topology, PV type and network policy were **not
verified** against the unreachable MRC cluster. These are deployment requirements,
not claims about infrastructure that exists. Four model workers at their limits
alone may require 8 vCPU and 12 GiB, in addition to ES, Fission and system pods.

## Delivery semantics

Raw writes are upserts keyed by stable source identity. Bulk HTTP 200 is not
sufficient: every item outcome contributes to created/updated/noop/failed counts.
Imports advance a file-hash/config-bound checkpoint only after successful writes.
A crash can replay the last batch without adding duplicate document IDs.

The optional Redis queue has one consumer group. Enqueue+dedup is atomic.
Workers write Elasticsearch before ACK. Abandoned pending tasks are reclaimed
after five minutes; three failed processing attempts move a task to a durable
dead-letter stream. Replay is explicit. Successful entries are ACKed and deleted;
do not add another group without redesigning retention.

This is **at-least-once processing with idempotent sinks**, not exactly-once
execution. AOF everysec can lose approximately the most recent second after an
unclean Redis failure. The 48-hour overlap publisher and a raw-data reprocessing
run provide recovery; Redis is not the system of record. Large full backfills
outside 48 hours require an explicit publisher window or batch processing.

KEDA considers both undelivered lag and pending tasks. Minimum replicas is one
so abandoned pending tasks can always be reclaimed. Scale-to-zero and HA Redis
are deliberately not claimed.

## Analytical contract

- APIs accept inclusive Sydney calendar dates; internal processing uses half-open timestamps.
- Event/created time and fetched time remain separate; late arrivals are processed by fetched time.
- Net sentiment uses label counts. Confidence is the probability/proportion for a class.
- Model revision, pipeline/schema version, direct/inherited topics and source dataset are retained.
- API default observed cohort excludes synthetic/fixture records.
- Coverage uses matching source/date/platform populations before topic filtering; raw posts lack inherited labels.
- Correlation uses exact calendar shifts, minimum 14 pairs, no missing-day compression or price interpolation.
- Lag-search p-values are exploratory; Bonferroni does not correct serial dependence or confounding.
