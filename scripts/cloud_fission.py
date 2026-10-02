"""Publish and verify only the API on the prepared Fission acceptance cluster."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import requests
import yaml

from scripts.cloud_elasticsearch import Cluster, CONTEXT, until
from scripts.package_cloud_api import RUNTIME
from scripts.render_deployment import generate, write_documents

ROOT = Path(__file__).resolve().parents[1]


def render():
    directory = ROOT/"artifacts/cloud-api-specs"
    directory.mkdir(parents=True, exist_ok=True)
    archive = ROOT/"artifacts/transport-api-linux.zip"
    if not archive.is_file():
        raise ValueError("Build the Linux API archive with scripts.package_cloud_api first")
    groups = generate("transport-app:review", "transport-model:review", RUNTIME, es_replicas=0)
    config = next(r for r in groups["core"] if r["kind"] == "ConfigMap")
    # The API has no reason to receive model-job or Redis settings.
    config["data"] = {k: v for k, v in config["data"].items() if k.startswith("ES_") or k == "LOG_LEVEL"}
    # Accepted event cohort is served by default; explicit model queries retain
    # the baseline index. Keep this in the renderer so redeployment preserves it.
    config["data"]["SOCIAL_ACTIVE_MODEL"] = "jev-1.13.0"
    write_documents(directory/"core.yaml", [config])
    selected = []
    names = {"transport-python", "transport-python-code", "transport-api", "transport-api-v1", "transport-api-compat"}
    for obj in groups["fission"]:
        if obj["kind"] == "ArchiveUploadSpec":
            obj["include"] = [str(archive)]
            selected.append(obj)
        elif obj.get("metadata", {}).get("name") in names:
            if obj["kind"] == "Function":
                obj["spec"]["package"]["functionName"] = "cloud_entrypoint.main"
            if obj["kind"] == "Environment":
                obj["spec"]["runtime"]["podspec"]["automountServiceAccountToken"] = False
                # envFrom values are resolved only at Pod creation. A configuration
                # digest changes the runtime template when non-secret settings change.
                digest = hashlib.sha256(json.dumps(config["data"], sort_keys=True).encode()).hexdigest()
                for container in obj["spec"]["runtime"]["podspec"]["containers"]:
                    container.setdefault("env", []).append({"name": "TRANSPORT_CONFIG_DIGEST", "value": digest})
            selected.append(obj)
    specs = directory/"specs"
    write_documents(specs/"application.yaml", selected)
    (specs/"fission-deployment-config.yaml").write_bytes((ROOT/"specs/fission-deployment-config.yaml").read_bytes())
    return directory


def deploy(cluster):
    directory = render()
    for name in ("transport-secrets", "transport-es-ca"):
        if not cluster.get("secret", name, "default", optional=True):
            raise ValueError("Missing application Secret: " + name)
    cluster.run("apply", "-f", str(directory/"core.yaml"))
    env = os.environ.copy()
    env["KUBECONFIG"] = cluster.command[2]
    env["FISSION_NAMESPACE"] = "fission"
    for action, flags in (("validate", []), ("apply", ["--wait"])):
        subprocess.run(["fission", "--kube-context", cluster.context, "spec", action,
                        "--specdir", str(directory/"specs"), *flags], env=env, cwd=ROOT,
                       check=True, timeout=300)


@contextmanager
def router(cluster, *, startup_timeout=240):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(cluster.command+["-n", "fission", "port-forward", "service/router",
                              f"{port}:80", "--address=127.0.0.1"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    session = requests.Session()
    session.trust_env = False
    url = f"http://127.0.0.1:{port}"
    try:
        def available():
            if process.poll() is not None:
                raise RuntimeError("Router port-forward exited")
            try:
                return session.get(url+"/api/v1/meta", timeout=10).status_code == 200
            except (requests.ConnectionError, requests.Timeout):
                return False
        until("Fission API specialization and router", available, timeout=startup_timeout)
        yield session, url
    finally:
        session.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def verify(cluster):
    checks = []
    with router(cluster) as (session, base):
        for path, expected in (
            ("/api/v1/meta", 200), ("/api/v1/health", 200), ("/api/v1/openapi.json", 200),
            ("/api/v1/social/volume", 200), ("/api/v1/social/sentiment", 200),
            ("/api/v1/social/posts", 200), ("/api/v1/news/volume", 200),
            ("/api/v1/news/sentiment", 200), ("/api/v1/oil/prices", 200),
            ("/api/v1/platforms/profiles", 200), ("/api/v1/quality", 200),
            # An exact, otherwise unused model avoids depending on an empty database.
            ("/api/v1/analyses/oil-sentiment?model=cloud-acceptance-nonexistent", 404),
            ("/api/v1/social/posts?limit=101", 400), ("/api/v1/unknown", 404),
            ("/api/social-topic-volume", 200),
        ):
            started = time.perf_counter()
            response = session.get(base+path, timeout=40)
            if response.status_code != expected:
                raise RuntimeError(f"{path}: expected {expected}, got {response.status_code}")
            body = response.json()
            request_id = bool(response.headers.get("X-Request-ID"))
            envelope = "data" in body or "error" in body or "openapi" in body
            if not envelope or (not path.endswith("openapi.json") and not request_id):
                raise RuntimeError("Invalid API response envelope or missing request ID: " + path)
            checks.append({"path": path, "status": response.status_code,
                           "duration_ms": round((time.perf_counter()-started)*1000, 2),
                           "request_id_present": request_id, "has_data_or_error": envelope})
    pods = json.loads(cluster.run("get", "pods", "-A", "-o", "json"))["items"]
    relevant = [p for p in pods if p["metadata"]["namespace"] == "fission" or
                p["metadata"].get("labels", {}).get("functionName") == "transport-api"]
    hpas = json.loads(cluster.run("get", "hpa", "-n", "default", "-o", "json"))["items"]
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "context": cluster.context,
        "fission_version": "1.23.0", "metrics_api_available": True,
        "api_checks": checks, "pods": [{"name": p["metadata"]["name"], "namespace": p["metadata"]["namespace"],
            "ready": any(c["type"] == "Ready" and c["status"] == "True"
                         for c in p["status"].get("conditions", [])),
            "images": [{"name": c["name"], "image_id": c.get("imageID")} for c in p["status"].get("containerStatuses", [])]}
            for p in relevant],
        "hpa": [{"name": h["metadata"]["name"], "spec": h["spec"], "status": h.get("status", {})} for h in hpas],
        "package": json.loads((ROOT/"artifacts/cloud-api-package.json").read_text(encoding="utf-8")),
        "limits": ["Requests used an authenticated kubectl tunnel; no public Internet endpoint was created",
                   "No production throughput or autoscaling-under-load claim",
                   "Read-only route checks do not establish historical-data recovery or ingestion"]}
    api = cluster.get("apiservice", "v1beta1.metrics.k8s.io")
    result["metrics_api_available"] = any(c["type"] == "Available" and c["status"] == "True" for c in api["status"]["conditions"])
    if not result["metrics_api_available"] or not all(p["ready"] for p in result["pods"]):
        raise RuntimeError("A Fission Pod or Metrics API is not ready")
    out = ROOT/"artifacts/cloud-fission-api-verification.json"
    out.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8", newline="\n")
    print(json.dumps({"api_checks_passed": len(checks), "evidence": str(out)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "verify"))
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default=CONTEXT)
    args = parser.parse_args()
    cloud = Cluster(args.kubeconfig, args.context)
    (deploy if args.action == "deploy" else verify)(cloud)
