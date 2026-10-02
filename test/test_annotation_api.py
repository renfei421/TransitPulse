"""Real storage checks for independent LLM annotations and global date boundaries."""
import pytest

from backend.api.app import create_app
from backend.common.es import bulk_documents, create_documents
from backend.common.settings import index_name


@pytest.mark.integration
def test_annotations_have_separate_measurement_and_replay_identity(es_service):
    es = es_service
    common = {"platform": "mastodon", "created_at": "2026-09-01T16:30:00Z", "dataset_kind": "synthetic",
              "source_dataset": "annotation-api-test", "raw_text": "Fuel is costly but I love my bus."}
    target = {"target": "public_transport", "sentiment": "positive", "evidence": "I love my bus"}
    annotation = {**common, "doc_id": "annotation-a", "source_doc_id": "post-a", "schema_version": 1,
                  "model_name": "llm-test", "relevance": "related", "content_type": "opinion",
                  "overall_tone": "mixed", "targets": [target], "needs_review": False}
    assert create_documents("social_posts_annotations", [annotation], client=es).created == 1
    assert create_documents("social_posts_annotations", [{**annotation, "overall_tone": "negative"}], client=es).already_present == 1
    raw = {**common, "doc_id": "post-a"}
    bulk_documents("social_discussion_posts_raw", [raw], client=es)
    bulk_documents("social_posts_processed", [{**raw, "schema_version": 2, "model_name": "classifier-test",
        "candidate_topics": ["oil_vehicle"], "contextual_sentiment_label": "negative",
        "contextual_sentiment_polarity": -.5, "content_kind": "commentary_candidate"}], client=es)
    for name in ("social_posts_annotations", "social_posts_processed", "social_discussion_posts_raw"):
        es.indices.refresh(index=index_name(name))
    client = create_app().test_client()
    params = {"from": "2026-09-01", "to": "2026-09-01", "dataset_kind": "synthetic", "timezone": "UTC"}
    found = client.get("/api/v1/social/annotations", query_string={**params, "relevance": "related", "content_type": "opinion"})
    assert found.status_code == 200
    body = found.get_json()
    assert body["meta"]["timezone"] == "UTC"
    row = body["data"]["items"][0]
    assert row["overall_tone"] == "mixed" and row["targets"] == [target]
    assert row["source_doc_id"] == "post-a" and "contextual_sentiment_score" not in row
    # The same instant is the following day in Sydney; no annotations on Sep 1.
    assert client.get("/api/v1/social/annotations", query_string={**params, "timezone": "Australia/Sydney"}).get_json()["data"]["items"] == []
    assert client.get("/api/v1/social/annotations", query_string={**params, "relevance": "unrelated"}).get_json()["data"]["items"] == []
    labels = client.get("/api/v1/social/sentiment", query_string={**params, "topic": "oil_vehicle"}).get_json()["data"]
    assert labels[0]["negative"] == 1 and labels[0]["doc_count"] == 1
    quality = client.get("/api/v1/quality", query_string={**params, "content_kind": "link_post"}).get_json()["data"]
    assert quality["processing_coverage"] == 1 and quality["selected_processed_documents"] == 0
    assert quality["models"] == []
    for extra in ({"timezone": "bad"}, {"content_type": "bad"}, {"topic": "ev"}):
        assert client.get("/api/v1/social/annotations", query_string={**params, **extra}).status_code == 400
