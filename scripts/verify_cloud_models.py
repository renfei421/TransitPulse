"""Read back model provenance, probability invariants and the real Fission API.

Post-level records and logs stay under ignored data/artifacts directories. The
exported report contains aggregate measurements only, never credential values.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from elasticsearch import Elasticsearch, helpers
from backend.data_process.llm_annotation import validate_annotation, MAX_INPUT_CHARS
from scripts.audit_search_reuse import save
from scripts.cloud_elasticsearch import Cluster, CONTEXT
from scripts.cloud_elasticsearch import connection
from scripts.cloud_fission import router


def verify(kubeconfig, job_name, directory):
    cloud = Cluster(kubeconfig, CONTEXT)
    job = cloud.get("job", job_name, "default")
    assert job["status"].get("succeeded") == 1
    pods = json.loads(cloud.run("get", "pods", "-n", "default", "-l", "job-name="+job_name, "-o", "json"))["items"]
    assert len(pods) == 1
    pod = pods[0]
    logs = cloud.run("logs", pod["metadata"]["name"], "-n", "default", "-c", "processor")
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/model-processor.log").write_text(logs, encoding="utf-8")
    save(Path("artifacts/model-job-completed.json"), job)
    save(Path("artifacts/model-pod-completed.json"), pod)
    summary = json.loads(logs.strip().splitlines()[-1])
    assert summary["status"] == "success" and summary["writes"]["failed"] == 0
    auth = cloud.secret("transport-secrets", "default")
    with connection(cloud) as (_, endpoint, ca), Elasticsearch(endpoint,
            basic_auth=(auth["ES_USER"], auth["ES_PASSWORD"]), ca_certs=str(ca)) as es:
        collections = {}
        for logical in ("social_discussion_posts_raw", "social_posts_processed", "social_processing_decisions", "social_posts_annotations"):
            rows = [h["_source"] for h in helpers.scan(es, index="v2_"+logical, query={"query": {"match_all": {}}})]
            collections[logical] = rows
            save(directory/(logical+".json"), rows)
    raw = {r["doc_id"]: r for r in collections["social_discussion_posts_raw"]}
    processed = collections["social_posts_processed"]
    decisions = collections["social_processing_decisions"]
    annotations = collections["social_posts_annotations"]
    assert len(raw) == summary["input_records"] == len(decisions)
    assert len(processed) == summary["processed_rows"]
    for row in processed:
        original = raw[row["doc_id"]]
        assert row["raw_text"] == original["raw_text"] and row["source_dataset"] == original["source_dataset"]
        assert row["model_revision"] == summary["model_revision"]
        assert row["sentiment_scope"] == "whole_post_tone_not_target_stance"
        assert row["decision"] == "process" and row["reason"] == "eligible"
        assert row["detected_language"] == "en" and not row.get("nearest_event_id")
        # Three-class probabilities are retained independently of polarity.
        probabilities = row["sentiment_probabilities"]
        assert set(probabilities) == {"positive", "neutral", "negative"}
        assert all(0 <= p <= 1 for p in probabilities.values())
        assert abs(sum(probabilities.values())-1) < 1e-5
    for row in annotations:
        original = raw[row["source_doc_id"]]
        assert row["input_sha256"] == hashlib.sha256(original["raw_text"].encode()).hexdigest()
        assert row["source_dataset"] == original["source_dataset"]
        validate_annotation({key: row[key] for key in ("relevance", "content_type", "overall_tone", "targets", "mode_switch", "needs_review")},
                            original["raw_text"][:MAX_INPUT_CHARS])
    api_checks = []
    params = {"from": "2026-07-03", "to": "2026-10-02", "timezone": "UTC", "dataset_kind": "live"}
    with router(cloud) as (session, base):
        def query(path, extra=None, status=200):
            result = session.get(base+"/api/v1/"+path, params={**params, **(extra or {})}, timeout=30)
            assert result.status_code == status, (path, result.status_code)
            api_checks.append({"resource": path, "filters": extra or {}, "status": status})
            return result.json()
        quality = query("quality")["data"]
        assert quality["raw_documents"] == len(raw) and quality["processed_documents"] == len(processed)
        for resource, rows in (("social/posts", processed), ("social/annotations", annotations)):
            ids, cursor = set(), None
            while True:
                page = query(resource, {"limit": 100, **({"cursor": cursor} if cursor else {})})["data"]
                page_ids = {r["doc_id"] for r in page["items"]}
                assert not ids & page_ids
                ids.update(page_ids)
                cursor = page["next_cursor"]
                if not cursor:
                    break
            assert ids == {r["doc_id"] for r in rows}
        kind_quality = query("quality", {"content_kind": "commentary_candidate"})["data"]
        assert kind_quality["raw_documents"] == len(raw)
        assert kind_quality["selected_processed_documents"] == sum(r["content_kind"] == "commentary_candidate" for r in processed)
        assert query("social/sentiment", {"topic": "oil_vehicle"})["data"]
        assert query("social/annotations", {"relevance": "unrelated"})["data"]["items"]
        query("social/posts", {"timezone": "invalid"}, status=400)
        query("social/annotations", {"content_type": "invalid"}, status=400)
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "context": CONTEXT,
        "job": job_name, "job_start": job["status"]["startTime"], "job_complete": job["status"]["completionTime"],
        "pipeline_summary": summary, "counts": {name: len(rows) for name, rows in collections.items()},
        "readback_checks": {"processed_original_text_source_model_probabilities": len(processed),
                            "annotation_input_hash_source_schema_evidence": len(annotations)},
        "processed_labels": dict(Counter(r["contextual_sentiment_label"] for r in processed)),
        "processed_input_truncated": sum(r["input_truncated"] for r in processed),
        "processed_content_kinds": dict(Counter(r["content_kind"] for r in processed)),
        "filter_reasons": dict(Counter(r["reason"] for r in decisions)),
        "api_quality": quality, "api_checks": api_checks,
        "api_package": json.loads(Path("artifacts/cloud-api-package.json").read_text()),
        "limits": ["Batch acceptance on the existing cluster, not continuous collection or autoscaling proof",
                   "LLM schema/evidence checks do not establish semantic correctness", "Observed sample, not a population estimate"]}
    save(directory/"verification.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--directory", type=Path, default=Path("data/cloud_model_20261001"))
    args = parser.parse_args()
    verify(args.kubeconfig, args.job_name, args.directory)
