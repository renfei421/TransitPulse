"""Submit one bounded CPU model Job without requiring a private image registry.

Acceptance deployment: pinned Python image, hash-locked dependencies, verified
source archive, emptyDir model cache, then offline inference. No app code goes in
a ConfigMap, no new disks/nodes are provisioned, and no credentials are packaged.
For recurring production use, publish the existing Dockerfile's model target.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import time
import uuid

from scripts.cloud_elasticsearch import Cluster, CONTEXT

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d"


def package():
    files = [ROOT/"requirements-model.txt", ROOT/"deploy/cloud/bootstrap-model.sh"]
    for folder in ("backend", "database"):
        files.extend(p for p in (ROOT/folder).rglob("*") if p.is_file() and p.suffix in {".py", ".json"}
                     and "__pycache__" not in p.parts and p.name != "config_private.py")
    data = io.BytesIO()
    with gzip.GzipFile(fileobj=data, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as archive:
        for path in sorted(files):
            content = path.read_bytes().replace(b"\r\n", b"\n")
            entry = tarfile.TarInfo("release/"+path.relative_to(ROOT).as_posix())
            entry.size, entry.mode, entry.mtime = len(content), 0o644, 0
            archive.addfile(entry, io.BytesIO(content))
    checksum = hashlib.sha256(data.getvalue()).hexdigest()
    target = ROOT/f"artifacts/model-release-{checksum[:16]}.tar.gz"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(data.getvalue())
    return target, checksum


def manifest(name, checksum, datasets, start, end, max_documents):
    args = ["--start-date", start, "--end-date", end, "--time-field", "fetched_at",
            "--dataset-kind", "live", "--eligibility-policy", "global-en-v1", "--local-only",
            "--max-documents", str(max_documents), "--thread-batch-size", "32",
            "--scan-size", "100", "--inference-batch-size", "8", "--truncation-side", "right"]
    for dataset in datasets:
        args.extend(["--source-dataset", dataset])
    env = [{"name": k, "value": v} for k, v in {
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/work/vendor:/work/release",
        "HF_HOME": "/work/hf-cache", "HOME": "/work", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2", "TOKENIZERS_PARALLELISM": "false", "RELEASE_SHA256": checksum,
        "SENTIMENT_CACHE_SIZE": "128", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }.items()]
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    mounts = [{"name": "work", "mountPath": "/work"}, {"name": "tmp", "mountPath": "/tmp"}]
    bootstrap = ('until test -f /work/release.ready; do sleep 2; done\n'
                 'printf "%s  /work/release.tar.gz\\n" "$RELEASE_SHA256" | sha256sum -c -\n'
                 'tar -xzf /work/release.tar.gz -C /work\n'
                 'exec sh /work/release/deploy/cloud/bootstrap-model.sh')
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "default",
        "labels": {"app.kubernetes.io/part-of": "transport-analytics", "component": "model-acceptance"}},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 86400,
        "template": {"metadata": {"labels": {"app": "transport-model-acceptance"}}, "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "initContainers": [{"name": "prepare", "image": IMAGE, "command": ["sh", "-ec", bootstrap],
                "env": env, "securityContext": security, "volumeMounts": mounts,
                "resources": {"requests": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "2Gi"},
                              "limits": {"cpu": "2", "memory": "3Gi", "ephemeral-storage": "7Gi"}}}],
            "containers": [{"name": "processor", "image": IMAGE,
                "command": ["python", "-m", "backend.data_process.cloud_sentiment.cloud_sentiment_pipeline"],
                "args": args, "env": env + [{"name": "HF_HUB_OFFLINE", "value": "1"}],
                "envFrom": [{"configMapRef": {"name": "transport-config"}},
                            {"secretRef": {"name": "transport-secrets"}}],
                "securityContext": security, "volumeMounts": mounts + [
                    {"name": "es-ca", "mountPath": "/etc/es-ca", "readOnly": True}],
                "resources": {"requests": {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "2Gi"},
                              "limits": {"cpu": "2", "memory": "3Gi", "ephemeral-storage": "7Gi"}}}],
            "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "6Gi"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                        {"name": "es-ca", "secret": {"secretName": "transport-es-ca"}}]}}}}


def submit(cluster, datasets, start, end, max_documents):
    archive, checksum = package()
    name = "transport-model-"+uuid.uuid4().hex[:10]
    spec = manifest(name, checksum, datasets, start, end, max_documents)
    evidence = {"job": name, "context": cluster.context, "release_sha256": checksum,
                "archive": str(archive.relative_to(ROOT)), "source_bytes": archive.stat().st_size,
                "manifest": spec, "state": "submitted"}
    out = ROOT/f"artifacts/{name}.json"
    out.write_text(json.dumps(evidence, indent=2)+"\n")
    cluster.run("create", "-f", "-", data=json.dumps(spec))
    print(json.dumps({"job": name, "evidence": str(out), "state": "waiting_for_upload"}), flush=True)
    deadline = time.monotonic()+300
    while time.monotonic() < deadline:
        pods = json.loads(cluster.run("get", "pods", "-n", "default", "-l", "job-name="+name, "-o", "json"))["items"]
        pod = next((p for p in pods if any("running" in c.get("state", {})
                    for c in p.get("status", {}).get("initContainerStatuses", []))), None)
        if pod:
            break
        time.sleep(3)
    else:
        raise TimeoutError("Job init container did not start; inspect events using the saved job name")
    pod_name = pod["metadata"]["name"]
    subprocess.run(cluster.command + ["cp", str(archive.relative_to(ROOT)),
        f"default/{pod_name}:/work/release.tar.gz", "-c", "prepare"], cwd=ROOT, check=True, timeout=90)
    cluster.run("exec", "-n", "default", pod_name, "-c", "prepare", "--", "touch", "/work/release.ready")
    evidence.update(pod=pod_name, state="release_uploaded")
    out.write_text(json.dumps(evidence, indent=2)+"\n")
    print(json.dumps({"job": name, "pod": pod_name, "release_sha256": checksum, "state": evidence["state"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--source-dataset", action="append", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-documents", type=int, default=2000)
    args = parser.parse_args()
    submit(Cluster(args.kubeconfig, CONTEXT), args.source_dataset, args.start, args.end, args.max_documents)
