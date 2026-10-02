"""Freshness checks use ingestion timestamps, distinct from event/publication time."""
from datetime import datetime, timezone, timedelta
import json
from uuid import uuid4
from backend.common.es import get_client, bulk_documents
from backend.common.settings import index_name
from backend.common.time import timestamp, utc_now
from backend.common.logging import get_logger

log = get_logger("health")
SOURCES = {"social_discussion_posts_raw": ("fetched_at", 48),
           "gdelt_news_raw": ("fetched_at", 48), "oil_prices_raw": ("fetched_at", 120),
           "social_posts_processed": ("processed_at", 48), "news_processed": ("processed_at", 48),
           "oil_sentiment_corr_results": ("computed_at", 48)}


def check(es, index, field, hours, now=None):
    now = now or datetime.now(timezone.utc)
    response = es.search(index=index_name(index), size=0, track_total_hits=True,
        query={"bool": {"must_not": [{"terms": {"dataset_kind": ["synthetic", "fixture"]}}]}},
        aggs={"latest": {"max": {"field": field}},
              "recent": {"filter": {"range": {field: {"gte": (now-timedelta(hours=24)).isoformat()}}}}})
    latest = response["aggregations"]["latest"].get("value_as_string")
    age = (now-timestamp(latest)).total_seconds()/3600 if latest else None
    return {"total": response["hits"]["total"]["value"], "timestamp_field": field,
            "latest": latest, "last_24h": response["aggregations"]["recent"]["doc_count"],
            "age_hours": age, "stale": age is None or age > hours, "threshold_hours": hours}


def run(es=None):
    es = es or get_client()
    checks, alerts = {}, []
    for index, (field, hours) in SOURCES.items():
        try:
            checks[index] = check(es, index, field, hours)
            if checks[index]["stale"]:
                alerts.append(index + ":stale")
        except Exception:
            checks[index] = {"status": "unavailable"}
            alerts.append(index + ":unavailable")
            log.exception("source_check_failed", extra={"fields": {"index": index}})
    document = {"id": uuid4().hex, "checked_at": utc_now(), "checks": checks, "alerts": alerts,
                "status": "degraded" if alerts else "healthy"}
    bulk_documents("health_log", [document], "id", client=es)
    log.info("health_checked", extra={"fields": document})
    return document


def handler():
    try:
        result = run()
        return result, 503 if result["alerts"] else 200
    except Exception:
        log.exception("health_failed")
        return {"error": "Health check unavailable"}, 503


if __name__ == "__main__":
    print(json.dumps(run(), allow_nan=False))
