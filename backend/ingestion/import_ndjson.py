"""Bounded-memory, resumable import of normalized, Bluesky or Mastodon NDJSON(.gz).

Checkpoints advance only after all bulk items succeed. A crash between the bulk
write and checkpoint may replay a batch; stable document IDs make this safe.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from backend.common.es import get_client, bulk_documents, WriteCounts
from backend.common.logging import get_logger
from backend.common.time import timestamp, utc_now
from backend.parallel_harvester.normalize import (
    normalise_bluesky_post, normalise_mastodon_status, make_doc_id,
)
from backend.parallel_harvester.topics import match_topics
log = get_logger("import")


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Independent collectors may publish snapshots concurrently.
    temporary = path.with_suffix(path.suffix + "." + uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024*1024):
            digest.update(block)
    return digest.hexdigest()


def normalize_record(record, *, source_dataset, dataset_kind="imported", server_domain=None):
    record = record.get("_source", record.get("doc", record))
    if "post_id" in record and "raw_text" in record:
        doc = dict(record)
    elif "uri" in record and isinstance(record.get("record"), dict):
        doc = normalise_bluesky_post(record, collection_method="offline_import")
    elif "content" in record and "account" in record:
        if not server_domain:
            from urllib.parse import urlsplit
            server_domain = urlsplit(record.get("url") or record.get("uri") or "").netloc
        if not server_domain:
            raise ValueError("Mastodon import requires server_domain")
        doc = normalise_mastodon_status(record, server_domain=server_domain, collection_method="offline_import")
        # Local numerical status IDs from different servers must never collide.
        doc["post_id"] = f"https://{server_domain}/statuses/{record['id']}"
        parent = record.get("in_reply_to_id")
        doc["parent_post_id"] = f"https://{server_domain}/statuses/{parent}" if parent else None
        doc["thread_root_id"] = doc["post_id"] if not parent else None
        doc["doc_id"] = make_doc_id("mastodon", doc["post_id"])
    else:
        raise ValueError("Unsupported schema; use the documented normalized adapter")
    if not doc.get("post_id") or not doc.get("platform") or not str(doc.get("raw_text") or "").strip():
        raise ValueError("Record requires platform, post_id and non-empty raw_text")
    if len(doc["raw_text"]) > 100000:
        raise ValueError("Post text exceeds 100000 characters")
    doc["created_at"] = timestamp(doc["created_at"]).isoformat()
    doc["doc_id"] = doc.get("doc_id") or make_doc_id(doc["platform"], doc["post_id"])
    topics, keywords = match_topics(doc["raw_text"])
    doc.setdefault("candidate_topics", topics)
    doc.setdefault("direct_candidate_topics", topics)
    doc.setdefault("matched_keywords", keywords)
    doc.setdefault("thread_root_id", doc["post_id"] if not doc.get("parent_post_id") else None)
    doc.update(dataset_kind=dataset_kind, source_dataset=source_dataset, schema_version=2,
               source_record_id=doc["post_id"])
    return doc


def run(path, *, source_dataset, checkpoint=None, batch_size=500, max_records=None,
        topics_only=False, dataset_kind="imported", server_domain=None, es=None):
    if batch_size < 1 or batch_size > 5000 or (max_records is not None and max_records < 1):
        raise ValueError("Invalid batch/record budget")
    if dataset_kind not in {"imported", "synthetic", "live"}:
        raise ValueError("Invalid dataset_kind")
    path = Path(path).resolve()
    checkpoint = Path(checkpoint or f"data/checkpoints/{path.name}.json")
    digest = fingerprint(path)
    settings = {"fingerprint": digest, "source_dataset": source_dataset, "dataset_kind": dataset_kind,
                "topics_only": topics_only, "server_domain": server_domain}
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
        **settings, "line": 0, "accepted": 0, "rejected": 0, "filtered": 0, "imported_at": utc_now()}
    if any(state.get(key) != value for key, value in settings.items()):
        raise ValueError("Checkpoint belongs to a different file or import configuration; use a new checkpoint")
    es = es or get_client()
    counts, batch = WriteCounts(), []
    accepted_this_run = 0
    started = time.monotonic()
    quarantine = checkpoint.with_suffix(".rejected.ndjson")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)

    def flush(line_number):
        if batch:
            counts.add(bulk_documents("social_discussion_posts_raw", batch, client=es))
            batch.clear()
        state.update(line=line_number, updated_at=utc_now())
        atomic_json(checkpoint, state)

    opener = gzip.open if path.suffix == ".gz" else open
    last_line = state["line"]
    with opener(path, "rt", encoding="utf-8-sig") as stream, quarantine.open("a", encoding="utf-8") as rejects:
        for line_number, line in enumerate(stream, 1):
            if line_number <= state["line"]:
                continue
            last_line = line_number
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Record must be an object")
                doc = normalize_record(record, source_dataset=source_dataset,
                                       dataset_kind=dataset_kind, server_domain=server_domain)
                if topics_only and not doc["candidate_topics"]:
                    state["filtered"] += 1
                else:
                    # Deterministic fetched_at across retries gives true noops.
                    doc["fetched_at"] = state["imported_at"]
                    batch.append(doc)
                    state["accepted"] += 1
                    accepted_this_run += 1
            except (ValueError, KeyError, TypeError) as exc:
                state["rejected"] += 1
                rejects.write(json.dumps({"line": line_number, "error_type": type(exc).__name__,
                    "record_sha256": hashlib.sha256(line.encode()).hexdigest()}) + "\n")
            if line_number % batch_size == 0:
                flush(line_number)
            if max_records and accepted_this_run >= max_records:
                break
        flush(last_line)
    result = {**state, "writes_this_run": counts.as_dict(), "accepted_this_run": accepted_this_run,
              "duration_seconds": round(time.monotonic()-started, 3), "checkpoint": str(checkpoint)}
    log.info("import_completed", extra={"fields": result})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--source-dataset", required=True)
    parser.add_argument("--checkpoint")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--topics-only", action="store_true")
    parser.add_argument("--dataset-kind", choices=["imported", "synthetic"], default="imported")
    parser.add_argument("--server-domain")
    args = vars(parser.parse_args())
    print(json.dumps(run(**args), allow_nan=False))


if __name__ == "__main__":
    main()
