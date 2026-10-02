"""Redis Streams work queue: at-least-once delivery, ES-before-ACK and poison DLQ.

There is one consumer group. Stream entries are deleted only after this group's
successful ACK; do not add another group without changing retention semantics.
"""
import argparse
import hashlib
import json
import os
import signal
import socket
import time
from uuid import uuid4
import redis
from elasticsearch.helpers import scan
from backend.common.es import get_client, bulk_documents
from backend.common.settings import secret, index_name
from backend.common.logging import get_logger
from backend.common.time import range_query, utc_now, resolve_range
from backend.data_process.sentiment import SentimentModel, PIPELINE_VERSION
from backend.data_process.cloud_sentiment.cloud_sentiment_pipeline import process_targets, batched

log = get_logger("queue")
# Atomic enqueue + dedup prevents a crash between SET NX and XADD losing a task.
ENQUEUE = """
if redis.call('EXISTS', KEYS[2]) == 1 then return false end
local id = redis.call('XADD', KEYS[1], '*', 'payload', ARGV[1])
redis.call('SET', KEYS[2], id, 'EX', ARGV[2])
return id
"""


class WorkQueue:
    def __init__(self, client=None, prefix=None, group="sentiment"):
        self.redis = client or redis.Redis.from_url(secret("REDIS_URL", "redis://127.0.0.1:16379/0"),
                                                    decode_responses=True, socket_timeout=10)
        self.prefix = prefix or os.getenv("QUEUE_PREFIX", "transport")
        self.stream = self.prefix + ":inference"
        self.dead = self.prefix + ":dead"
        self.group = group
        self.claim_cursor = "0-0"

    def ensure_group(self):
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, doc_ids, version=PIPELINE_VERSION, *, identity=None, ttl=86400):
        if not doc_ids or len(doc_ids) > 100:
            raise ValueError("A queue task requires 1..100 document IDs")
        payload = {"doc_ids": sorted(set(doc_ids)), "version": version, "queued_at": utc_now()}
        stable = identity or json.dumps({"ids": payload["doc_ids"], "version": version}, sort_keys=True)
        key = self.prefix + ":dedup:" + hashlib.sha256(stable.encode()).hexdigest()
        payload["dedup_key"] = key
        return self.redis.eval(ENQUEUE, 2, self.stream, key, json.dumps(payload), ttl)

    def read(self, consumer, *, idle_ms=300000, block_ms=1000):
        # Reclaim abandoned work first. Cursor continuation prevents older pending
        # messages from starving when the pending list exceeds one scan page.
        claimed = self.redis.xautoclaim(self.stream, self.group, consumer, idle_ms,
                                       start_id=self.claim_cursor, count=1)
        self.claim_cursor = claimed[0]
        if claimed[1]:
            return claimed[1][0]
        rows = self.redis.xreadgroup(self.group, consumer, {self.stream: ">"}, count=1, block=block_ms)
        return rows[0][1][0] if rows else None

    def acknowledge(self, message_id):
        # The durable sink must have succeeded before this transaction executes.
        with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(self.stream, self.group, message_id)
            pipe.xdel(self.stream, message_id)
            pipe.hdel(self.prefix + ":attempts", message_id)
            pipe.execute()

    def failed(self, message_id, fields, error_type, max_attempts=3):
        count = self.redis.hincrby(self.prefix + ":attempts", message_id, 1)
        if count >= max_attempts:
            with self.redis.pipeline(transaction=True) as pipe:
                pipe.xadd(self.dead, {"original_id": message_id, "payload": fields.get("payload", "{}"),
                    "error_type": error_type, "attempts": count, "failed_at": utc_now()})
                pipe.xack(self.stream, self.group, message_id)
                pipe.xdel(self.stream, message_id)
                pipe.hdel(self.prefix + ":attempts", message_id)
                pipe.execute()
        return count

    def stats(self):
        groups = self.redis.xinfo_groups(self.stream)
        group = next(item for item in groups if item["name"] == self.group)
        return {"undelivered": group.get("lag"), "pending": group["pending"],
                "dead_letters": self.redis.xlen(self.dead), "stream_entries": self.redis.xlen(self.stream)}

    def replay_dead(self, limit=100):
        count = 0
        for message_id, fields in self.redis.xrange(self.dead, count=limit):
            payload = json.loads(fields["payload"])
            if not payload.get("doc_ids"):
                continue
            # XADD and deletion from DLQ are one Redis transaction.
            payload["queued_at"] = utc_now()
            with self.redis.pipeline(transaction=True) as pipe:
                pipe.xadd(self.stream, {"payload": json.dumps(payload)})
                pipe.xdel(self.dead, message_id)
                pipe.execute()
            count += 1
        return count


def publish(es, queue, *, start=None, end=None, batch_size=32):
    queue.ensure_group()
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be 1..100")
    hits = scan(es, index=index_name("social_discussion_posts_raw"),
                query={"query": range_query("fetched_at", start, end)}, size=500)
    published = skipped = 0
    for batch in batched(hits, batch_size):
        # Include source version in dedup identity so later edits are processable.
        identity = json.dumps(sorted((h["_id"], h["_source"].get("fetched_at"),
                                      h["_source"].get("raw_text")) for h in batch), ensure_ascii=False)
        if queue.enqueue([h["_id"] for h in batch], identity=PIPELINE_VERSION + identity):
            published += 1
        else:
            skipped += 1
    return {"tasks_published": published, "tasks_deduplicated": skipped, **queue.stats()}


def process_message(es, model, fields):
    payload = json.loads(fields["payload"])
    if payload.get("version") != PIPELINE_VERSION:
        raise ValueError("Unsupported pipeline version; re-publish raw records for the new version")
    ids = payload.get("doc_ids", [])
    if not ids or len(ids) > 100:
        raise ValueError("Invalid document ID batch")
    response = es.mget(index=index_name("social_discussion_posts_raw"), ids=ids)
    if any(not doc.get("found") for doc in response["docs"]):
        raise ValueError("Task references missing raw records")
    docs = [{**doc["_source"], "doc_id": doc["_id"]} for doc in response["docs"]]
    output = process_targets(docs, es, model)
    return bulk_documents("social_posts_processed", output, client=es).as_dict()


def run_worker(queue, es, model, *, max_messages=None, idle_ms=300000):
    queue.ensure_group()
    consumer = socket.gethostname() + "-" + uuid4().hex[:8]
    stop = False
    def shutdown(*_):
        nonlocal stop
        stop = True
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), shutdown)
    completed = failed = 0
    while not stop and (max_messages is None or completed+failed < max_messages):
        message = queue.read(consumer, idle_ms=idle_ms)
        if not message:
            if max_messages is not None:
                break
            continue
        identifier, fields = message
        started = time.monotonic()
        try:
            writes = process_message(es, model, fields)
            queue.acknowledge(identifier)
            completed += 1
            log.info("task_completed", extra={"fields": {"id": identifier, "writes": writes,
                      "duration_seconds": round(time.monotonic()-started, 3)}})
        except Exception as exc:
            attempts = queue.failed(identifier, fields, type(exc).__name__)
            failed += 1
            log.exception("task_failed", extra={"fields": {"id": identifier, "attempts": attempts}})
    return {"completed": completed, "failed": failed, **queue.stats()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish", "worker", "stats", "replay-dead", "init"])
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--window-hours", type=float, default=48)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--idle-ms", type=int, default=300000)
    parser.add_argument("--vader", action="store_true")
    args = parser.parse_args()
    queue = WorkQueue()
    queue.ensure_group()
    if args.command == "publish":
        start, end = resolve_range(args.start, args.end, args.window_hours if not args.start else None)
        result = publish(get_client(), queue, start=start, end=end, batch_size=args.batch_size)
    elif args.command == "worker":
        result = run_worker(queue, get_client(), SentimentModel(use_transformers=not args.vader),
                            max_messages=args.max_messages, idle_ms=args.idle_ms)
    elif args.command == "replay-dead":
        result = {"replayed": queue.replay_dead()}
    else:
        result = queue.stats()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
