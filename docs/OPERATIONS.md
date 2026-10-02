# Operations and reproducibility

For the current DigitalOcean recovery environment, start with the
[Elasticsearch cloud runbook](CLOUD_ELASTICSEARCH.zh-CN.md). Its single-node
acceptance topology requires `--es-replicas 0` when rendering an application
release (`ES_REPLICAS=0` in GitLab). The shared renderer retains a default of one
replica for multi-node deployments.

## Configuration and credentials

Use environment variables or NAME_FILE paths to mounted secret files.
backend/common/settings.py is authoritative. ES_PASSWORD is required unless
ES_ALLOW_ANONYMOUS=true is explicitly set for the local demo. TLS verification
defaults on; ES_CA_CERT points to the trusted CA. Credentials never come from
HTTP query parameters.

deploy/secrets.example.yaml is a template, not a deployable credential set.
Provide transport-secrets and transport-es-ca in the function runtime namespace
and job namespace if they differ. Source keys include BLUESKY_HANDLE,
BLUESKY_APP_PASSWORD, MASTODON_ACCESS_TOKEN and EIA_API_KEY.
Provide transport-migration-secrets (ES_USER and ES_PASSWORD) only in the job
namespace. The migration Job uses that separate identity; ordinary app pods do not.

Create a least-privilege ES application user for reads/writes on the selected
prefix. Provision mappings with a separate migration identity. Existing secrets
from old commits/archives should be rotated if still valid. Git history was not
rewritten. Do not commit a populated secret template, kubeconfig or private data.

For a private image registry, configure a persistent read_registry deploy token
in the runtime and job service accounts' imagePullSecrets. A short-lived
CI_JOB_TOKEN is not a durable pull credential for future CronJobs.

## Schema v2 migration

1. Snapshot/export existing data; keep old indices available.
2. Choose a fresh ES_INDEX_PREFIX, e.g. v2_. Do not retype old populated fields in place.
3. Run python -m database.migrate with the migration credentials.
4. Import raw social data with backend.ingestion.import_ndjson or re-collect it.
5. Reprocess social/news using the fixed model version.
6. Compare counts, missing context, model mixture and examples before switching the API prefix.

Mappings live only in database/mappings. Migration is additive and never deletes
indices. Incompatible existing field types fail visibly. In particular, legacy
Mastodon IDs were local numeric IDs; v2 capture/import scopes them by server.
Reimport into a fresh prefix, instead of mixing old and new identities.

Local timestamps use Australia/Sydney through zoneinfo; runtime images include
timezone data. For date-only oil observations, the publication date stays date-only.

## Local processing and export

~~~powershell
$env:ES_HOST='http://127.0.0.1:19200'
$env:ES_ALLOW_ANONYMOUS='true'
$env:ES_INDEX_PREFIX='v2_'
uv run python -m database.migrate
uv run --extra model python -m backend.data_process.cloud_sentiment.cloud_sentiment_pipeline --window-hours 48
uv run --extra model python -m backend.data_process.cloud_sentiment.news_sentiment_pipeline --window-hours 48
uv run python -m backend.oil_sentiment_corr.compute --from-date 2026-08-01 --to-date 2026-09-29
uv run python -m backend.data_process.export_es_index --index social_discussion_posts_raw --output data/exports/posts.ndjson
~~~

Use --no-transformers only for an explicitly identified VADER baseline.
A RoBERTa load failure is not silently replaced by another model.
For full history, omit window arguments. Use bounded batches and allow enough
time. A daily 48-hour overlap is recovery for recent arrivals, not a historical
reconciliation guarantee.

## Build and validate releases

~~~bash
uv run python -m scripts.package_fission
uv run python -m scripts.validate_deployment
docker build --target base -t REGISTRY/app:COMMIT .
docker build --target model -t REGISTRY/model:COMMIT .
docker build -f deploy/Dockerfile.fission -t REGISTRY/fission:COMMIT .
uv run python -m scripts.render_deployment --image REGISTRY/app:COMMIT --model-image REGISTRY/model:COMMIT --fission-image REGISTRY/fission:COMMIT --namespace default --runtime-namespace default
fission spec validate
~~~

The renderer does not contact a cluster. Default review tags are local examples;
use immutable commit tags/digests in a real registry.

Offline validation checks the vendored official Fission 1.23.0 CRD schemas,
resource references, API coverage and deployment invariants. Native Fission
validation also checks cluster conflicts and was blocked by the unavailable
cluster. The actual Fission runtime was validated separately:
python -m scripts.smoke_fission after building its image and starting the demo.

Confirm the actual Fission workload namespace in your installation before
rendering. The job-trigger ServiceAccount belongs there; its RoleBinding targets
the job namespace. Only it receives create-jobs permission.

## CI/CD and rollout

.gitlab-ci.yml provides lint/source audit, unit tests, ES/Redis integration/E2E,
optional real-model tests, archive packaging, conditional image publishing and
protected manual staging deployment. It has not been run in the remote GitLab
instance during this work.

Required CI settings:
- BUILD_IMAGES=true to publish commit-tagged images on the default branch.
- RUN_MODEL_CI=true to include the downloadable model test.
- A Docker-capable runner for image builds.
- A protected shell runner tagged transport-deploy with uv, kubectl and Fission.
- KUBECONFIG as a file variable; DEPLOY_NAMESPACE and FISSION_RUNTIME_NAMESPACE.
- Existing cluster secrets and image-pull credentials.

scripts/deploy_release.py validates first, applies non-secret config/RBAC,
runs a uniquely named migration Job, waits for success, applies Fission specs,
then CronJobs. Protect the staging environment/variables in GitLab.

Roll back by rendering/applying the preceding image tags and switching back to
the previous index prefix. Additive migrations do not erase the old prefix.
Do not run Fission destroy or broad ES deletion to roll back.

## Optional queue mode

~~~bash
python -m scripts.render_deployment --queue-mode --image REGISTRY/app:COMMIT --model-image REGISTRY/model:COMMIT --fission-image REGISTRY/fission:COMMIT
# After reviewing namespaces/secrets and installing KEDA 2.18+:
kubectl apply -f deploy/generated/queue.yaml
fission spec apply --wait
~~~

The queue manifest includes Redis AOF/PVC, publisher, model Deployment,
TriggerAuthentication and ScaledObject. It needs an available default storage
class and sufficient CPU/memory. Queue mode removes the daily social trigger;
news and correlation retain their schedules. The queue is optional for a small
daily dataset.

~~~powershell
$env:REDIS_URL='redis://127.0.0.1:16379/0'
uv run python -m backend.ingestion.queue publish --window-hours 48
uv run --extra model python -m backend.ingestion.queue worker
uv run python -m backend.ingestion.queue stats
uv run python -m backend.ingestion.queue replay-dead
~~~

Inspect dead-letter error types and fix the cause before replay. A crashed
consumer's work becomes reclaimable after idle-ms (default 300000). Set the idle
threshold above normal worst-case batch duration to limit duplicate inference.

## Monitoring and legacy cleanup

JSON logs include event, run/request ID, duration and safe error type. pipeline_runs
records processing outcomes; post_comment_crawler_runs_raw records harvesting
outcomes. health_log reports source freshness using fetched_at, with a longer
oil threshold for weekends/holidays. Stale data is a diagnostic, not proof that
the service is down. API /health tests ES readiness; it is distinct from freshness.

Inspect runtime pods/deployments, ownerReferences, services, endpoints and logs
before retiring the previously reported unused FastAPI workload. The new code
does not deploy it. It was not deleted from the unreachable cloud cluster.
