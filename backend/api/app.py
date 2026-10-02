"""One router shared by local HTTP tests and the Fission function."""
import base64
from datetime import date, timedelta
import json
import os
import time
from urllib.parse import urlsplit
from uuid import uuid4

from flask import Flask, Response, request
from backend.common.es import get_client, json_safe
from backend.common.logging import get_logger, run_id
from backend.common.settings import index_name
from backend.api import queries

log = get_logger("api")
BASE = "/api/v1"
RESOURCES = ("social/volume", "social/sentiment", "social/posts", "social/annotations", "news/volume",
             "news/sentiment", "oil/prices", "platforms/profiles", "analyses/oil-sentiment", "quality",
             "social/target-sentiment", "experiments/status")
LEGACY = {
    "/api/social-topic-volume": "social/volume", "/api/social-daily-sentiment": "social/sentiment",
    "/api/gdelt-daily-volume": "news/volume", "/api/news-daily-sentiment": "news/sentiment",
    "/api/oil-daily-price": "oil/prices", "/api/platform-subtopic-profile": "platforms/profiles",
    "/api/oil-sentiment-corr-result": "analyses/oil-sentiment",
    "/api/oil-sentiment-corr": "analyses/oil-sentiment",
}


def response(data=None, status=200, *, message=None, meta=None, headers=None):
    payload = {"data": data, "meta": meta or {}} if message is None else {"error": message}
    payload["request_id"] = run_id.get()
    return Response(json.dumps(json_safe(payload), ensure_ascii=False, allow_nan=False), status=status,
                    mimetype="application/json", headers={"X-Request-ID": run_id.get(), **(headers or {})})


def parameters():
    today = date.today()
    start = request.args.get("from", (today - timedelta(days=29)).isoformat())
    end = request.args.get("to", today.isoformat())
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or (last - first).days > 366:
        raise ValueError("Date range must be ordered and at most 367 days inclusive")
    options = {key: request.args[key] for key in ("dataset_kind", "platform", "topic", "source_dataset",
                                                 "model", "keyword", "timezone", "content_kind", "content_type", "relevance", "experiment_id") if key in request.args}
    if options.get("dataset_kind", "observed") not in ("observed", "live", "imported", "synthetic", "all"):
        raise ValueError("Invalid dataset_kind")
    if options.get("topic") and options["topic"] not in queries.TOPICS:
        raise ValueError("Invalid topic")
    for key, allowed in (("timezone", ("UTC", "Australia/Sydney")), ("content_kind", queries.CONTENT_KINDS),
                         ("content_type", queries.CONTENT_TYPES), ("relevance", queries.RELEVANCE)):
        if key in options and options[key] not in allowed:
            raise ValueError(f"Invalid {key}")
    if any(len(value) > 200 for value in options.values()):
        raise ValueError("Filter exceeds 200 characters")
    return first.isoformat(), last.isoformat(), options


def posts(es, start, end, options, *, annotations=False):
    limit = int(request.args.get("limit", "25"))
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor = request.args.get("cursor")
    search_after = None
    if cursor:
        if len(cursor) > 1024:
            raise ValueError("Invalid cursor")
        try:
            search_after = json.loads(base64.urlsafe_b64decode(cursor))
            if not isinstance(search_after, list) or len(search_after) != 2:
                raise ValueError()
        except Exception as exc:
            raise ValueError("Invalid cursor") from exc
    query_filters = queries.filters(start, end, processed=not annotations, options=options)
    if annotations:
        query_filters.append({"term": {"schema_version": 1}})
    source_fields = (["doc_id", "source_doc_id", "annotation_id", "platform", "created_at", "raw_text", "source_dataset",
                      "dataset_kind", "model_name", "model_revision", "prompt_version", "contract_sha256", "relevance",
                      "content_type", "overall_tone", "targets", "mode_switch", "needs_review", "input_truncated",
                      "annotated_at"] if annotations else
                     ["doc_id", "platform", "created_at", "raw_text", "candidate_topics", "topic_source",
                      "contextual_sentiment_label", "contextual_sentiment_polarity", "model_name",
                      "contextual_sentiment_score", "thread_root_id", "parent_post_id", "dataset_kind", "source_dataset",
                      "model_revision", "processing_run_id", "sentiment_scope", "eligibility_policy", "content_kind",
                      "detected_language", "language_confidence", "input_truncated", "input_token_count",
                      "experiment_id", "experiment_phase", "target_sentiments", "jev.relevance", "jev.content_type",
                      "jev.contract_sha256", "jev.rubric_version"])
    result = es.search(index=index_name("social_posts_annotations" if annotations else queries.social_index(options)), size=limit,
        query={"bool": {"filter": query_filters}},
        sort=[{"created_at": "asc"}, {"doc_id": "asc"}], search_after=search_after,
        source=source_fields)
    hits = result["hits"]["hits"]
    next_cursor = base64.urlsafe_b64encode(json.dumps(hits[-1]["sort"]).encode()).decode() if len(hits) == limit else None
    return {"items": [hit["_source"] for hit in hits], "next_cursor": next_cursor}


def dispatch():
    path = urlsplit(request.headers.get("X-Fission-Full-Url") or request.path).path.rstrip("/")
    if request.method != "GET":
        return response(status=405, message="Method not allowed", headers={"Allow": "GET"})
    if path in (BASE, BASE + "/meta"):
        return response({"version": 1, "links": {r: BASE + "/" + r for r in (*RESOURCES, "health", "openapi.json")},
                         "timezone": "Australia/Sydney", "sentiment": "(positive-negative)/classified_count",
                         "sample_policy": "Synthetic records excluded by default; set dataset_kind=synthetic explicitly.",
                         "model_policy": "Use model filter to compare one model. /quality reports classifier model mixtures. LLM annotations are separate.",
                         "default_social_model": os.getenv("SOCIAL_ACTIVE_MODEL") or None,
                         "social_timezones": ["Australia/Sydney", "UTC"]})
    if path == BASE + "/openapi.json":
        from pathlib import Path
        return Response(Path(__file__).with_name("openapi.json").read_text(encoding="utf-8"), mimetype="application/json")
    if path == BASE + "/health":
        if not get_client().ping():
            return response(status=503, message="Elasticsearch unavailable")
        return response({"status": "ready"})
    resource = LEGACY.get(path) or (path[len(BASE)+1:] if path.startswith(BASE + "/") else "")
    if resource not in RESOURCES:
        return response(status=404, message="Resource not found")
    if resource == "experiments/status":
        from backend.api.jev_queries import experiment_status
        experiment_id=request.args.get("experiment_id", "")
        if not experiment_id or len(experiment_id)>100 or not all(c.isalnum() or c in "-_" for c in experiment_id):
            raise ValueError("A valid experiment_id is required")
        data=experiment_status(get_client(),experiment_id)
        return response(data) if data is not None else response(status=404,message="Experiment not found")
    start, end, options = parameters()
    if set(options) - queries.RESOURCE_FILTERS[resource]:
        raise ValueError("Filter is not supported by this resource")
    if resource in {"social/volume", "social/sentiment", "social/posts", "social/target-sentiment", "platforms/profiles", "quality"}:
        active_model = os.getenv("SOCIAL_ACTIVE_MODEL")
        if active_model and not options.get("model"):
            options["model"] = active_model
    if resource == "analyses/oil-sentiment" and (("from" in request.args) != ("to" in request.args)):
        raise ValueError("Provide both dates for an exact precomputed window")
    es = get_client()
    if resource == "platforms/profiles":
        data = queries.profiles(es, start, end, options)
    elif resource == "analyses/oil-sentiment":
        data = queries.correlation(es, start if "from" in request.args else None, end if "to" in request.args else None, options)
        if data is None:
            return response(status=404, message="No precomputed result for this window")
    elif resource == "quality":
        data = queries.quality(es, start, end, options)
    elif resource == "social/target-sentiment":
        from backend.api.jev_queries import target_daily
        data = target_daily(es, start, end, options)
    elif resource in ("social/posts", "social/annotations"):
        data = posts(es, start, end, options, annotations=resource == "social/annotations")
    else:
        data = queries.daily(es, resource, start, end, options)
    headers = {"Deprecation": "true", "Link": f'<{BASE}/{resource}>; rel="successor-version"'} if path in LEGACY else {}
    return response(data, meta={"from": start, "to": end, "dataset_kind": options.get("dataset_kind", "observed"),
         "sentiment_definition": ("mean P(positive)-P(negative) among accepted poster-target attitudes" if resource == "social/target-sentiment" else
             "target labels and overall tone; no calibrated probabilities" if resource == "social/annotations" else "(positive-negative)/classified_count"),
         "timezone": options.get("timezone", "Australia/Sydney"),
         "model": options.get("model"),
         "topic_counts_overlap": True}, headers=headers)


def main():
    token = run_id.set(str(uuid4()))
    started = time.perf_counter()
    result = None
    try:
        result = dispatch()
    except (ValueError, TypeError):
        result = response(status=400, message="Invalid query parameters; see /api/v1/openapi.json")
    except Exception:
        log.exception("request_failed")
        result = response(status=503, message="Data service unavailable")
    finally:
        log.info("request_completed", extra={"fields": {"method": request.method, "path": request.path,
                 "status": result.status_code if result else 503,
                 "duration_ms": round((time.perf_counter()-started)*1000, 3)}})
        run_id.reset(token)
    return result


def create_app():
    app = Flask(__name__)
    def route(path):
        return main()
    app.add_url_rule("/", defaults={"path": ""}, view_func=route, methods=["GET", "POST", "PUT", "DELETE"])
    app.add_url_rule("/<path:path>", view_func=route, methods=["GET", "POST", "PUT", "DELETE"])
    return app


if __name__ == "__main__":
    import os
    create_app().run(host="127.0.0.1", port=int(os.getenv("PORT", "9090")), debug=False)
