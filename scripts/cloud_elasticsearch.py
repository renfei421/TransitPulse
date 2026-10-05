"""Bootstrap and verify the short-lived DOKS Elasticsearch acceptance deployment.

Every Kubernetes operation uses an explicit kubeconfig and context. Credentials
stay in memory and Kubernetes Secrets, never command arguments or evidence files.
The optional restart check interrupts this single-node ES instance briefly.
"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

from elasticsearch import Elasticsearch
import requests
import yaml

from database.migrate import migrate

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "do-sgp1-k8s-1-34-10-do-5-sgp1-1790830229700"
OPERATOR_IMAGE = "docker.elastic.co/eck/eck-operator:3.5.0@sha256:b6f261372d9d9af7b00aab03efea25263314d16063c4d440ac322e52c2fdf314"
POD = "elasticsearch-es-acceptance-0"
CLAIM = "elasticsearch-data-" + POD
PREFIX = "v2_"
# ES 8.x's named write privilege also grants explicit mapping updates. Use pinned
# action privileges to separate schema administration, while retaining automatic
# mapping of new fields supported by the application's existing dynamic schemas.
APP_PRIVILEGES = ["read", "view_index_metadata", "indices:data/write/index*",
                  "indices:data/write/update*", "indices:data/write/bulk*",
                  "indices:admin/mapping/auto_put"]


class Cluster:
    def __init__(self, kubeconfig, context):
        config = yaml.safe_load(Path(kubeconfig).read_text(encoding="utf-8-sig"))
        selected = next(c["context"] for c in config["contexts"] if c["name"] == context)
        auth = next(u["user"] for u in config["users"] if u["name"] == selected["user"])
        endpoint = next(c["cluster"] for c in config["clusters"] if c["name"] == selected["cluster"])
        if auth.get("exec") or endpoint.get("insecure-skip-tls-verify"):
            raise ValueError("Use a TLS-verified kubeconfig without exec credential plugins")
        self.command = ["kubectl", "--kubeconfig", str(Path(kubeconfig).resolve()), "--context", context]
        self.context = context
        self.run("get", "namespace", "kube-system", "-o", "name")

    def run(self, *args, data=None, timeout=90):
        result = subprocess.run(self.command + ["--request-timeout=30s", *args],
            input=data, text=True, encoding="utf-8", capture_output=True, timeout=timeout)
        if result.returncode:
            # In particular, don't echo server error bodies for Secret writes.
            raise RuntimeError(f"kubectl {args[0]} failed (exit {result.returncode}); inspect resource status")
        return result.stdout

    def get(self, kind, name, namespace="elastic", optional=False):
        args = ["get", kind, name, "-n", namespace, "-o", "json"]
        if optional:
            args.append("--ignore-not-found")
        raw = self.run(*args)
        return json.loads(raw) if raw.strip() else None

    def apply(self, document):
        return self.run("apply", "--server-side", "--field-manager=transport-es-bootstrap",
                        "-f", "-", data=json.dumps(document))

    def secret(self, name, namespace="elastic"):
        obj = self.get("secret", name, namespace, optional=True)
        return {k: base64.b64decode(v).decode() for k, v in obj.get("data", {}).items()} if obj else {}

    def put_secret(self, name, data, namespace="default"):
        # Merge just the supplied keys; preserve unrelated source API keys.
        encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
        self.apply({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                    "metadata": {"name": name, "namespace": namespace}, "data": encoded})


def until(description, callback, timeout=600):
    deadline = time.monotonic() + timeout
    next_message = 0
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        if time.monotonic() >= next_message:
            print(f"Waiting: {description}", flush=True)
            next_message = time.monotonic() + 30
        time.sleep(3)
    raise TimeoutError(description)


def ready_pod(cluster, previous_uid=None):
    pod = cluster.get("pod", POD, optional=True)
    if not pod or pod["metadata"]["uid"] == previous_uid or pod["metadata"].get("deletionTimestamp"):
        return None
    return pod if any(c["type"] == "Ready" and c["status"] == "True"
                      for c in pod.get("status", {}).get("conditions", [])) else None


def install(cluster):
    lock = json.loads((ROOT/"deploy/cloud/eck-lock.json").read_text())
    cache = ROOT/"artifacts/eck-3.5.0"
    cache.mkdir(parents=True, exist_ok=True)
    for name, entry in lock.items():
        path = cache/name
        content = path.read_bytes() if path.exists() else urllib.request.urlopen(entry["url"], timeout=60).read()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("ECK manifest checksum mismatch: " + name)
        path.write_bytes(content)
    # Do not silently replace a different ECK installation or upgrade its CRDs.
    operator = cluster.get("statefulset", "elastic-operator", "elastic-system", optional=True)
    if operator and operator["spec"]["template"]["spec"]["containers"][0]["image"] != OPERATOR_IMAGE:
        raise ValueError("Existing ECK image differs; review the operator upgrade explicitly")
    for crd in yaml.safe_load_all((cache/"crds.yaml").read_text()):
        existing = cluster.get("crd", crd["metadata"]["name"], optional=True)
        if existing:
            if existing["spec"] != crd["spec"]:
                # Kubernetes may default CRD fields. Version labels also identify the installed release.
                expected = crd["metadata"].get("labels", {}).get("app.kubernetes.io/version")
                actual = existing["metadata"].get("labels", {}).get("app.kubernetes.io/version")
                if not expected or actual != expected:
                    raise ValueError("Existing ECK CRD differs from locked release")
        else:
            cluster.run("create", "-f", "-", data=json.dumps(crd))
    docs = list(yaml.safe_load_all((cache/"operator.yaml").read_text()))
    for doc in docs:
        if doc["kind"] == "StatefulSet":
            doc["spec"]["template"]["spec"]["containers"][0]["image"] = OPERATOR_IMAGE
        if doc["kind"] == "ConfigMap":
            settings = yaml.safe_load(doc["data"]["eck.yaml"])
            settings.update({"namespaces": ["elastic"], "disable-telemetry": True})
            doc["data"]["eck.yaml"] = yaml.safe_dump(settings, sort_keys=False)
    cluster.run("apply", "-f", "-", data=yaml.safe_dump_all(docs), timeout=180)
    cluster.run("-n", "elastic-system", "rollout", "status", "statefulset/elastic-operator",
                "--timeout=240s", timeout=270)
    manifests = list(yaml.safe_load_all((ROOT/"deploy/cloud/elasticsearch.yaml").read_text()))
    for doc in manifests:
        cluster.run("apply", "-f", "-", data=yaml.safe_dump(doc))
    until("Elasticsearch Ready", lambda: ready_pod(cluster))
    print("ECK and Elasticsearch are Ready", flush=True)


@contextmanager
def connection(cluster):
    """A temporary loopback tunnel with CA and hostname verification still enabled."""
    ca = cluster.secret("elasticsearch-es-http-certs-public")["tls.crt"]
    with tempfile.TemporaryDirectory(prefix="transport-es-ca-") as directory:
        ca_path = Path(directory)/"ca.crt"
        ca_path.write_text(ca)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(cluster.command + ["-n", "elastic", "port-forward",
            "service/elasticsearch-es-http", f"{port}:9200", "--address=127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        session = requests.Session()
        session.trust_env = False
        session.verify = str(ca_path)
        url = f"https://127.0.0.1:{port}"

        def connected():
            if process.poll() is not None:
                raise RuntimeError("Elasticsearch port-forward exited")
            try:
                return session.get(url, timeout=3).status_code == 401
            except requests.ConnectionError:
                return False

        try:
            until("verified TLS port-forward", connected, timeout=45)
            yield session, url, ca_path
        finally:
            session.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def call(session, url, method, path, auth, body=None, expected=(200, 201)):
    response = session.request(method, url+path, auth=auth, json=body, timeout=45)
    if response.status_code not in expected:
        raise RuntimeError(f"ES {method} {path} returned {response.status_code}")
    return response.json()


def provision(cluster):
    admin = ("elastic", cluster.secret("elasticsearch-es-elastic-user")["elastic"])
    ca = cluster.secret("elasticsearch-es-http-certs-public")["tls.crt"]
    with connection(cluster) as (session, url, ca_path):
        identities = {}
        for username, secret_name, privileges in (
            ("transport-app", "transport-secrets", APP_PRIVILEGES),
            ("transport-migration", "transport-migration-secrets", ["manage"]),
        ):
            role = username + "-v2"
            call(session, url, "PUT", "/_security/role/"+role, admin,
                 {"cluster": ["monitor"], "indices": [{"names": [PREFIX+"*"], "privileges": privileges}]})
            current = cluster.secret(secret_name, "default")
            if current.get("ES_USER") not in (None, username):
                raise ValueError("Existing ES identity differs: " + secret_name)
            password = current.get("ES_PASSWORD") or secrets.token_urlsafe(36)
            # Persist the generated credential before the HTTP call so retries reuse it.
            cluster.put_secret(secret_name, {"ES_USER": username, "ES_PASSWORD": password})
            call(session, url, "PUT", "/_security/user/"+username, admin,
                 {"password": password, "roles": [role], "enabled": True})
            identities[username] = (username, password)
        cluster.put_secret("transport-es-ca", {"ca.crt": ca})
        previous_prefix = os.environ.get("ES_INDEX_PREFIX")
        os.environ["ES_INDEX_PREFIX"] = PREFIX
        try:
            with Elasticsearch(url, basic_auth=identities["transport-migration"],
                               ca_certs=str(ca_path), request_timeout=45) as client:
                applied = migrate(client, replicas=0, shards=1)
        finally:
            if previous_prefix is None:
                os.environ.pop("ES_INDEX_PREFIX", None)
            else:
                os.environ["ES_INDEX_PREFIX"] = previous_prefix
    print(json.dumps({"schema_indices": len(applied), "index_replicas": 0,
                      "application_user": "transport-app", "migration_user": "transport-migration"}), flush=True)


def verify(cluster, restart=False):
    admin = ("elastic", cluster.secret("elasticsearch-es-elastic-user")["elastic"])
    credentials = cluster.secret("transport-secrets", "default")
    app = (credentials["ES_USER"], credentials["ES_PASSWORD"])
    migration = cluster.secret("transport-migration-secrets", "default")
    schema_auth = (migration["ES_USER"], migration["ES_PASSWORD"])
    before = until("Elasticsearch Ready before probe", lambda: ready_pod(cluster))
    claim = cluster.get("pvc", CLAIM)
    volume = cluster.get("pv", claim["spec"]["volumeName"])
    if claim["status"]["phase"] != "Bound" or volume["spec"]["persistentVolumeReclaimPolicy"] != "Retain":
        raise RuntimeError("Expected a Bound PVC with Retain volume policy")
    index = PREFIX + "cloud_storage_probe"
    doc_id = str(uuid.uuid4())
    document = {"probe_id": doc_id, "dataset_kind": "synthetic", "purpose": "cloud_persistence_acceptance"}
    with connection(cluster) as (session, url, _):
        info = call(session, url, "GET", "/", app)
        call(session, url, "GET", "/", (app[0], "invalid-probe-password"), expected=(401,))
        existing = session.head(url+"/"+index, auth=schema_auth, timeout=30)
        if existing.status_code == 404:
            call(session, url, "PUT", "/"+index, schema_auth,
                 {"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                  "mappings": {"dynamic": "strict", "properties": {
                      k: {"type": "keyword"} for k in document}}})
        elif existing.status_code != 200:
            raise RuntimeError("Probe index access failed")
        call(session, url, "PUT", f"/{index}/_doc/{doc_id}?refresh=wait_for", app, document)
        # Exercise the application's real bulk upsert path under restricted auth.
        response = session.post(url+"/_bulk?refresh=wait_for", auth=app, timeout=45,
            headers={"Content-Type": "application/x-ndjson"}, data=(
                json.dumps({"update": {"_index": index, "_id": doc_id}})+"\n"+
                json.dumps({"doc": document, "doc_as_upsert": True})+"\n"))
        if response.status_code != 200 or response.json().get("errors"):
            raise RuntimeError("Application bulk upsert failed under its restricted role")
        if call(session, url, "GET", f"/{index}/_doc/{doc_id}", app)["_source"] != document:
            raise RuntimeError("Probe readback did not match the written document")
        # These are live negative tests, not assumptions about role configuration.
        call(session, url, "PUT", f"/{PREFIX}forbidden_probe_{doc_id}", app, {}, expected=(403,))
        call(session, url, "PUT", f"/{index}/_mapping", app,
             {"properties": {"unwanted_field": {"type": "keyword"}}}, expected=(403,))
        call(session, url, "GET", "/_security/user", app, expected=(403,))
        call(session, url, "GET", "/outside_transport_prefix/_search", app, expected=(403,))
        call(session, url, "POST", f"/{index}/_flush", admin)
    restart_started = time.monotonic()
    after = before
    if restart:
        print("Restarting only the acceptance ES Pod; keeping PVC and PV", flush=True)
        cluster.run("delete", "pod", POD, "-n", "elastic", "--wait=true", "--timeout=120s", timeout=150)
        after = until("replacement ES Pod Ready", lambda: ready_pod(cluster, before["metadata"]["uid"]))
    with connection(cluster) as (session, url, _):
        if call(session, url, "GET", f"/{index}/_doc/{doc_id}", app)["_source"] != document:
            raise RuntimeError("Probe content changed or disappeared across verification")
        health = call(session, url, "GET", "/_cluster/health?wait_for_status=green&timeout=30s", app)
        if health["status"] != "green" or health.get("timed_out"):
            raise RuntimeError("Cluster did not reach green")
        # Index statistics need index-monitoring privileges that the app does not need.
        indices = call(session, url, "GET", "/_cat/indices/v2_*?format=json&h=index,health,docs.count,pri,rep", schema_auth)
        license_info = call(session, url, "GET", "/_license", admin)["license"]
    after_claim = cluster.get("pvc", CLAIM)
    if (after_claim["metadata"]["uid"] != claim["metadata"]["uid"] or
            after_claim["spec"]["volumeName"] != claim["spec"]["volumeName"]):
        raise RuntimeError("PVC identity or bound volume changed across verification")
    es = cluster.get("elasticsearch", "elasticsearch")
    service = cluster.get("service", "elasticsearch-es-http")
    result = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "context": cluster.context,
        "elasticsearch_version": info["version"]["number"], "license": license_info["type"],
        "cluster_health": health["status"], "data_nodes": health["number_of_data_nodes"],
        "service_type": service["spec"]["type"], "tls_ca_and_hostname_verified": True,
        "anonymous_and_wrong_password_rejected": True, "app_can_read_write": True,
        "app_can_bulk_upsert": True,
        "app_cannot_create_indices_manage_security_or_read_other_prefix": True,
        "app_cannot_explicitly_change_mappings": True,
        "index_prefix": PREFIX, "indices": indices,
        "storage": {"claim": CLAIM, "claim_uid": claim["metadata"]["uid"],
                    "phase": after_claim["status"]["phase"], "capacity": after_claim["status"]["capacity"],
                    "volume": claim["spec"]["volumeName"], "reclaim_policy": volume["spec"]["persistentVolumeReclaimPolicy"],
                    "volume_claim_delete_policy": es["spec"]["volumeClaimDeletePolicy"]},
        "persistence_probe": {"index": index, "document_id": doc_id, "restarted": restart,
                    "pod_uid_before": before["metadata"]["uid"], "pod_uid_after": after["metadata"]["uid"],
                    "node_before": before["spec"]["nodeName"], "node_after": after["spec"]["nodeName"],
                    "same_pvc_and_volume": True, "document_matches": True,
                    "restart_verification_seconds": round(time.monotonic()-restart_started, 2) if restart else None},
        "limits": ["Single ES node, zero index replicas; not highly available",
                   "No node-loss, cross-node reattachment, snapshot restore or load benchmark in this test",
                   "Historical source data has not been recovered or imported"]}
    path = ROOT/"artifacts/cloud-elasticsearch-verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8", newline="\n")
    print(json.dumps({"health": result["cluster_health"], "version": result["elasticsearch_version"],
                      "persistence_restart_test": restart, "evidence": str(path)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "provision", "verify"))
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default=CONTEXT)
    parser.add_argument("--restart-pod", action="store_true", help="Briefly interrupt ES to test persistence")
    args = parser.parse_args()
    if args.restart_pod and args.action != "verify":
        parser.error("--restart-pod is only valid with verify")
    cluster = Cluster(args.kubeconfig, args.context)
    if args.action == "install":
        install(cluster)
    elif args.action == "provision":
        provision(cluster)
    else:
        verify(cluster, restart=args.restart_pod)


if __name__ == "__main__":
    main()
