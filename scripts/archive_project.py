"""Full logical archive, local-only restore and document-level verification.

An archive is private: it contains original posts and cached provider responses.
Cloud export freezes only v2_* application indices after checking for active Jobs.
Restore never replaces an existing index and never calls an inference provider.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan, streaming_bulk

from scripts.cloud_elasticsearch import Cluster, CONTEXT, connection


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def document(hit):
    result = {"_id": hit["_id"], "_source": hit["_source"]}
    if "_routing" in hit:
        result["_routing"] = hit["_routing"]
    return result


def fingerprint(rows):
    """Order-independent, duplicate-sensitive digest of IDs and full source values."""
    digests = [hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False,
               separators=(",", ":"), allow_nan=False).encode()).digest() for row in rows]
    return {"count": len(digests), "documents_sha256": hashlib.sha256(b"".join(sorted(digests))).hexdigest()}


def archive_rows(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def portable_settings(settings):
    # Copy meaningful user settings, never UUIDs, creation/version, write blocks or cloud allocation rules.
    allowed = {"number_of_shards", "number_of_routing_shards", "routing_partition_size", "analysis",
               "similarity", "mapping", "sort", "max_result_window", "max_inner_result_window"}
    result = {k: v for k, v in settings["index"].items() if k in allowed}
    return {**result, "number_of_replicas": 0, "refresh_interval": "-1"}


def local_client(url):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.path not in ("", "/"):
        raise ValueError("Restore/verification requires an explicit loopback HTTP Elasticsearch endpoint")
    return Elasticsearch(url, request_timeout=120)


def export_cloud(directory, kubeconfig):
    directory.mkdir(parents=True, exist_ok=False)
    cluster = Cluster(kubeconfig, CONTEXT)
    jobs = json.loads(cluster.run("get", "jobs", "-A", "-o", "json"))["items"]
    crons = json.loads(cluster.run("get", "cronjobs", "-A", "-o", "json"))["items"]
    if any(j.get("status", {}).get("active", 0) for j in jobs) or any(not j["spec"].get("suspend") for j in crons):
        raise ValueError("Stop active Jobs and suspend CronJobs before archiving")
    inventory = {}
    for kind in ("nodes", "pv", "pvc", "services", "pods", "jobs"):
        data = json.loads(cluster.run("get", kind, "-A", "-o", "json"))["items"]
        inventory[kind] = [{"name": x["metadata"]["name"], "namespace": x["metadata"].get("namespace"),
                            "spec": x["spec"] if kind in ("nodes", "pv", "pvc", "services") else None,
                            "status": x.get("status")} for x in data]
    write_json(directory / "cloud-inventory.json", inventory)
    admin = ("elastic", cluster.secret("elasticsearch-es-elastic-user")["elastic"])
    with connection(cluster) as (_, url, ca):
        with Elasticsearch(url, basic_auth=admin, ca_certs=str(ca), request_timeout=120) as es:
            metadata = dict(es.indices.get(index="v2_*"))
            if not metadata or any(not name.startswith("v2_") for name in metadata):
                raise ValueError("Unexpected application index selection")
            # Block writers before opening scan snapshots. Read APIs remain available.
            es.indices.put_settings(index=",".join(metadata), settings={"blocks.write": True})
            es.indices.refresh(index=",".join(metadata))
            write_json(directory / "index-metadata.json", metadata)
            manifest = {"format": "transport-full-archive-v1", "created_at": datetime.now(timezone.utc).isoformat(),
                        "es_version": es.info()["version"]["number"], "context": CONTEXT,
                        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                        "indices": {}, "complete": False}
            write_json(directory / "manifest.json", manifest)
            for name in sorted(metadata):
                path = directory / (name + ".ndjson.gz")
                expected = es.count(index=name)["count"]
                def rows():
                    with gzip.open(path, "wt", encoding="utf-8", compresslevel=3) as stream:
                        for hit in scan(es, index=name, size=250, scroll="10m"):
                            row = document(hit)
                            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                            yield row
                entry = fingerprint(rows())
                if entry["count"] != expected or es.count(index=name)["count"] != expected:
                    raise ValueError("Archive count mismatch: " + name)
                entry.update(file=path.name, file_sha256=file_hash(path), bytes=path.stat().st_size)
                manifest["indices"][name] = entry
                write_json(directory / "manifest.json", manifest)
                print(json.dumps({"archived": name, **entry}), flush=True)
            manifest["metadata_sha256"] = file_hash(directory / "index-metadata.json")
            manifest["complete"] = True
            write_json(directory / "manifest.json", manifest)


def validate_archive(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("complete") or manifest.get("format") != "transport-full-archive-v1":
        raise ValueError("Incomplete or unsupported archive")
    if file_hash(directory / "index-metadata.json") != manifest["metadata_sha256"]:
        raise ValueError("Index metadata checksum mismatch")
    for name, entry in manifest["indices"].items():
        if not name.startswith("v2_") or entry["file"] != name + ".ndjson.gz":
            raise ValueError("Unexpected index or archive filename")
        if file_hash(directory / entry["file"]) != entry["file_sha256"]:
            raise ValueError("Archive checksum mismatch: " + name)
    return manifest


def restore(directory, url):
    manifest = validate_archive(directory)
    metadata = json.loads((directory / "index-metadata.json").read_text(encoding="utf-8"))
    with local_client(url) as es:
        if es.info()["version"]["number"] != manifest["es_version"]:
            raise ValueError("Restore rehearsal must use the archived Elasticsearch version")
        # Fail before any mutation, even when just one target name already exists.
        if any(es.indices.exists(index=name) for name in manifest["indices"]):
            raise ValueError("Target already contains archive indices; use a clean dedicated ES instance")
        for name, entry in manifest["indices"].items():
            spec = metadata[name]
            es.indices.create(index=name, mappings=spec["mappings"], settings=portable_settings(spec["settings"]))
            actions = ({"_index": name, "_op_type": "create", **row} for row in archive_rows(directory / entry["file"]))
            count = 0
            for ok, _ in streaming_bulk(es, actions, chunk_size=250, max_chunk_bytes=10*1024*1024,
                                        raise_on_error=True, raise_on_exception=True):
                if not ok:
                    raise RuntimeError("Restore bulk item failed")
                count += 1
            if count != entry["count"]:
                raise ValueError("Restore count mismatch")
            es.indices.put_settings(index=name, settings={"refresh_interval": "1s"})
            es.indices.refresh(index=name)
            for alias, settings in spec.get("aliases", {}).items():
                es.indices.put_alias(index=name, name=alias, **settings)
            print(json.dumps({"restored": name, "count": count}), flush=True)
    verify(directory, url)


def verify(directory, url):
    manifest = validate_archive(directory)
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "es_url": url, "indices": {}, "passed": False}
    with local_client(url) as es:
        for name, entry in manifest["indices"].items():
            actual = fingerprint(document(hit) for hit in scan(es, index=name, size=500, scroll="10m"))
            if any(actual[k] != entry[k] for k in ("count", "documents_sha256")):
                raise ValueError("Restored documents differ: " + name)
            report["indices"][name] = actual
            print(json.dumps({"verified": name, "count": actual["count"]}), flush=True)
    report["passed"] = True
    write_json(directory / "restore-verification.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "restore", "verify"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--url", default="http://127.0.0.1:19222")
    args = parser.parse_args()
    if args.action == "export":
        if not args.kubeconfig:
            parser.error("export requires --kubeconfig")
        export_cloud(args.directory, args.kubeconfig)
    else:
        {"restore": restore, "verify": verify}[args.action](args.directory, args.url)


if __name__ == "__main__":
    main()
