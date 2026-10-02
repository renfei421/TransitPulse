"""Fission-triggered jobs share validated parameters, immutable image and Secret references."""
import json
import os
from pathlib import Path
import ssl
from urllib.request import Request, urlopen
from uuid import uuid4

from backend.common.logging import get_logger
from backend.common.time import previous_day, timestamp
log = get_logger("job_trigger")
MODULES = {
    "social": "backend.data_process.cloud_sentiment.cloud_sentiment_pipeline",
    "news": "backend.data_process.cloud_sentiment.news_sentiment_pipeline",
}


def processing_args(parameters):
    if set(parameters) - {"start_date", "end_date", "window_hours", "dry_run", "context_max_depth"}:
        raise ValueError("Unknown job parameter")
    start, end = parameters.get("start_date"), parameters.get("end_date")
    if start or end:
        if not start or not end or timestamp(start) >= timestamp(end):
            raise ValueError("Provide an ordered start_date and end_date pair")
        if (timestamp(end)-timestamp(start)).days > 367:
            raise ValueError("Backfill window exceeds 367 days")
        args = ["--start-date", timestamp(start).isoformat(), "--end-date", timestamp(end).isoformat()]
    else:
        hours = float(parameters.get("window_hours", 48))
        if not 1 <= hours <= 24*31:
            raise ValueError("window_hours must be 1..744")
        # Overlap absorbs delayed fetches/restarts. Writes are idempotent.
        args = ["--window-hours", str(hours)]
    args += ["--time-field", "fetched_at"]
    if parameters.get("dry_run", "false").lower() == "true":
        args.append("--dry-run")
    return args


def job_manifest(kind, parameters=None):
    if kind not in MODULES:
        raise ValueError("Unknown processor")
    parameters = parameters or {}
    args = processing_args(parameters)
    if kind == "social":
        depth = int(parameters.get("context_max_depth", "5"))
        if not 0 <= depth <= 20:
            raise ValueError("context_max_depth must be 0..20")
        args += ["--context-max-depth", str(depth)]
    image = os.environ["SENTIMENT_JOB_IMAGE"]
    if image.endswith(":latest") or (":" not in image and "@sha256:" not in image):
        raise ValueError("SENTIMENT_JOB_IMAGE must have an immutable release tag or digest")
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"transport-{kind}-{uuid4().hex[:12]}",
                     "namespace": os.getenv("JOB_NAMESPACE", "default"),
                     "labels": {"app.kubernetes.io/part-of": "transport-analytics", "component": kind}},
        "spec": {"backoffLimit": 2, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 86400,
            "template": {"metadata": {"labels": {"app": "transport-processing"}}, "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001},
                "containers": [{"name": "processor", "image": image,
                    "command": ["python", "-m", MODULES[kind]], "args": args,
                    "envFrom": [{"configMapRef": {"name": "transport-config"}},
                                {"secretRef": {"name": "transport-secrets"}}],
                    "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                  "limits": {"cpu": "2", "memory": "3Gi"}},
                    "securityContext": {"allowPrivilegeEscalation": False,
                                         "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "es-ca", "mountPath": "/etc/es-ca", "readOnly": True}],
                }], "volumes": [{"name": "es-ca", "secret": {"secretName": "transport-es-ca"}}]
            }}}}


def trigger(kind):
    from flask import request
    try:
        manifest = job_manifest(kind, request.args.to_dict())
    except (ValueError, KeyError):
        return {"error": "Invalid job parameters or deployment configuration"}, 400
    namespace = manifest["metadata"]["namespace"]
    service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    try:
        req = Request(f"https://kubernetes.default.svc/apis/batch/v1/namespaces/{namespace}/jobs",
            data=json.dumps(manifest).encode(), method="POST",
            headers={"Authorization": "Bearer " + (service_account/"token").read_text().strip(),
                     "Content-Type": "application/json"})
        context = ssl.create_default_context(cafile=str(service_account/"ca.crt"))
        with urlopen(req, context=context, timeout=30) as result:
            body = json.load(result)
        log.info("job_created", extra={"fields": {"name": body["metadata"]["name"], "kind": kind}})
        return {"status": "created", "job": body["metadata"]["name"], "namespace": namespace}, 201
    except Exception:
        log.exception("job_creation_failed")
        return {"error": "Unable to create processing job"}, 503


previous_local_day_range = previous_day
