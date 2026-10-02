"""Fission harvest entrypoints with request-local windows and structured outcomes."""
from datetime import datetime, timedelta, timezone
import uuid
from flask import request

from backend.common.logging import get_logger, run_id
from backend.parallel_harvester import config
from backend.parallel_harvester.es_client import bulk_index, get_index_fields
from backend.parallel_harvester.sources import (
    harvest_bluesky_discussions, harvest_mastodon_discussions, study_window,
    parse_utc_datetime,
)
from backend.parallel_harvester.topics import generate_queries

log = get_logger("harvester")


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def make_run_doc(platform, started_at, ended_at, status, result=None, error_message=None):
    result = result or {}
    identifier = f"{platform}_{started_at}_{uuid.uuid4().hex[:8]}"
    return {
        "doc_id":identifier, "run_id":identifier, "platform":platform, "status":status,
        "started_at":started_at, "ended_at":ended_at,
        "query_count":result.get("query_count", 0), "queries_failed":result.get("queries_failed", 0),
        "seed_records_fetched":result.get("seed_fetched", 0),
        "seed_records_inserted":result.get("seed_inserted", 0),
        "reply_nodes_inserted":result.get("reply_nodes_inserted", 0),
        "reply_edges_inserted":result.get("reply_edges_inserted", 0),
        "writes":result.get("writes", {}), "error_message":error_message,
        "window_start":result.get("window_start"), "window_end":result.get("window_end"),
    }


def _parse_request():
    now = datetime.now(timezone.utc)
    end = request.args.get("end", now.isoformat())
    full_run = request.args.get("full_run", "false").lower() == "true"
    start = request.args.get("start", config.STUDY_START if full_run else (now-timedelta(hours=48)).isoformat())
    a, b = parse_utc_datetime(start), parse_utc_datetime(end)
    if not a or not b or a >= b:
        raise ValueError("start/end must be ISO timestamps with start before end")
    limit = int(request.args.get("query_limit", str(config.DEFAULT_QUERY_LIMIT)))
    if not 1 <= limit <= 1000:
        raise ValueError("query_limit must be between 1 and 1000")
    queries = generate_queries()
    offset = int(request.args.get("query_offset", str((int(now.timestamp())//21600*limit) % len(queries))))
    offset %= len(queries)
    queries = queries[offset:] + queries[:offset]
    return queries[:limit], start, end


def _run(platform, harvest_fn, queries, start, end):
    started = utc_now_iso()
    window_token = study_window.set((start, end))
    log_token = run_id.set(uuid.uuid4().hex)
    try:
        fields = get_index_fields(config.POSTS_INDEX)
        edge_fields = get_index_fields(config.EDGES_INDEX)
        result = harvest_fn(queries, fields, edge_fields, max_seeds=config.MAX_SEEDS_PER_RUN)
        result.update(query_count=len(queries), window_start=start, window_end=end)
        failures = result.get("queries_failed", 0)
        status = "failed" if failures == len(queries) else "partial_success" if failures else "success"
        doc = make_run_doc(platform, started, utc_now_iso(), status, result)
        bulk_index(config.RUNS_INDEX, [doc], "doc_id")
        log.info("harvest_complete", extra={"fields":doc})
        return doc, 503 if status == "failed" else 200
    except Exception:
        log.exception("harvest_failed", extra={"fields":{"platform":platform}})
        doc = make_run_doc(platform, started, utc_now_iso(), "failed", error_message="harvest_failed")
        try:
            bulk_index(config.RUNS_INDEX, [doc], "doc_id")
        except Exception:
            log.exception("run_audit_write_failed")
        return {"status":"error", "message":"Harvester failed; inspect structured logs", "run_id":run_id.get()}, 503
    finally:
        study_window.reset(window_token)
        run_id.reset(log_token)


def _handle(platform, collector):
    try:
        queries, start, end = _parse_request()
    except (TypeError, ValueError):
        return {"status":"error", "message":"Invalid collection window or query_limit"}, 400
    return _run(platform, collector, queries, start, end)


def main_bluesky():
    return _handle("bluesky", harvest_bluesky_discussions)


def main_mastodon():
    return _handle("mastodon", harvest_mastodon_discussions)
