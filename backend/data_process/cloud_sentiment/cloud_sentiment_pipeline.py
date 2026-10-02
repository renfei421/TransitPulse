"""Bounded-memory social processing shared by scheduled Jobs and queue workers."""
import argparse
from collections import Counter
from itertools import islice
import json
import os
import time
import uuid

from elasticsearch.helpers import scan
from backend.common.es import bulk_documents, get_client, WriteCounts, BulkWriteError
from backend.common.settings import index_name
from backend.common.logging import get_logger, run_id
from backend.common.time import range_query, resolve_range, utc_now
from backend.data_process.sentiment import MODEL_NAME, SentimentModel
from backend.data_process.cloud_sentiment.context_thread_sentiment import node_from_ndjson_doc
from backend.data_process.threads import build_threads_from_posts_and_edges
from backend.data_process.sentiment import annotate_thread
from backend.data_process.eligibility import POLICY_VERSION, LanguageDetector, assess, model_text

log = get_logger("social_processing")
POSTS = "social_discussion_posts_raw"
EDGES = "social_discussion_edges_raw"
OUTPUT = "social_posts_processed"


def batched(items, batch_size):
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    iterator = iter(items)
    while batch := list(islice(iterator, batch_size)):
        yield batch


def fetch_post_nodes_by_ids(es, logical_index, post_ids, terms_batch_size=500):
    nodes = {}
    for group in batched(sorted(post_ids), terms_batch_size):
        for hit in scan(es, index=index_name(logical_index),
                        query={"query":{"terms":{"post_id":group}}}, size=terms_batch_size):
            node = node_from_ndjson_doc(hit["_source"])
            node["doc_id"] = node.get("doc_id") or hit["_id"]
            nodes[node["post_id"]] = node
    return nodes


def fetch_parent_context(es, logical_index, known_nodes, max_depth=5, terms_batch_size=500):
    nodes = dict(known_nodes)
    frontier = {n["parent_post_id"] for n in nodes.values() if n.get("parent_post_id")} - nodes.keys()
    for _ in range(max_depth):
        if not frontier:
            break
        found = fetch_post_nodes_by_ids(es,logical_index,frontier,terms_batch_size)
        new = {k:v for k,v in found.items() if k not in nodes}
        nodes.update(new)
        frontier = {n["parent_post_id"] for n in new.values() if n.get("parent_post_id")} - nodes.keys()
    return nodes


def fetch_edges(es, post_ids, logical_index=EDGES, batch_size=500):
    edges = []
    # Never send an unbounded terms query or materialize all historical edges.
    for group in batched(sorted(post_ids),batch_size):
        edges.extend(hit["_source"] for hit in scan(
            es,index=index_name(logical_index),
            query={"query":{"terms":{"descendant_post_id":group}}}, size=batch_size))
    return edges


def process_targets(target_docs, es, model, *, max_depth=5, use_context=True, posts_index=POSTS, edges_index=EDGES):
    nodes = {doc["post_id"]:node_from_ndjson_doc(doc) for doc in target_docs}
    target_ids = set(nodes)
    nodes = fetch_parent_context(es, posts_index, nodes, max_depth) if use_context else nodes
    edges = fetch_edges(es,set(nodes),edges_index) if use_context else []
    seeds = {e.get("seed_post_id") for e in edges if e.get("seed_post_id")} - nodes.keys()
    if seeds:
        nodes.update(fetch_post_nodes_by_ids(es,posts_index,seeds))
    rows = []
    # Fill inference batches across small threads; annotation reuses this bounded cache.
    model.classify_many([node.get("raw_text", "") for node in nodes.values()])
    for thread in build_threads_from_posts_and_edges(nodes,edges):
        _, results = annotate_thread(thread,model,use_context=use_context)
        for row in results:
            if row["post_id"] in target_ids:
                row.update(processed_at=utc_now(), processing_run_id=run_id.get(),
                           thread_seed_post_id=thread.get("post_id"))
                rows.append(row)
    if {r["post_id"] for r in rows} != target_ids:
        raise ValueError("Thread reconstruction lost target posts")
    return rows


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--posts-index",default=POSTS)
    parser.add_argument("--edges-index",default=EDGES)
    parser.add_argument("--processed-index",default=OUTPUT)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--window-hours",type=float)
    parser.add_argument("--time-field",choices=["created_at","fetched_at"],default="fetched_at")
    parser.add_argument("--scan-size",type=int,default=200)
    parser.add_argument("--thread-batch-size",type=int,default=100)
    parser.add_argument("--context-max-depth",type=int,default=5)
    parser.add_argument("--model",default=MODEL_NAME)
    parser.add_argument("--no-transformers",action="store_true",help="Explicit VADER baseline; never a silent fallback")
    parser.add_argument("--local-only",action="store_true")
    parser.add_argument("--dry-run",action="store_true")
    parser.add_argument("--eligibility-policy", choices=("all", POLICY_VERSION), default="all")
    parser.add_argument("--dataset-kind", choices=("observed", "live", "imported", "synthetic", "all"), default="observed")
    parser.add_argument("--source-dataset", action="append", default=[])
    parser.add_argument("--max-documents", type=int, default=5000)
    parser.add_argument("--inference-batch-size", type=int, default=8)
    parser.add_argument("--truncation-side", choices=("left", "right"), default="left")
    return parser.parse_args(argv)


def run_pipeline(args, *, es=None, model=None, language_detector=None):
    es = es or get_client()
    start,end = resolve_range(args.start_date,args.end_date,args.window_hours)
    if not 1 <= args.max_documents <= 100000 or args.thread_batch_size < 1 or args.scan_size < 1:
        raise ValueError("Invalid processing budget or batch size")
    if args.eligibility_policy != "all" and not args.local_only:
        raise ValueError("English routing currently requires local-only inference; do not import unvetted parent context")
    clauses = [range_query(args.time_field, start, end)]
    if args.source_dataset:
        clauses.append({"terms": {"source_dataset": args.source_dataset}})
    if args.dataset_kind == "observed":
        clauses.append({"bool": {"must_not": [{"terms": {"dataset_kind": ["synthetic", "fixture"]}}]}})
    elif args.dataset_kind != "all":
        clauses.append({"term": {"dataset_kind": args.dataset_kind}})
    selection = {"bool": {"filter": clauses}}
    input_count = es.count(index=index_name(args.posts_index), query=selection)["count"]
    if input_count > args.max_documents:
        raise ValueError("Matched documents exceed the explicit processing budget")
    identifier = "social_"+uuid.uuid4().hex
    token = run_id.set(identifier)
    began = time.monotonic()
    total = 0
    counts = WriteCounts()
    seen_rows = 0
    reasons, kinds, fingerprints = Counter(), Counter(), set()
    details = {"eligibility_policy": args.eligibility_policy, "source_datasets": args.source_dataset,
               "max_documents": args.max_documents, "local_only": args.local_only,
               "sentiment_scope": "whole_post_tone_not_target_stance", "truncation_side": args.truncation_side,
               "release_sha256": os.getenv("RELEASE_SHA256"), "filter_reasons": reasons, "content_kinds": kinds}
    try:
        # Model load failures belong to the run audit too; never silently use a fallback.
        model = model or SentimentModel(args.model, use_transformers=not args.no_transformers,
                                       batch_size=args.inference_batch_size, truncation_side=args.truncation_side)
        if args.eligibility_policy != "all":
            language_detector = language_detector or LanguageDetector()
        hits = scan(es,index=index_name(args.posts_index),
                    query={"query": selection, "sort": [{"doc_id": "asc"}]}, preserve_order=True,
                    size=args.scan_size,scroll="30m")
        for group in batched(hits,args.thread_batch_size):
            docs = [{**h["_source"],"doc_id":h["_source"].get("doc_id") or h["_id"]} for h in group]
            seen_rows += len(docs)
            if seen_rows > args.max_documents:
                raise ValueError("Processing budget exceeded during scan")
            decisions, eligible, metadata, originals = [], [], {}, {d["doc_id"]: d for d in docs}
            for doc in docs:
                if args.eligibility_policy == "all":
                    eligible.append(doc)
                    continue
                decision = assess(doc, language_detector)
                if decision["decision"] == "process":
                    if decision["text_fingerprint"] in fingerprints:
                        decision.update(decision="skip", reason="duplicate_model_text")
                    else:
                        fingerprints.add(decision["text_fingerprint"])
                        kinds[decision["content_kind"]] += 1
                        eligible.append({**doc, "raw_text": model_text(doc["raw_text"])})
                reasons[decision["reason"]] += 1
                metadata[doc["doc_id"]] = decision
                decisions.append({**decision, "doc_id": doc["doc_id"], "source_dataset": doc.get("source_dataset"),
                                  "created_at": doc.get("created_at"), "dataset_kind": doc.get("dataset_kind"),
                                  "processing_run_id": identifier, "decided_at": utc_now()})
            if decisions and not args.dry_run:
                bulk_documents("social_processing_decisions", decisions, client=es)
            rows = process_targets(eligible,es,model,max_depth=args.context_max_depth,
                                   use_context=not args.local_only,posts_index=args.posts_index,edges_index=args.edges_index)
            for row in rows:
                if row["doc_id"] in metadata:
                    source = originals[row["doc_id"]]
                    row.update(metadata[row["doc_id"]], raw_text=source["raw_text"],
                               lang=source.get("lang"), country_text_hints=source.get("country_text_hints") or [],
                               collection_profile=source.get("collection_profile") or "legacy_au",
                               pipeline_version="sentiment-v3-global", sentiment_scope="whole_post_tone_not_target_stance",
                               input_preprocessing="url_mention_placeholders_v1", input_max_tokens=512,
                               input_truncation_side=args.truncation_side)
                    if model.pipe is not None:
                        token_length = len(model.pipe.tokenizer.encode(model_text(source["raw_text"]), truncation=False))
                        row.update(input_token_count=token_length, input_truncated=token_length > 512)
                    # Legacy Australian events must not leak into the global analysis cohort.
                    for field in ("nearest_event_id", "nearest_event_name", "days_from_event", "event_period"):
                        row[field] = None
            if not args.dry_run:
                try:
                    counts.add(bulk_documents(args.processed_index,rows,client=es))
                except BulkWriteError as exc:
                    counts.add(exc.counts)
                    raise
            total += len(rows)
            log.info("processing_batch",extra={"fields":{"target_posts":len(docs),"processed_rows":len(rows),"total":total}})
        summary = {
            "run_id":identifier, "component":"social_processing", "status":"success",
            "start_date":start,"end_date":end,"time_field":args.time_field,
            "processed_rows":total,"writes":counts.as_dict(),"dry_run":args.dry_run,
            "duration_seconds":round(time.monotonic()-began,3),"completed_at":utc_now(),
            "model_name":model.model_name,"model_revision":model.revision,
            "input_records": input_count, "seen_rows": seen_rows, "filtered": seen_rows-total,
            "dataset_kind": args.dataset_kind, "details": details,
        }
        if not args.dry_run:
            bulk_documents("pipeline_runs",[summary],"run_id",client=es)
        log.info("processing_complete",extra={"fields":summary})
        return summary
    except Exception:
        log.exception("processing_failed")
        if not args.dry_run:
            try:
                bulk_documents("pipeline_runs",[{"run_id":identifier,"component":"social_processing",
                               "status":"failed","completed_at":utc_now(), "seen_rows": seen_rows,
                               "processed_rows": total, "writes": counts.as_dict(), "details": details}],"run_id",client=es)
            except Exception:
                log.exception("audit_write_failed")
        raise
    finally:
        run_id.reset(token)


def main():
    print(json.dumps(run_pipeline(parse_args()),allow_nan=False))


if __name__ == "__main__":
    main()
