"""Render release-specific Fission specs and Kubernetes resources from one definition.

Generation does not contact or modify a cluster. Images must be supplied before
deploying; checked-in examples use explicit local review tags.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
LABELS = {"app.kubernetes.io/part-of": "transport-analytics"}


def resource(kind, name, spec=None, namespace="default", api="v1", **extra):
    result = {"apiVersion": api, "kind": kind,
              "metadata": {"name": name, "namespace": namespace, "labels": LABELS}, **extra}
    if spec is not None:
        result["spec"] = spec
    return result


def container(image, module, args=None, model=False):
    return {"name": "transport", "image": image, "command": ["python", "-m", module], "args": args or [],
        "envFrom": [{"configMapRef": {"name": "transport-config"}}, {"secretRef": {"name": "transport-secrets"}}],
        "resources": {"requests": {"cpu": "500m" if model else "100m", "memory": "1Gi" if model else "256Mi"},
                      "limits": {"cpu": "2" if model else "1", "memory": "3Gi" if model else "1Gi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "es-ca", "mountPath": "/etc/es-ca", "readOnly": True}]}


def pod(image, module, args=None, model=False):
    return {"restartPolicy": "Never", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001},
            "containers": [container(image, module, args, model)],
            "volumes": [{"name": "es-ca", "secret": {"secretName": "transport-es-ca"}}]}


def job(image, module, args=None):
    return {"backoffLimit": 2, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 86400,
            "template": {"metadata": {"labels": LABELS}, "spec": pod(image, module, args)}}


def generate(image, model_image, fission_image, namespace="default", runtime_namespace=None, queue_mode=False,
             es_replicas=1):
    if es_replicas not in range(4):
        raise ValueError("es_replicas must be between 0 and 3")
    runtime_namespace = runtime_namespace or namespace
    config = {"ES_HOST": "https://elasticsearch-es-http.elastic.svc:9200",
              "ES_USER": "transport-app", "ES_VERIFY_CERTS": "true", "ES_CA_CERT": "/etc/es-ca/ca.crt",
              "ES_INDEX_PREFIX": "v2_", "JOB_NAMESPACE": namespace, "SENTIMENT_JOB_IMAGE": model_image,
              "QUEUE_PREFIX": "transport", "REDIS_ADDRESS": "transport-redis:6379",
              "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "LOG_LEVEL": "INFO"}
    core = [resource("ConfigMap", "transport-config", namespace=namespace, data=config),
            resource("ServiceAccount", "transport-job-trigger", namespace=runtime_namespace),
            resource("Role", "transport-job-creator", namespace=namespace, api="rbac.authorization.k8s.io/v1",
                     rules=[{"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create"]}]),
            resource("RoleBinding", "transport-job-creator", namespace=namespace, api="rbac.authorization.k8s.io/v1",
                     roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "transport-job-creator"},
                     subjects=[{"kind": "ServiceAccount", "name": "transport-job-trigger", "namespace": runtime_namespace}])]
    if runtime_namespace != namespace:
        core.append(resource("ConfigMap", "transport-config", namespace=runtime_namespace, data=config))
    migration = resource("Job", "transport-schema-migration", job(image, "database.migrate", ["--replicas", str(es_replicas)]),
                         namespace, "batch/v1")
    migration["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "transport-config"}},
        {"secretRef": {"name": "transport-migration-secrets"}},
    ]
    cron = []
    for name, schedule, module, arguments in (
        ("health", "15 * * * *", "backend.health_check.health_check", []),
        ("correlation", "45 3 * * *", "backend.oil_sentiment_corr.compute", []),
        ("brent", "30 2 * * 2-6", "backend.brent_ingest.fetch_brent", []),
        ("news-volume", "0 2 * * *", "backend.news_harvester.volume", []),
        ("news-articles", "15 2 * * *", "backend.news_harvester.articles", ["--target-articles", "300", "--time-budget-minutes", "30"]),
    ):
        cron.append(resource("CronJob", "transport-"+name, {
            "schedule": schedule, "timeZone": "Australia/Sydney", "concurrencyPolicy": "Forbid",
            "startingDeadlineSeconds": 1800, "successfulJobsHistoryLimit": 2, "failedJobsHistoryLimit": 3,
            "jobTemplate": {"spec": job(image, module, arguments)}}, namespace, "batch/v1"))

    fission = []
    for env_name, trigger in (("transport-python", False), ("transport-trigger-python", True)):
        runtime_container = {"name": env_name, "envFrom": [
            {"configMapRef": {"name": "transport-config"}}, {"secretRef": {"name": "transport-secrets"}}],
            "volumeMounts": [{"name": "es-ca", "mountPath": "/etc/es-ca", "readOnly": True}]}
        podspec = {"containers": [runtime_container],
                   "volumes": [{"name": "es-ca", "secret": {"secretName": "transport-es-ca"}}]}
        if trigger:
            podspec["serviceAccountName"] = "transport-job-trigger"
        fission.append(resource("Environment", env_name, {
            "version": 3, "poolsize": 0,
            "runtime": {"image": fission_image, "container": {"name": env_name}, "podspec": podspec},
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                          "limits": {"cpu": "1", "memory": "512Mi"}}}, namespace, "fission.io/v1"))
    fission.append({"kind": "ArchiveUploadSpec", "name": "transport-code",
                    "include": ["artifacts/transport-functions.zip"]})
    # Separate packages let Fission associate each deployment with the correct environment.
    for environment in ("transport-python", "transport-trigger-python"):
        fission.append(resource("Package", environment+"-code", {
            "environment": {"name": environment, "namespace": namespace},
            "deployment": {"type": "url", "url": "archive://transport-code"}},
            namespace, "fission.io/v1", status={"buildstatus": "succeeded"}))
    functions = {
        "transport-api": ("backend.api.app.main", False, 30),
        "transport-social-job": ("backend.data_process.cloud_sentiment.fission_job_trigger.main", True, 40),
        "transport-news-job": ("backend.data_process.cloud_sentiment.fission_news_trigger.main", True, 40),
        "transport-bluesky": ("backend.parallel_harvester.app.main_bluesky", False, 900),
        "transport-mastodon": ("backend.parallel_harvester.app.main_mastodon", False, 900),
    }
    for name, (entrypoint, trigger, timeout) in functions.items():
        env = "transport-trigger-python" if trigger else "transport-python"
        fission.append(resource("Function", name, {
            "environment": {"name": env, "namespace": namespace},
            "package": {"functionName": entrypoint, "packageref": {"name": env+"-code", "namespace": namespace}},
            "functionTimeout": timeout, "idletimeout": 120, "concurrency": 20 if name == "transport-api" else 1,
            "requestsPerPod": 20 if name == "transport-api" else 1,
            "InvokeStrategy": {"StrategyType": "execution", "ExecutionStrategy": {
                "ExecutorType": "newdeploy", "MinScale": 1 if name == "transport-api" else 0,
                "MaxScale": 3 if name == "transport-api" else 1, "TargetCPUPercent": 70,
                "SpecializationTimeout": 120}},
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                          "limits": {"cpu": "1", "memory": "512Mi"}}}, namespace, "fission.io/v1"))
    for name, prefix in (("api-v1", "/api/v1"), ("api-compat", "/api")):
        fission.append(resource("HTTPTrigger", "transport-"+name, {
            "prefix": prefix, "keepPrefix": True, "methods": ["GET"],
            "functionref": {"type": "name", "name": "transport-api"},
            "createingress": False}, namespace, "fission.io/v1"))
    for name, schedule in (("social-job", "0 0 18 * * *"), ("news-job", "0 15 18 * * *"),
                            ("bluesky", "0 0 */6 * * *"), ("mastodon", "0 30 */6 * * *")):
        if queue_mode and name == "social-job":
            continue
        fission.append(resource("TimeTrigger", "transport-"+name, {
            "cron": schedule, "functionref": {"type": "name", "name": "transport-"+name}},
            namespace, "fission.io/v1"))

    queue_pod = pod(model_image, "backend.ingestion.queue", ["worker"], model=True)
    queue_pod.update(restartPolicy="Always", terminationGracePeriodSeconds=330)
    queue_pod["initContainers"] = [container(image, "backend.ingestion.queue", ["init"])]
    queue_deploy = resource("Deployment", "transport-inference", {
        "replicas": 1, "selector": {"matchLabels": {"app": "transport-inference"}},
        "template": {"metadata": {"labels": {**LABELS, "app": "transport-inference"}}, "spec": queue_pod}},
        namespace, "apps/v1")
    keda = resource("ScaledObject", "transport-inference", {
        "scaleTargetRef": {"name": "transport-inference"}, "pollingInterval": 15,
        "cooldownPeriod": 300, "minReplicaCount": 1, "maxReplicaCount": 4,
        "triggers": [
            {"type": "redis-streams", "metadata": {"addressFromEnv": "REDIS_ADDRESS",
                "stream": "transport:inference", "consumerGroup": "sentiment",
                "lagCount": "100", "activationLagCount": "0"},
             "authenticationRef": {"name": "transport-redis-auth"}},
            {"type": "redis-streams", "metadata": {"addressFromEnv": "REDIS_ADDRESS",
                "stream": "transport:inference", "consumerGroup": "sentiment", "pendingEntriesCount": "10"},
             "authenticationRef": {"name": "transport-redis-auth"}}]}, namespace, "keda.sh/v1alpha1")
    auth = resource("TriggerAuthentication", "transport-redis-auth", {
        "secretTargetRef": [{"parameter": "password", "name": "transport-secrets", "key": "REDIS_PASSWORD"}]},
        namespace, "keda.sh/v1alpha1")
    publisher = resource("CronJob", "transport-publisher", {
        "schedule": "*/5 * * * *", "concurrencyPolicy": "Forbid",
        "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 2,
        "jobTemplate": {"spec": job(image, "backend.ingestion.queue", ["publish"])}}, namespace, "batch/v1")
    redis_service = resource("Service", "transport-redis", {
        "clusterIP": "None", "selector": {"app": "transport-redis"},
        "ports": [{"name": "redis", "port": 6379, "targetPort": 6379}]}, namespace)
    redis_stateful = resource("StatefulSet", "transport-redis", {
        "serviceName": "transport-redis", "replicas": 1,
        "selector": {"matchLabels": {"app": "transport-redis"}},
        "template": {"metadata": {"labels": {**LABELS, "app": "transport-redis"}}, "spec": {
            "automountServiceAccountToken": False,
            "containers": [{"name": "redis", "image": "redis:7.4.5-alpine",
                "command": ["sh", "-ec", 'exec redis-server --appendonly yes --appendfsync everysec --requirepass "$REDIS_PASSWORD"'],
                "env": [{"name": "REDIS_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "transport-secrets", "key": "REDIS_PASSWORD"}}}],
                "ports": [{"containerPort": 6379}],
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
                "volumeMounts": [{"name": "data", "mountPath": "/data"}]}]}},
        "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {
            "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "5Gi"}}}}]}, namespace, "apps/v1")
    return {"core": core, "migration": [migration], "cron": cron, "fission": fission,
            "queue": [redis_service, redis_stateful, queue_deploy, auth, keda, publisher]}


def write_documents(path, documents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")


def render(image="transport-app:review", model_image="transport-model:review",
           fission_image="transport-fission:review", namespace="default", runtime_namespace=None,
           output=".", queue_mode=False, es_replicas=1):
    for value in (image, model_image, fission_image):
        if value.endswith(":latest") or (":" not in value and "@sha256:" not in value):
            raise ValueError("Use release tags or digests, not latest")
    groups = generate(image, model_image, fission_image, namespace, runtime_namespace, queue_mode, es_replicas)
    output = Path(output)
    for name in ("core", "cron", "migration", "queue"):
        write_documents(output/f"deploy/generated/{name}.yaml", groups[name])
    write_documents(output/"specs/application.yaml", groups["fission"])
    if output.resolve() != ROOT:
        (output/"specs/fission-deployment-config.yaml").write_bytes((ROOT/"specs/fission-deployment-config.yaml").read_bytes())
    return groups


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="transport-app:review")
    parser.add_argument("--model-image", default="transport-model:review")
    parser.add_argument("--fission-image", default="transport-fission:review")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--runtime-namespace")
    parser.add_argument("--output", default=".")
    parser.add_argument("--queue-mode", action="store_true")
    parser.add_argument("--es-replicas", type=int, choices=range(4), default=1,
                        help="Index replicas for newly created indices; use 0 for single-node acceptance")
    render(**vars(parser.parse_args()))
