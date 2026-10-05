# Engineering review → implementation and evidence

This matrix distinguishes repository fixes from remote operational acceptance.
The original report is historical; the updated implementation is version 0.2.
Paths below are relative to the repository root.

| Feedback | Implemented response | Evidence / remaining boundary |
|---|---|---|
| CronJob alignment makes sense; absence of queues can be justified | Retained scheduled oil/news/correlation; made Redis inference queue optional | docs/ARCHITECTURE.md; queue mode removes the daily social trigger |
| Python script in oil-sentiment ConfigMap is bad practice | Code lives in packaged source and immutable release images; ConfigMaps contain configuration only | scripts/package_fission.py; specs/application.yaml; source audit and offline invariants |
| ES credentials must be Secrets; hard-coded credentials in es_client.py | Shared backend/common/settings.py and es.py; Secret/env/file inputs; TLS verification on by default | deploy/secrets.example.yaml; config tests; credentials were removed from current source, not retroactively revoked |
| Missing CI/CD code/documentation | GitLab stages for lint/audit/unit, ES/Redis integration/E2E, optional model, packaging, image build/publish, protected manual deploy | .gitlab-ci.yml; scripts/deploy_release.py; remote pipeline not executed in this work |
| Modest harvested volume; import authorized historical data | Streaming NDJSON/gzip adapter, fingerprint-bound checkpoints, date-partitioned search, live Jetstream capture, adaptive GDELT discovery | test/test_ingestion.py, test/test_services.py; 12 new real posts verified; no historical export file supplied |
| More data should exercise scale | Actual 100k synthetic ES ingestion/aggregation benchmark; separate fixed-model batch benchmark | docs/evidence/benchmark-100k.json and model-benchmark.json; not 100k harvested posts |
| No Redis / KEDA scalability scenario | Redis Streams dedup, pending reclaim, ES-before-ACK, retries/DLQ/replay; optional 1–4 worker KEDA spec | Real Redis+ES crash-before-ACK integration test; cloud autoscaling remains unverified |
| Error handling but no logging | JSON logging with run/request IDs, durations, component events and redacted failures; pipeline run records | backend/common/logging.py; harvester/processing/health/API logs; no deployed observability dashboard claimed |
| Good unit/integration but no E2E | Real HTTP path from raw thread + fetched parent context to sentiment and analytics; pagination, sample exclusion, compatibility and quality checks | test/test_services.py; 132 tests passed with real services and model enabled |
| Deployment does not use Fission specs | Deterministic source archive; declared Environment/Package/Function/HTTPTrigger/TimeTrigger | specs/; 32 resources checked offline with official Fission CRD schemas; actual runtime specialization passes |
| FastAPI instead of Fission / unused pod | Production route is a Fission function; lightweight Flask harness shares the same router for local demo/tests | backend/api/app.py; scripts/smoke_fission.py; old remote pod not deleted because cluster could not be inspected |
| API URLs not RESTful or discoverable | GET /api/v1/social/posts, /social/sentiment, /news/volume, /oil/prices, /analyses/oil-sentiment, /quality; OpenAPI and discovery links | Versioned resources, bounded cursor pagination, supported filters, 400/404/405/503; old public paths retained as deprecated adapters |
| Mapping creation inside compute/health scripts | All mappings centralized and applied explicitly by migration command/Job | database/migrate.py and database/mappings/; separate migration Secret/identity; pipeline handlers do not provision schemas |
| Duplicated blocks and low comment density | Shared source clients, thread builder, sentiment engine, settings/time/jobs/ES/logging; legacy serial/API entrypoints delegate | Comments explain retry/ACK ordering, calendar alignment and truncation instead of restating every line |
| Unversioned dependencies | Pinned direct dependencies, uv.lock, transitive hashed requirements, digest-pinned Python/Fission bases and fixed HF revision | Docker images built successfully; base/model/Fission requirements have different scopes |
| Architecture diagram lacked nodes/storage detail | Updated data flow, node/heap/resource assumptions, ES replicas, Redis AOF/PVC, namespaces/RBAC and source-of-record semantics | docs/ARCHITECTURE.md; actual remote topology is explicitly unverified |
| Generic data flow | Trace raw identity → ingestion timestamp → optional queue → model/schema provenance → processed index → API and notebook | Architecture and Operations distinguish event time, arrival time, retries, migration and recovery |
| RoBERTa choice not empirically justified | Fixed social-text baseline, explicit VADER comparison, grouped split and evaluation tooling | docs/EVALUATION.md; no human gold data was supplied, so superiority/accuracy is not claimed |
| Thread/reply analysis was promising | Correct topic inheritance, bounded ancestor fetch, cycle handling, missing-parent flag; negation precedes agreement rule | Regression tests + real ES/HTTP parent/child E2E; semantic quality still needs human evaluation |
| Nice graphs and example queries | Rebuilt Notebook around API v1, quality diagnostics, signed metrics, correct date-pair scatter and aggregate HTML | Seven code cells executed successfully; synthetic demo and observational limits labeled |

## Verification limits

- Test coverage of the entire backend is 50.98%; selected core modules have higher
  coverage. Historical offline scripts remain in the denominator.
- Fission CRD validation and container specialization are not a substitute for
  Kubernetes admission, storage provisioning, ingress and load testing.
- Redis is a single-instance optional design. Delivery is at least once with an
  idempotent sink; exactly-once inference, zero loss and HA are not claimed.
- Data-access failures are reported as failures, not converted to empty success.
  Bluesky historical search returned 403; EIA needs a key.
- No cloud resources were deleted or deployed during the unreachable-cluster phase.
- [Fission specs](https://fission.io/docs/usage/spec/) and
  [KEDA Redis Streams](https://keda.sh/docs/2.18/scalers/redis-streams/) are the
  upstream references used for the deployment design.
