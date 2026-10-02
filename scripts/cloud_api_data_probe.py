"""Verify real cloud API queries using two temporary, explicitly synthetic rows.

Writes only UUID-named probe documents into the existing processed index using
the application identity; removes those exact IDs with the administrator in a
finally block. This verifies API/ES integration, not ingestion or model inference.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

from scripts.cloud_elasticsearch import Cluster, CONTEXT, call, connection
from scripts.cloud_fission import router

ROOT = Path(__file__).resolve().parents[1]


def verify(cluster):
    now = datetime.now(timezone.utc)
    dataset = "cloud-api-probe-" + uuid.uuid4().hex
    ids = [dataset + "-" + str(i) for i in range(2)]
    index = "v2_social_posts_processed"
    checks = []
    with connection(cluster) as (es, endpoint, _), router(cluster) as (http, api):
        admin = ("elastic", cluster.secret("elasticsearch-es-elastic-user")["elastic"])
        credentials = cluster.secret("transport-secrets", "default")
        app = (credentials["ES_USER"], credentials["ES_PASSWORD"])
        params = {"from": (now-timedelta(days=1)).date().isoformat(),
                  "to": (now+timedelta(days=1)).date().isoformat(), "source_dataset": dataset}

        def query(resource, **extra):
            response = http.get(api+"/api/v1/"+resource, params={**params, **extra}, timeout=30)
            if response.status_code != 200:
                raise RuntimeError("Cloud fixture query failed: " + resource)
            return response.json()["data"]

        def check(name, condition):
            if not condition:
                raise AssertionError(name)
            checks.append({"check": name, "passed": True})

        try:
            for identifier in ids:
                call(es, endpoint, "PUT", f"/{index}/_create/{identifier}?refresh=wait_for", app,
                     {"doc_id": identifier, "post_id": identifier, "platform": "cloud_probe",
                      "created_at": now.isoformat(), "processed_at": now.isoformat(),
                      "dataset_kind": "synthetic", "source_dataset": dataset, "schema_version": 2,
                      "raw_text": "Synthetic acceptance example: petrol prices are terrible.",
                      "candidate_topics": ["fuel_price"], "thread_root_id": ids[0],
                      "contextual_sentiment_label": "negative", "contextual_sentiment_polarity": -1.0,
                      "model_name": "fixture-no-inference"})
            check("default_excludes_synthetic", query("social/posts")["items"] == [])
            options = {"dataset_kind": "synthetic", "platform": "cloud_probe", "topic": "fuel_price"}
            first = query("social/posts", **options, limit=1)
            second = query("social/posts", **options, limit=1, cursor=first["next_cursor"])
            check("cursor_pages_are_disjoint_and_complete",
                  {first["items"][0]["doc_id"], second["items"][0]["doc_id"]} == set(ids))
            last = query("social/posts", **options, limit=1, cursor=second["next_cursor"])
            check("pagination_terminates", last == {"items": [], "next_cursor": None})
            check("topic_filter_applies", query("social/posts", dataset_kind="synthetic", topic="ev")["items"] == [])
            sentiment = query("social/sentiment", **options)
            check("signed_sentiment_and_counts", len(sentiment) == 1 and
                  sentiment[0]["doc_count"] == 2 and sentiment[0]["negative"] == 2 and
                  sentiment[0]["net_sentiment"] == -1)
            profiles = query("platforms/profiles", **options)
            check("platform_aggregation", profiles["cloud_probe"]["fuel_price"]["negative"] == 2)
            quality = query("quality", **options)
            check("quality_does_not_invent_raw_coverage", quality["selected_processed_documents"] == 2 and
                  quality["raw_documents"] == 0 and quality["processing_coverage"] is None)
        finally:
            # No wildcard, delete-by-query, mapping change, or index deletion.
            for identifier in ids:
                call(es, endpoint, "DELETE", f"/{index}/_doc/{identifier}?refresh=wait_for", admin,
                     expected=(200, 404))
        check("exact_probe_documents_removed", query("social/posts", dataset_kind="synthetic")["items"] == [])
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "context": cluster.context,
              "index": index, "dataset_kind": "synthetic", "temporary_documents": len(ids), "checks": checks,
              "limits": ["Precomputed fixture labels; no sentiment model executed",
                         "No harvesting, throughput, or autoscaling test"]}
    output = ROOT/"artifacts/cloud-api-data-verification.json"
    output.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8", newline="\n")
    print(json.dumps({"data_checks_passed": len(checks), "evidence": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default=CONTEXT)
    args = parser.parse_args()
    verify(Cluster(args.kubeconfig, args.context))
