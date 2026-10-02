"""Explicit target-attitude resources and durable experiment status."""
from elasticsearch import NotFoundError
from backend.api.queries import filters
from backend.common.settings import index_name


def target_daily(es, start, end, options=None):
    options = options or {}
    if options.get("model") and not options["model"].startswith("jev-"):
        raise ValueError("Target sentiment requires a Jev model")
    parent_options = {key: value for key, value in options.items() if key != "topic"}
    accepted = [{"term": {"target_sentiments.accepted": True}}]
    if options.get("topic"):
        accepted.append({"term": {"target_sentiments.target": options["topic"]}})
    # Filtering inside the nested aggregation prevents another target's score
    # from leaking into the requested target's statistics.
    aggs = {"days": {"date_histogram": {"field":"created_at","calendar_interval":"day",
            "time_zone":options.get("timezone","Australia/Sydney"),"min_doc_count":1},
        "aggs": {"targets": {"nested":{"path":"target_sentiments"},"aggs": {
            "accepted":{"filter":{"bool":{"filter":accepted}},"aggs": {
                "topics":{"terms":{"field":"target_sentiments.target","size":4},"aggs": {
                    "polarity":{"avg":{"field":"target_sentiments.score"}},
                    "labels":{"terms":{"field":"target_sentiments.label","size":3}}
                }}}}}}}}}
    response = es.search(index=index_name("social_posts_jev"), size=0,
        query={"bool": {"filter": filters(start,end,processed=True,options=parent_options)}}, aggs=aggs)
    rows=[]
    for day in response["aggregations"]["days"]["buckets"]:
        for topic in day["targets"]["accepted"]["topics"]["buckets"]:
            rows.append({"date":day["key_as_string"][:10],"topic":topic["key"],
                "accepted_count":topic["doc_count"],"processed_posts_on_day":day["doc_count"],
                "mean_target_polarity":topic["polarity"]["value"],
                "labels":{item["key"]:item["doc_count"] for item in topic["labels"]["buckets"]},
                "sentiment_scope":"poster_target_attitude"})
    return rows


def experiment_status(es, experiment_id):
    try:
        return es.get(index=index_name("experiment_runs"),id=experiment_id)["_source"]
    except NotFoundError:
        return None
