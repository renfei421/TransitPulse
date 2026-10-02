"""Budgeted Bluesky search backfill with durable per-page cursors.

Search visibility is platform controlled; this is not a promise of exhaustive
archive coverage. Empty pages, repeated cursors and capped windows are recorded.
"""
import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import time
from backend.common.http import get
from backend.common.es import get_client, bulk_documents, WriteCounts
from backend.ingestion.import_ndjson import normalize_record, atomic_json
from backend.common.time import utc_now
from backend.common.logging import get_logger
log = get_logger("backfill")
URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


def run(start, end, queries, *, checkpoint="data/checkpoints/bluesky-backfill.json",
        request_budget=30, pages_per_window=10, pause=1.0, es=None, fetch=None, sleep=time.sleep):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or (last-first).days > 3660 or request_budget < 1 or pages_per_window < 1 or pause < 0:
        raise ValueError("Invalid date range or request budget")
    queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))
    if not queries:
        raise ValueError("At least one search query is required")
    signature = hashlib.sha256(json.dumps([start, end, queries, pages_per_window]).encode()).hexdigest()
    checkpoint = Path(checkpoint)
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
        "signature": signature, "windows": {}, "dataset": "bluesky_search_backfill"}
    if state["signature"] != signature:
        raise ValueError("Use a new checkpoint when changing dates, queries or page cap")
    es = es or get_client()
    fetch = fetch or (lambda params: get(URL, params=params, timeout=30).json())
    counts = WriteCounts()
    requests_used = 0
    day = first
    while day <= last:
        for query in queries:
            key = day.isoformat() + "|" + query
            progress = state["windows"].setdefault(key, {"cursor": None, "pages": 0, "status": "pending", "seen_cursors": []})
            if progress["status"] in {"exhausted", "capped", "repeated_cursor"}:
                continue
            while requests_used < request_budget:
                params = {"q": query, "sort": "latest", "limit": 100,
                          "since": day.isoformat()+"T00:00:00Z",
                          "until": (day+timedelta(days=1)).isoformat()+"T00:00:00Z", "lang": "en"}
                if progress["cursor"]:
                    params["cursor"] = progress["cursor"]
                payload = fetch(params)
                requests_used += 1
                docs = []
                for record in payload.get("posts", []):
                    doc = normalize_record(record, source_dataset="bluesky_search_backfill", dataset_kind="live")
                    doc.update(fetched_at=utc_now(), source_query=query, collection_method="date_partitioned_search")
                    docs.append(doc)
                counts.add(bulk_documents("social_discussion_posts_raw", docs, client=es))
                progress["pages"] += 1
                next_cursor = payload.get("cursor")
                if not docs or not next_cursor:
                    progress["status"] = "exhausted"
                elif next_cursor in progress["seen_cursors"]:
                    progress["status"] = "repeated_cursor"
                elif progress["pages"] >= pages_per_window:
                    progress["status"] = "capped"
                else:
                    progress["seen_cursors"].append(next_cursor)
                    progress["cursor"] = next_cursor
                atomic_json(checkpoint, state)  # after every durable page
                if progress["status"] != "pending":
                    break
                if requests_used < request_budget:
                    sleep(pause)
            if requests_used >= request_budget:
                break
            sleep(pause)
        if requests_used >= request_budget:
            break
        day += timedelta(days=1)
    result = {"request_pages": requests_used, "writes": counts.as_dict(),
              "completed_windows": sum(w["status"] == "exhausted" for w in state["windows"].values()),
              "capped_windows": sum(w["status"] in {"capped", "repeated_cursor"} for w in state["windows"].values()),
              "planned_windows": ((last-first).days+1)*len(queries), "checkpoint": str(checkpoint)}
    log.info("backfill_completed", extra={"fields": result})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--query", action="append", required=True, dest="queries")
    parser.add_argument("--request-budget", type=int, default=30)
    parser.add_argument("--pages-per-window", type=int, default=10)
    parser.add_argument("--checkpoint", default="data/checkpoints/bluesky-backfill.json")
    print(json.dumps(run(**vars(parser.parse_args()))))


if __name__ == "__main__":
    main()
