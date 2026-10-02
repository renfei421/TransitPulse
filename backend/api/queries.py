"""Bounded resource queries. Confidence is never used as sentiment direction."""
from backend.common.settings import index_name
from backend.common.time import range_query, day_bounds
import os


def social_index(options=None):
    model = (options or {}).get("model") or os.getenv("SOCIAL_ACTIVE_MODEL", "")
    return "social_posts_jev" if model.startswith("jev-") else "social_posts_processed"

TOPICS = ("ev", "public_transport", "fuel_price", "oil_vehicle")
LABELS = ("positive", "neutral", "negative")
CONTENT_KINDS = ("commentary_candidate", "link_post", "other_text", "promotion_candidate")
CONTENT_TYPES = ("personal_experience", "opinion", "news_or_link", "promotion", "other", "uncertain")
RELEVANCE = ("related", "adjacent", "unrelated", "uncertain")
SOCIAL_FILTERS = {"dataset_kind", "platform", "topic", "source_dataset", "model", "timezone", "content_kind", "experiment_id"}
RESOURCE_FILTERS = {
    **{name: SOCIAL_FILTERS for name in ("social/volume", "social/sentiment", "social/posts", "platforms/profiles", "quality")},
    "social/target-sentiment": SOCIAL_FILTERS,
    "social/annotations": {"dataset_kind", "platform", "source_dataset", "model", "timezone", "content_type", "relevance"},
    "news/volume": {"dataset_kind", "source_dataset", "keyword"},
    "news/sentiment": {"dataset_kind", "source_dataset", "model"},
    "oil/prices": {"dataset_kind", "source_dataset"},
    "analyses/oil-sentiment": {"dataset_kind", "model"},
}


def filters(start, end, field="created_at", *, processed=False, options=None):
    options = options or {}
    first, last = day_bounds(start, end, options.get("timezone", "Australia/Sydney"))
    result = [range_query(field, first, last)]
    if processed:
        result.append({"term": {"schema_version": 2}})
    kind = options.get("dataset_kind", "observed")
    if kind == "observed":
        result.append({"bool": {"must_not": [{"terms": {"dataset_kind": ["synthetic", "fixture"]}}]}})
    elif kind != "all":
        result.append({"term": {"dataset_kind": kind}})
    for argument, stored in (("platform", "platform"), ("topic", "candidate_topics"),
                             ("source_dataset", "source_dataset"), ("model", "model_name"),
                             ("content_kind", "content_kind"), ("content_type", "content_type"), ("relevance", "relevance")):
        if options.get(argument):
            result.append({"term": {stored: options[argument]}})
    if options.get("experiment_id"):
        result.append({"term": {"experiment_id": options["experiment_id"]}})
    return result


def label_aggs(label="contextual_sentiment_label", polarity="contextual_sentiment_polarity"):
    return {
        "labels": {"filters": {"filters": {name: {"term": {label: name}} for name in LABELS}}},
        "polarity": {"avg": {"field": polarity}},
        "threads": {"cardinality": {"field": "thread_root_id", "precision_threshold": 1000}},
    }


def topic_aggs():
    return {"by_topic": {"filters": {"filters": {t: {"term": {"candidate_topics": t}} for t in TOPICS}},
                          "aggs": label_aggs()}}


def summary(bucket):
    counts = {label: bucket["labels"]["buckets"][label]["doc_count"] for label in LABELS}
    classified = sum(counts.values())
    net = (counts["positive"] - counts["negative"]) / classified if classified else None
    return {**counts, "doc_count": bucket["doc_count"], "classified_count": classified,
            "unclassified_count": bucket["doc_count"] - classified,
            "net_sentiment": net, "avg_sentiment": net,
            "mean_model_polarity": bucket["polarity"]["value"],
            "unique_threads_approx": bucket["threads"]["value"]}


def daily(es, resource, start, end, options=None):
    specifications = {
        "social/volume": (social_index(options), "created_at", topic_aggs(), True),
        "social/sentiment": (social_index(options), "created_at", topic_aggs(), True),
        "news/sentiment": ("news_processed", "time", label_aggs("sentiment", "sentiment_polarity"), True),
        "news/volume": ("gdelt_news_volume_raw", "date",
                        {"daily_volume": {"sum": {"field": "volume"}},
                         "avg_share_percent": {"avg": {"field": "share_percent"}}}, False),
        "oil/prices": ("oil_prices_raw", "date",
                       {"avg_price": {"avg": {"field": "price"}},
                        "daily_return_pct": {"avg": {"field": "daily_return_pct"}}}, False),
    }
    index, field, aggs, processed = specifications[resource]
    query_filters = filters(start, end, field, processed=processed, options=options)
    if resource == "oil/prices":
        query_filters.append({"term": {"ticker": "BRENT"}})
    if resource == "news/volume":
        query_filters.append({"term": {"keyword": (options or {}).get("keyword", "iran_hormuz")}})
    # Date-only prices/volumes are publication dates, not Sydney timestamp instants.
    if field == "date":
        query_filters[0] = {"range": {"date": {"gte": start, "lte": end}}}
    response = es.search(index=index_name(index), size=0, query={"bool": {"filter": query_filters}},
        aggs={"by_day": {"date_histogram": {"field": field, "calendar_interval": "day",
                 "time_zone": "UTC" if field == "date" else (options or {}).get("timezone", "Australia/Sydney"), "min_doc_count": 1},
                         "aggs": aggs}})
    rows = []
    for day in response["aggregations"]["by_day"]["buckets"]:
        stamp = day["key_as_string"][:10]
        if resource.startswith("social/"):
            for topic, bucket in day["by_topic"]["buckets"].items():
                if bucket["doc_count"]:
                    rows.append({"date": stamp, "topic": topic, "value": bucket["doc_count"], **summary(bucket)})
        elif resource == "news/sentiment":
            rows.append({"date": stamp, **summary(day)})
        else:
            rows.append({"date": stamp, **{key: day[key]["value"] for key in aggs}})
    return rows


def profiles(es, start, end, options=None):
    response = es.search(index=index_name(social_index(options)), size=0,
        query={"bool": {"filter": filters(start, end, processed=True, options=options)}},
        aggs={"platforms": {"terms": {"field": "platform", "size": 20}, "aggs": topic_aggs()}})
    return {platform["key"]: {topic: summary(bucket) for topic, bucket in
             platform["by_topic"]["buckets"].items()} for platform in response["aggregations"]["platforms"]["buckets"]}


def correlation(es, start=None, end=None, options=None):
    terms = [{"term": {"pipeline_version": "correlation-v2"}}]
    options = options or {}
    kind = options.get("dataset_kind", "observed")
    if kind == "observed":
        terms.append({"bool": {"must_not": [{"terms": {"dataset_kind": ["synthetic", "fixture"]}}]}})
    elif kind != "all":
        terms.append({"term": {"dataset_kind": kind}})
    if options.get("model"):
        terms.append({"term": {"model_name": options["model"]}})
    if start and end:
        terms.extend([{"term": {"from_date": start}}, {"term": {"to_date": end}}])
    response = es.search(index=index_name("oil_sentiment_corr_results"), size=1,
                         query={"bool": {"filter": terms}}, sort=[{"computed_at": "desc"}])
    hits = response["hits"]["hits"]
    return hits[0]["_source"] if hits else None


def quality(es, start, end, options=None):
    options = options or {}
    # Raw records have neither model output nor inherited topic assignments.
    # Coverage therefore uses the same source/date/platform cohort, before topic filtering.
    source_options = {key: value for key, value in options.items() if key not in {"topic", "model", "content_kind"}}
    base = filters(start, end, options=source_options)
    raw = es.count(index=index_name("social_discussion_posts_raw"), query={"bool": {"filter": base}})["count"]
    coverage_options = {key: value for key, value in options.items() if key not in {"topic", "content_kind"}}
    processed_cohort = es.count(index=index_name(social_index(options)),
        query={"bool": {"filter": filters(start, end, processed=True, options=coverage_options)}})["count"]
    response = es.search(index=index_name(social_index(options)), size=0, track_total_hits=True,
        query={"bool": {"filter": filters(start, end, processed=True, options=options)}},
        aggs={
            "authors": {"cardinality": {"field": "author_id_hash", "precision_threshold": 1000}},
            "threads": {"cardinality": {"field": "thread_root_id", "precision_threshold": 1000}},
            "missing_topic": {"missing": {"field": "candidate_topics"}},
            "missing_context": {"filter": {"term": {"context_missing_parent": True}}},
            "models": {"terms": {"field": "model_name", "size": 20}},
            "latest_fetched": {"max": {"field": "fetched_at"}},
            "latest_processed": {"max": {"field": "processed_at"}},
        })
    a = response["aggregations"]
    processed = response["hits"]["total"]["value"]
    return {"raw_documents": raw, "processed_documents": processed_cohort,
            "selected_processed_documents": processed,
            "processing_coverage": processed_cohort / raw if raw else None,
            "coverage_scope": "Source/date/platform cohort before topic/content_kind filtering; optional model filter applies to processed records only",
            "unique_authors_approx": a["authors"]["value"], "unique_threads_approx": a["threads"]["value"],
            "unassigned_topics": a["missing_topic"]["doc_count"],
            "missing_parent_context": a["missing_context"]["doc_count"],
            "models": [{"name": b["key"], "count": b["doc_count"]} for b in a["models"]["buckets"]],
            "latest_fetched_at": a["latest_fetched"].get("value_as_string"),
            "latest_processed_at": a["latest_processed"].get("value_as_string"),
            "interpretation": "Observed sample; topic groups overlap; counts do not estimate population opinion."}
