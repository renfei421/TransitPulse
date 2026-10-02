"""Run a cost-bounded structured annotation sample using cloud raw documents.

Uses an existing local API credential, never copies it to Kubernetes. Cache and
post-level responses remain in ignored data/. Aggregate reports are exportable.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from elasticsearch import Elasticsearch
from backend.common.es import create_documents
from backend.data_process.llm_annotation import StructuredAnnotator, AnnotationError, MODEL, PROMPT_VERSION, CONTRACT_SHA256
from scripts.audit_search_reuse import save
from scripts.cloud_elasticsearch import Cluster, CONTEXT, connection


def run(sample, directory, kubeconfig, *, limit=30, budget=.5, ingest=False):
    if not 1 <= limit <= 30:
        raise ValueError("This acceptance pilot permits at most 30 samples")
    samples = json.loads(sample.read_text(encoding="utf-8"))[:limit]
    cloud = Cluster(kubeconfig, CONTEXT)
    auth = cloud.secret("transport-secrets", "default")
    with connection(cloud) as (_, endpoint, ca), Elasticsearch(endpoint,
            basic_auth=(auth["ES_USER"], auth["ES_PASSWORD"]), ca_certs=str(ca)) as es:
        response = es.mget(index="v2_social_discussion_posts_raw", ids=[s["doc"]["doc_id"] for s in samples])
        if not all(h.get("found") for h in response["docs"]):
            raise ValueError("A sample is absent from cloud raw storage")
        docs = [h["_source"] for h in response["docs"]]
    annotator = StructuredAnnotator(budget_usd=budget)
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(annotator.annotate, d): d for d in docs}
        for future in as_completed(futures):
            doc = futures[future]
            try:
                result = future.result()
                annotation = result.pop("annotation")
                row = {**result, **annotation, "source_dataset": doc["source_dataset"], "dataset_kind": doc["dataset_kind"],
                       "platform": doc["platform"], "created_at": doc["created_at"], "raw_text": doc["raw_text"],
                       "annotated_at": datetime.now(timezone.utc).isoformat(), "schema_version": 1}
                results.append(row)
                save(directory/"results"/(row["annotation_id"]+".json"), row)
                print(json.dumps({"completed": len(results), "cache_hit": row["cache_hit"],
                                  "relevance": row["relevance"], "content_type": row["content_type"]}), flush=True)
            except Exception as exc:
                errors.append({"doc_id": doc["doc_id"], "error_type": type(exc).__name__,
                               "reason": str(exc) if isinstance(exc, AnnotationError) else "See local diagnostics"})
                print(json.dumps({"failed": len(errors), "error_type": type(exc).__name__}), flush=True)
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "model": MODEL, "prompt_version": PROMPT_VERSION,
        "contract_sha256": CONTRACT_SHA256, "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
        "requested": len(samples), "succeeded": len(results), "failed": len(errors), "errors": errors,
        "cache_hits": sum(r["cache_hit"] for r in results), "budget_usd": budget,
        "reserved_usd_this_run": annotator.reserved,
        "estimated_cost_usd_all_successful_samples": sum(r["estimated_cost_usd"] for r in results),
        "estimated_cost_usd_new_successful_calls": sum(r["estimated_cost_usd"] for r in results if not r["cache_hit"]),
        "relevance": dict(Counter(r["relevance"] for r in results)),
        "content_types": dict(Counter(r["content_type"] for r in results)),
        "overall_tones": dict(Counter(r["overall_tone"] for r in results)),
        "needs_review": sum(r["needs_review"] for r in results), "input_truncated": sum(r["input_truncated"] for r in results),
        "explicit_mode_switches": sum(r["mode_switch"]["explicit"] for r in results),
        "sink": {"status": "not_run"},
        "limits": ["LLM labels and earlier assistant labels are not human gold", "Estimated token cost is not a billing invoice",
                   "Invalid/refused/incomplete responses are failures; no automatic billable retry",
                   "Whole-post tone and per-target stance are different measurements"]}
    if ingest and results:
        previous = os.environ.get("ES_INDEX_PREFIX")
        os.environ["ES_INDEX_PREFIX"] = "v2_"
        try:
            with connection(cloud) as (_, endpoint, ca), Elasticsearch(endpoint,
                    basic_auth=(auth["ES_USER"], auth["ES_PASSWORD"]), ca_certs=str(ca)) as es:
                rows = [{**r, "doc_id": r["annotation_id"], "source_doc_id": r["doc_id"]} for r in results]
                counts = create_documents("social_posts_annotations", rows, client=es)
                stored = es.mget(index="v2_social_posts_annotations", ids=[r["doc_id"] for r in rows])["docs"]
                # Replays preserve the original annotation timestamp and billing metadata.
                fields = ("source_doc_id", "input_sha256", "contract_sha256", "relevance", "targets", "mode_switch")
                verified = sum(h.get("found") and all(h["_source"].get(k) == row.get(k) for k in fields)
                               for h, row in zip(stored, rows))
                report["sink"] = {"status": "completed" if verified == len(rows) else "verification_failed",
                                  **counts.as_dict(), "readback_verified": verified}
                if verified != len(rows):
                    raise ValueError("Stored annotation readback mismatch")
        except Exception as exc:
            report["sink"] = {**report["sink"], "status": "failed", "error_type": type(exc).__name__}
            raise
        finally:
            if previous is None:
                os.environ.pop("ES_INDEX_PREFIX", None)
            else:
                os.environ["ES_INDEX_PREFIX"] = previous
            save(directory/"report.json", report)
    save(directory/"report.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--budget", type=float, default=.5)
    parser.add_argument("--ingest", action="store_true")
    args = parser.parse_args()
    run(args.sample, args.directory, args.kubeconfig, limit=args.limit, budget=args.budget, ingest=args.ingest)
