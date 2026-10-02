"""Real Elasticsearch/Redis and real HTTP end-to-end tests (no mocked backend)."""
import json
import threading
from pathlib import Path
from unittest.mock import Mock
import pytest
import requests
from werkzeug.serving import make_server

from backend.common.es import bulk_documents, create_documents
from backend.common.settings import index_name
from backend.ingestion.import_ndjson import run as import_file
from backend.ingestion.queue import process_message
from backend.data_process.sentiment import SentimentModel
from backend.data_process.cloud_sentiment.cloud_sentiment_pipeline import process_targets
from backend.api.app import create_app

pytestmark = pytest.mark.integration


def raw(identifier="a", **overrides):
    return {"doc_id": identifier, "post_id": identifier, "platform": "bluesky",
            "created_at": "2026-09-01T08:00:00Z", "fetched_at": "2026-09-01T09:00:00Z",
            "raw_text": "Petrol prices are horrible!", "candidate_topics": ["fuel_price"],
            "direct_candidate_topics": ["fuel_price"], "thread_root_id": identifier,
            "dataset_kind": "synthetic", **overrides}


def test_bulk_replay_is_noop_and_mapping_failure_reported(es_service):
    es = es_service
    first = bulk_documents("social_discussion_posts_raw", [raw()], client=es)
    second = bulk_documents("social_discussion_posts_raw", [raw()], client=es)
    assert first.created == 1 and second.noop == 1
    es.indices.refresh(index=index_name("social_discussion_posts_raw"))
    assert es.count(index=index_name("social_discussion_posts_raw"))["count"] == 1


def test_overlapping_batches_preserve_first_source_and_replay_adds_nothing(es_service):
    es = es_service
    assert create_documents("social_discussion_posts_raw", [raw(source_dataset="first")], client=es).created == 1
    docs = [raw(source_dataset="second", raw_text="changed"), raw("b", source_dataset="second")]
    counts = create_documents("social_discussion_posts_raw", docs, client=es)
    assert counts.created == 1 and counts.already_present == 1 and counts.failed == 0
    replay = create_documents("social_discussion_posts_raw", docs, client=es)
    assert replay.created == 0 and replay.already_present == 2
    stored = es.get(index=index_name("social_discussion_posts_raw"), id="a")["_source"]
    assert stored["source_dataset"] == "first" and stored["raw_text"] == raw()["raw_text"]


def test_import_resume_and_fingerprint_protection(es_service, tmp_path):
    path = tmp_path/"sample.ndjson"
    path.write_text("\n".join(json.dumps(raw(str(i))) for i in range(5))+"\n{bad\n", encoding="utf-8")
    checkpoint = tmp_path/"checkpoint.json"
    first = import_file(path, source_dataset="sample", checkpoint=checkpoint,
                        max_records=2, batch_size=2, dataset_kind="synthetic", es=es_service)
    assert first["writes_this_run"]["created"] == 2
    second = import_file(path, source_dataset="sample", checkpoint=checkpoint,
                         batch_size=2, dataset_kind="synthetic", es=es_service)
    assert second["writes_this_run"]["created"] == 3
    assert second["rejected"] == 1
    third = import_file(path, source_dataset="sample", checkpoint=checkpoint,
                        dataset_kind="synthetic", es=es_service)
    assert third["accepted_this_run"] == 0
    es_service.indices.refresh(index=index_name("social_discussion_posts_raw"))
    assert es_service.count(index=index_name("social_discussion_posts_raw"))["count"] == 5
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="different file"):
        import_file(path, source_dataset="sample", checkpoint=checkpoint,
                    dataset_kind="synthetic", es=es_service)


def test_import_failed_bulk_does_not_checkpoint(tmp_path):
    path = tmp_path/"input.ndjson"
    path.write_text(json.dumps(raw())+"\n", encoding="utf-8")
    es = Mock()
    es.bulk.side_effect = RuntimeError("unavailable")
    checkpoint = tmp_path/"checkpoint.json"
    with pytest.raises(RuntimeError):
        import_file(path, source_dataset="sample", checkpoint=checkpoint, es=es)
    assert not checkpoint.exists()


def test_redis_reclaims_crash_before_ack_and_sink_is_idempotent(es_service, redis_queue):
    es, queue = es_service, redis_queue
    bulk_documents("social_discussion_posts_raw", [raw()], client=es)
    es.indices.refresh(index=index_name("social_discussion_posts_raw"))
    first_id = queue.enqueue(["a"])
    assert first_id
    assert not queue.enqueue(["a"])
    first = queue.read("crashed", idle_ms=0, block_ms=1)
    model = SentimentModel(use_transformers=False)
    process_message(es, model, first[1])
    # Simulate worker death AFTER successful ES write but BEFORE ACK.
    redelivered = queue.read("replacement", idle_ms=0, block_ms=1)
    assert redelivered[0] == first_id
    process_message(es, model, redelivered[1])
    queue.acknowledge(first_id)
    es.indices.refresh(index=index_name("social_posts_processed"))
    assert es.count(index=index_name("social_posts_processed"))["count"] == 1
    assert queue.stats()["pending"] == 0


def test_poison_message_dead_letter_and_replay(redis_queue):
    queue = redis_queue
    identifier = queue.enqueue(["missing"])
    fields = queue.read("worker", idle_ms=0, block_ms=1)[1]
    for attempt in range(1, 4):
        assert queue.failed(identifier, fields, "MissingDocument", max_attempts=3) == attempt
    assert queue.stats()["dead_letters"] == 1
    assert queue.stats()["pending"] == 0
    assert queue.replay_dead() == 1
    assert queue.stats()["dead_letters"] == 0
    assert queue.read("replacement", idle_ms=0, block_ms=1)


@pytest.mark.e2e
def test_raw_thread_to_processing_to_http_analytics(es_service):
    es = es_service
    root = raw()
    child = raw("b", raw_text="exactly", candidate_topics=[], direct_candidate_topics=[],
                parent_post_id="a", thread_root_id="a", is_reply=True)
    bulk_documents("social_discussion_posts_raw", [root, child], client=es)
    es.indices.refresh(index=index_name("social_discussion_posts_raw"))
    # Only the child is a target: parent context must be fetched from real ES.
    model = SentimentModel(use_transformers=False)
    children = process_targets([child], es, model)
    assert len(children) == 1 and children[0]["topic_source"] == "inherited"
    rows = process_targets([root, child], es, model)
    bulk_documents("social_posts_processed", rows, client=es)
    es.indices.refresh(index=index_name("social_posts_processed"))
    server = make_server("127.0.0.1", 0, create_app())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    params = {"from": "2026-09-01", "to": "2026-09-01", "dataset_kind": "synthetic"}
    try:
        response = requests.get(base+"/api/v1/social/sentiment", params=params, timeout=5)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data[0]["negative"] == 2
        assert data[0]["net_sentiment"] == -1
        assert data[0]["doc_count"] == 2
        assert requests.get(base+"/api/v1/social/sentiment", params={"from": "2026-09-01", "to": "2026-09-01"},
                            timeout=5).json()["data"] == []
        profile = requests.get(base+"/api/v1/platforms/profiles", params=params, timeout=5).json()["data"]
        assert profile["bluesky"]["fuel_price"]["net_sentiment"] == -1
        quality = requests.get(base+"/api/v1/quality", params=params, timeout=5).json()["data"]
        assert quality["processing_coverage"] == 1
        assert quality["models"][0]["name"] == "vaderSentiment"
        selected_quality = requests.get(base+"/api/v1/quality", params={**params,
            "model": "vaderSentiment", "topic": "fuel_price"}, timeout=5).json()["data"]
        assert selected_quality["raw_documents"] == 2
        assert selected_quality["processing_coverage"] == 1
        assert selected_quality["selected_processed_documents"] == 2
        assert requests.get(base+"/api/v1/oil/prices", params={**params, "model": "vaderSentiment"}, timeout=5).status_code == 400
        assert requests.get(base+"/api/v1/analyses/oil-sentiment", params={"from": "2026-09-01"}, timeout=5).status_code == 400
        first = requests.get(base+"/api/v1/social/posts", params={**params, "limit": 1}, timeout=5).json()["data"]
        second = requests.get(base+"/api/v1/social/posts",
            params={**params, "limit": 1, "cursor": first["next_cursor"]}, timeout=5).json()["data"]
        assert first["items"][0]["doc_id"] != second["items"][0]["doc_id"]
        legacy = requests.get(base+"/api/social-daily-sentiment", params=params, timeout=5)
        assert legacy.headers["Deprecation"] == "true"
        assert legacy.json()["data"] == data
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_news_processing_provenance_and_failure_audit(es_service):
    from backend.data_process.cloud_sentiment.news_sentiment_pipeline import parse_args, run_pipeline
    es = es_service
    article = {"id": "news-test", "title": "Excellent electric buses", "text": "A wonderful service!",
               "time": "2026-09-01T08:00:00Z", "fetched_at": "2026-09-20T08:00:00Z",
               "dataset_kind": "synthetic", "source_dataset": "test-corpus"}
    bulk_documents("gdelt_news_raw", [article], "id", client=es)
    es.indices.refresh(index=index_name("gdelt_news_raw"))
    args = parse_args(["--start-date", "2026-09-20", "--end-date", "2026-09-21", "--no-transformers"])
    result = run_pipeline(args, es=es, model=SentimentModel(use_transformers=False))
    assert result["processed_rows"] == 1  # Event time is older than the ingestion window.
    processed = es.get(index=index_name("news_processed"), id="news-test")["_source"]
    assert processed["source_dataset"] == "test-corpus"
    assert processed["input_scope"] == "full_text_lexicon"
    assert processed["sentiment_polarity"] > 0
    failed_model = Mock()
    failed_model.pipe = None
    failed_model.classify_many.side_effect = RuntimeError("model unavailable")
    with pytest.raises(RuntimeError, match="model unavailable"):
        run_pipeline(args, es=es, model=failed_model)
    es.indices.refresh(index=index_name("pipeline_runs"))
    assert es.count(index=index_name("pipeline_runs"), query={"term": {"status": "failed"}})["count"] == 1


def test_freshness_excludes_synthetic_and_uses_arrival_time(es_service):
    from datetime import datetime, timezone
    from backend.health_check.health_check import check
    es = es_service
    rows = [raw("real", dataset_kind="live", fetched_at="2026-09-20T08:00:00Z"),
            raw("fixture", fetched_at="2026-09-25T08:00:00Z")]
    bulk_documents("social_discussion_posts_raw", rows, client=es)
    es.indices.refresh(index=index_name("social_discussion_posts_raw"))
    state = check(es, "social_discussion_posts_raw", "fetched_at", 48,
                  now=datetime(2026, 9, 22, 9, tzinfo=timezone.utc))
    assert state["total"] == 1 and state["age_hours"] == 49
    assert state["stale"] is True


def test_correlation_persists_calendar_pairs_and_separates_demo(es_service):
    from datetime import date, timedelta
    from backend.oil_sentiment_corr.compute import run
    from backend.api.queries import correlation
    es = es_service
    oil, social = [], []
    for i in range(20):
        day = (date(2026, 9, 1) + timedelta(days=i)).isoformat()
        change = 1 if i % 2 else -1
        oil.append({"id": day, "date": day, "ticker": "BRENT", "price": 70+i,
                    "daily_return_pct": change, "dataset_kind": "synthetic"})
        social.append(raw(str(i), created_at=day+"T08:00:00Z", schema_version=2,
            contextual_sentiment_label="positive" if change > 0 else "negative", model_name="test-model"))
    bulk_documents("oil_prices_raw", oil, "id", client=es)
    bulk_documents("social_posts_processed", social, client=es)
    es.indices.refresh(index=[index_name("oil_prices_raw"), index_name("social_posts_processed")])
    result = run("2026-09-01", "2026-09-20", es, dataset_kind="synthetic", model_name="test-model")
    assert result["n_days_used"] == 20 and result["correlation_pearson"] == 1
    es.indices.refresh(index=index_name("oil_sentiment_corr_results"))
    assert correlation(es, "2026-09-01", "2026-09-20") is None
    stored = correlation(es, "2026-09-01", "2026-09-20", {"dataset_kind": "synthetic", "model": "test-model"})
    assert len(stored["best_lag_scatter_data"]) >= 14
    assert correlation(es, "2026-09-01", "2026-09-19", {"dataset_kind": "synthetic"}) is None
