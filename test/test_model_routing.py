from unittest.mock import Mock

import pytest

from backend.data_process.eligibility import assess, content_kind, model_text
from scripts.cloud_model_job import manifest


def test_routing_does_not_send_unknown_languages_or_tag_only_posts_to_model():
    detector = Mock()
    assert assess({"lang": None, "raw_text": "I love the buses in this city"}, detector)["reason"] == "provider_language_unknown"
    assert assess({"lang": "de", "raw_text": "Ein schöner Tag"}, detector)["reason"] == "provider_non_english"
    assert assess({"lang": "en", "raw_text": "# PublicTransport #Tram #EV http://example.com"}, detector)["reason"] == "insufficient_prose"
    detector.classify.assert_not_called()


def test_language_disagreement_and_weak_evidence_are_explicit():
    doc = {"lang": "en", "raw_text": "The bus service in this city is very useful."}
    detector = Mock()
    detector.classify.return_value = ("de", .99)
    assert assess(doc, detector)["reason"] == "language_disagreement"
    detector.classify.return_value = ("en", .6)
    assert assess(doc, detector)["reason"] == "language_uncertain"
    detector.classify.return_value = ("en", .95)
    assert assess(doc, detector)["decision"] == "process"
    assert content_kind("My bus was late. https://example.com") == "commentary_candidate"
    assert content_kind("New EV charger opened. https://example.com") == "link_post"
    assert model_text("Hi @alice see https:// example.com/path") == "Hi @user see http"


def test_acceptance_job_is_bounded_and_receives_no_credentials_during_dependency_install():
    job = manifest("test", "a"*64, ["pilot"], "2026-10-01", "2026-10-02", 2000)
    spec = job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 3600
    assert not spec["automountServiceAccountToken"]
    assert "envFrom" not in spec["initContainers"][0]
    assert "sha256sum -c" in spec["initContainers"][0]["command"][-1]
    worker = spec["containers"][0]
    assert worker["resources"]["limits"]["memory"] == "3Gi"
    assert {"name": "HF_HUB_OFFLINE", "value": "1"} in worker["env"]


@pytest.mark.integration
def test_global_processing_excludes_records_without_losing_audit_or_source(es_service):
    from backend.common.es import bulk_documents
    from backend.common.settings import index_name
    from backend.data_process.sentiment import SentimentModel
    from backend.data_process.cloud_sentiment.cloud_sentiment_pipeline import parse_args, run_pipeline
    text = "I love the buses in this city very much."
    docs = [{"doc_id": key, "post_id": key, "raw_text": value, "lang": lang,
             "created_at": "2026-09-01T08:00:00Z", "fetched_at": "2026-10-01T08:00:00Z",
             "dataset_kind": "synthetic", "source_dataset": "routing-test", "candidate_topics": ["public_transport"],
             "nearest_event_id": "australian-event"}
            for key, value, lang in [("a", text, "en"), ("b", text, "en"), ("c", text, "de"),
                                     ("d", "#tram", "en"), ("e", "None of these words describes an English paragraph.", "en")]]
    bulk_documents("social_discussion_posts_raw", docs, client=es_service)
    es_service.indices.refresh(index=index_name("social_discussion_posts_raw"))
    detector = Mock()
    detector.classify.side_effect = lambda t: ("fr", .99) if t.startswith("None") else ("en", .99)
    args = parse_args(["--dataset-kind", "synthetic", "--source-dataset", "routing-test", "--eligibility-policy", "global-en-v1",
                       "--local-only", "--max-documents", "5", "--thread-batch-size", "1"])
    result = run_pipeline(args, es=es_service, model=SentimentModel(use_transformers=False), language_detector=detector)
    assert result["input_records"] == 5 and result["processed_rows"] == 1 and result["filtered"] == 4
    assert result["details"]["filter_reasons"]["duplicate_model_text"] == 1
    row = es_service.get(index=index_name("social_posts_processed"), id="a")["_source"]
    assert row["raw_text"] == text and row["source_dataset"] == "routing-test"
    assert row["nearest_event_id"] is None and row["sentiment_scope"] == "whole_post_tone_not_target_stance"
    assert es_service.get(index=index_name("social_processing_decisions"), id="e")["_source"]["reason"] == "language_disagreement"
    args.max_documents = 4
    with pytest.raises(ValueError, match="budget"):
        run_pipeline(args, es=es_service, model=SentimentModel(use_transformers=False), language_detector=detector)
