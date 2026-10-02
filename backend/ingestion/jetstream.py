"""Bounded live capture for the documented Jetstream LEGACY v1 protocol.

This deliberately uses time_us microsecond cursors, not the incompatible v2
sequence cursor. Restart replays two seconds; stable IDs absorb duplicate events.
"""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed
from elasticsearch import NotFoundError
from backend.common.es import get_client, bulk_documents, WriteCounts
from backend.common.settings import index_name
from backend.common.logging import get_logger
from backend.ingestion.import_ndjson import atomic_json, normalize_record
from backend.parallel_harvester.normalize import make_doc_id

HOSTS = ("jetstream1.us-east.bsky.network", "jetstream2.us-east.bsky.network",
         "jetstream1.us-west.bsky.network", "jetstream2.us-west.bsky.network")
log = get_logger("jetstream")


def parse_event(event):
    if event.get("kind") != "commit":
        return None, None
    commit = event.get("commit") or {}
    if commit.get("collection") != "app.bsky.feed.post":
        return None, None
    uri = f"at://{event['did']}/app.bsky.feed.post/{commit['rkey']}"
    if commit.get("operation") == "delete":
        return "delete", uri
    if commit.get("operation") not in {"create", "update"}:
        return None, None
    record = commit.get("record") or {}
    if not record.get("text"):
        return None, None
    doc = normalize_record({"uri": uri, "record": record, "author": {"did": event["did"]}},
                           source_dataset="jetstream_legacy_v1", dataset_kind="live")
    return commit["operation"], doc


def delete_post(es, uri):
    identifier = make_doc_id("bluesky", uri)
    deleted = 0
    for index in ("social_discussion_posts_raw", "social_posts_processed"):
        try:
            es.delete(index=index_name(index), id=identifier)
            deleted += 1
        except NotFoundError:
            pass
    return deleted


def run(*, seconds=60, max_events=10000, max_mb=20, batch_size=100,
        checkpoint="data/checkpoints/jetstream-v1.json", host=HOSTS[0], australia_only=False, es=None):
    if host not in HOSTS or seconds <= 0 or max_events < 1 or max_mb <= 0 or batch_size < 1:
        raise ValueError("Invalid host or capture budget")
    es = es or get_client()
    checkpoint = Path(checkpoint)
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    if state and (state.get("protocol") != "legacy-v1" or state.get("australia_only") != australia_only):
        raise ValueError("Cursor belongs to a different protocol or cohort")
    cursor = state.get("cursor")
    began = time.monotonic()
    seen = accepted = filtered = deleted = byte_count = reconnects = 0
    counts, batch = WriteCounts(), []
    def flush():
        if batch:
            counts.add(bulk_documents("social_discussion_posts_raw", batch, client=es))
            batch.clear()
        if cursor is not None:
            atomic_json(checkpoint, {"protocol": "legacy-v1", "cursor": cursor,
                                    "australia_only": australia_only, "host": host})
    while time.monotonic()-began < seconds and seen < max_events and byte_count < max_mb*1024*1024:
        params = {"wantedCollections": "app.bsky.feed.post"}
        if cursor is not None:
            params["cursor"] = max(0, cursor-2_000_000)
        try:
            with connect(f"wss://{host}/subscribe?{urlencode(params)}", open_timeout=15,
                         max_size=1024*1024, ping_interval=20, close_timeout=3) as socket:
                while seen < max_events and byte_count < max_mb*1024*1024:
                    remaining = seconds-(time.monotonic()-began)
                    if remaining <= 0:
                        break
                    try:
                        message = socket.recv(timeout=min(remaining, 10))
                    except TimeoutError:
                        continue
                    byte_count += len(message.encode() if isinstance(message, str) else message)
                    event = json.loads(message)
                    if "time_us" not in event:
                        raise ValueError("Unexpected protocol: legacy-v1 requires time_us")
                    seen += 1
                    operation, document = parse_event(event)
                    if operation == "delete":
                        # Flush first so a prior create in this batch cannot resurrect it.
                        flush()
                        deleted += delete_post(es, document)
                    elif document is not None:
                        language = document.get("lang")
                        relevant = bool(document["candidate_topics"]) and language in {None, "en"}
                        relevant = relevant and (not australia_only or bool(document.get("inferred_region")))
                        if relevant:
                            from backend.common.time import utc_now
                            document["fetched_at"] = utc_now()
                            batch.append(document)
                            accepted += 1
                        else:
                            filtered += 1
                            if operation == "update":
                                flush()
                                deleted += delete_post(es, document["post_id"])
                    cursor = max(cursor or 0, int(event["time_us"]))
                    if seen % batch_size == 0:
                        flush()
            break
        except (OSError, TimeoutError, ConnectionClosed) as exc:
            # Sink failures are not swallowed by this transport-only retry.
            reconnects += 1
            log.warning("stream_reconnect", extra={"fields": {"attempt": reconnects, "type": type(exc).__name__}})
            if reconnects >= 3:
                raise
            time.sleep(min(2**reconnects, max(0, seconds-(time.monotonic()-began))))
    flush()
    result = {"protocol": "legacy-v1", "events_seen": seen, "accepted": accepted,
              "filtered": filtered, "deletions_applied": deleted, "bytes_received": byte_count,
              "reconnects": reconnects, "duration_seconds": round(time.monotonic()-began, 3),
              "writes": counts.as_dict(), "cohort": "Australia keyword inferred" if australia_only else "global English transport keywords",
              "cursor": cursor, "coverage": "Bounded live sample, not an historical archive or representative survey"}
    log.info("capture_completed", extra={"fields": result})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--max-events", type=int, default=10000)
    parser.add_argument("--max-mb", type=float, default=20)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--checkpoint", default="data/checkpoints/jetstream-v1.json")
    parser.add_argument("--host", choices=HOSTS, default=HOSTS[0])
    parser.add_argument("--australia-only", action="store_true")
    print(json.dumps(run(**vars(parser.parse_args()))))


if __name__ == "__main__":
    main()
