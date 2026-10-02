"""Measure local synthetic ingestion/query throughput; never relabel it as live data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import statistics
import time
import tracemalloc
from uuid import uuid4
from backend.common.es import get_client, bulk_documents
from backend.common.settings import index_name
from backend.data_process.cloud_sentiment.cloud_sentiment_pipeline import batched
from database.migrate import definitions


def document(i):
    return {"doc_id": f"synthetic-{i}", "post_id": f"synthetic-{i}", "platform": "bluesky" if i % 2 else "mastodon",
            "raw_text": f"Synthetic transport benchmark record {i}: petrol prices and electric vehicles.",
            "created_at": f"2026-09-{i % 28+1:02d}T08:00:00Z", "fetched_at": "2026-09-30T00:00:00Z",
            "candidate_topics": ["fuel_price", "ev"], "schema_version": 2, "dataset_kind": "synthetic",
            "source_dataset": "benchmark_v1", "thread_root_id": f"thread-{i//5}",
            "author_id_hash": f"author-{i % 10000}"}


def create(es, logical):
    definition = definitions()[logical]
    definition["settings"] = {"number_of_shards": 1, "number_of_replicas": 0}
    es.indices.create(index=index_name(logical), body=definition)


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values)-1, int((len(values)-1)*p))]


def run(records=100000, batch_size=500, queries=100, concurrency=4, baseline_records=200):
    if min(records, batch_size, queries, concurrency, baseline_records) < 1:
        raise ValueError("Budgets must be positive")
    prefix = "bench_"+uuid4().hex[:10]+"_"
    os.environ["ES_INDEX_PREFIX"] = prefix
    es = get_client()
    logical = "social_discussion_posts_raw"
    create(es, logical)
    began = time.perf_counter()
    for i in range(baseline_records):
        bulk_documents(logical, [document(i)], client=es)
    single_seconds = time.perf_counter()-began
    # Recreate only the exact benchmark-owned index for a fair fresh bulk run.
    es.indices.delete(index=index_name(logical))
    create(es, logical)
    began = time.perf_counter()
    for group in batched((document(i) for i in range(baseline_records)), batch_size):
        bulk_documents(logical, group, client=es)
    small_bulk_seconds = time.perf_counter()-began
    es.indices.delete(index=index_name(logical))
    create(es, logical)
    tracemalloc.start()
    began = time.perf_counter()
    for group in batched((document(i) for i in range(records)), batch_size):
        bulk_documents(logical, group, client=es)
    ingest_seconds = time.perf_counter()-began
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    es.indices.refresh(index=index_name(logical))
    stored = es.count(index=index_name(logical))["count"]
    def query(_):
        started = time.perf_counter()
        response = es.search(index=index_name(logical), size=0,
            query={"term": {"candidate_topics": "fuel_price"}},
            aggs={"days": {"date_histogram": {"field": "created_at", "calendar_interval": "day"}},
                  "authors": {"cardinality": {"field": "author_id_hash"}}})
        assert len(response["aggregations"]["days"]["buckets"]) == 28 if records >= 28 else True
        return (time.perf_counter()-started)*1000
    for n in range(5):
        query(n)
    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        latencies = list(executor.map(query, range(queries)))
    query_seconds = time.perf_counter()-began
    return {"kind": "synthetic_local_benchmark", "hardware": {"platform": platform.platform(),
        "python": platform.python_version(), "logical_cpu_count": os.cpu_count()},
        "elasticsearch": es.info()["version"]["number"], "index_prefix": prefix, "stored_documents": stored,
        "configuration": {"batch_size": batch_size, "shards": 1, "replicas": 0,
                          "http_concurrency": concurrency, "query_requests": queries, "baseline_records": baseline_records},
        "ingestion": {"seconds": ingest_seconds, "documents_per_second": records/ingest_seconds,
                      "python_traced_peak_mb": peak/1024/1024,
                      "single_request_baseline_seconds": single_seconds,
                      "same_size_bulk_seconds": small_bulk_seconds,
                      "same_size_speedup": single_seconds/small_bulk_seconds},
        "query": {"p50_ms": statistics.median(latencies), "p95_ms": percentile(latencies, .95),
                  "p99_ms": percentile(latencies, .99), "requests_per_second": queries/query_seconds},
        "limitations": ["Synthetic text, no neural inference", "Single local node; no distributed-scaling claim",
                        "Memory is Python traced allocations, not total process or Elasticsearch RSS",
                        "Warm repeated aggregation queries; not uncached production traffic"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=100000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--baseline-records", type=int, default=200)
    parser.add_argument("--output", default="artifacts/benchmark.json")
    args = vars(parser.parse_args())
    output = Path(args.pop("output"))
    result = run(**args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, indent=2))
