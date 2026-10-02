"""Regression cases motivated by the instructor feedback and data audit."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import requests

from backend.common.es import bulk_documents, BulkWriteError
from backend.common.time import day_bounds
from backend.common.http import get, retry_after
from backend.common.logging import redact
from backend.api.app import create_app
from backend.api.queries import summary, filters
from backend.data_process.sentiment import SentimentModel, detect_reply_relation, annotate_thread, build_context
from backend.data_process.threads import build_threads_from_post_nodes
from backend.news_harvester.volume import extract_volume_rows
from backend.news_harvester.discovery import discover
from backend.oil_sentiment_corr.compute import aligned_pairs, compute_stats
from backend.brent_ingest.fetch_brent import build_docs
from backend.common.jobs import job_manifest
from backend.parallel_harvester.topics import match_topics
from backend.ingestion.import_ndjson import normalize_record


@pytest.mark.parametrize("outcomes, expected", [
    (["created", "updated", "noop"], (1, 1, 1)),
    (["noop", "noop"], (0, 0, 2)),
])
def test_bulk_counts_actual_outcomes(outcomes, expected):
    es = Mock()
    es.bulk.return_value = {"items": [{"update": {"status": 200, "result": r}} for r in outcomes]}
    result = bulk_documents("x", [{"doc_id": str(n)} for n in range(len(outcomes))], client=es)
    assert (result.created, result.updated, result.noop) == expected


def test_bulk_http_200_item_failure_is_failure():
    es = Mock()
    es.bulk.return_value = {"items": [{"update": {"status": 201, "result": "created"}},
                                    {"update": {"status": 400, "error": {"type": "mapping"}}}]}
    with pytest.raises(BulkWriteError) as error:
        bulk_documents("x", [{"doc_id": "a"}, {"doc_id": "b"}], client=es)
    assert error.value.counts.created == 1
    assert error.value.counts.failed == 1


@pytest.mark.parametrize("day, hours", [("2026-10-04", 23), ("2026-04-05", 25), ("2026-02-01", 24)])
def test_sydney_day_boundaries_handle_dst(day, hours):
    a, b = map(datetime.fromisoformat, day_bounds(day, day))
    assert (b-a).total_seconds() == hours*3600
    assert filters(day, day)[0]["range"]["created_at"] == {"gte": a.isoformat(), "lt": b.isoformat()}


@pytest.mark.parametrize("text", ["I do not agree", "not true at all", "I don't agree", "wrong", "no"])
def test_negation_is_not_agreement(text):
    assert detect_reply_relation(text)[0] == "disagreement"


@pytest.mark.parametrize("text", ["agree", "exactly", "same here", "同意"])
def test_clear_agreement(text):
    assert detect_reply_relation(text)[0] == "agreement"


def test_confidence_is_not_polarity():
    model = SentimentModel(use_transformers=False)
    result = model.classify("This is horrible and terrible!")
    assert result["confidence"] >= 0
    assert result["polarity"] < 0
    assert result["label"] == "negative"
    assert model.classify("")["label"] == "neutral"


def test_inherited_topic_and_agreement_survive_flattening():
    model = SentimentModel(use_transformers=False)
    tree = {"post_id": "a", "raw_text": "Petrol prices are terrible.", "candidate_topics": ["fuel_price"],
            "children": [{"post_id": "b", "raw_text": "exactly", "candidate_topics": [], "children": []}]}
    _, rows = annotate_thread(tree, model)
    assert rows[1]["candidate_topics"] == ["fuel_price"]
    assert rows[1]["topic_source"] == "inherited"
    assert rows[1]["contextual_sentiment_label"] == rows[0]["contextual_sentiment_label"]
    assert rows[1]["sentiment_method"] == "agreement_inherit_parent"
    assert tree["children"][0]["candidate_topics"] == []  # no input mutation


def test_disagreement_is_not_mechanical_sign_flip():
    model = SentimentModel(use_transformers=False)
    _, rows = annotate_thread({"post_id": "a", "raw_text": "horrible", "children": [
        {"post_id": "b", "raw_text": "wrong", "children": []}]}, model)
    assert rows[1]["sentiment_method"] == "context_classification"


def test_target_text_survives_long_context():
    text = build_context("a"*10000, "b"*10000, "c"*10000, "TARGET")
    assert text.endswith("[REPLY]\nTARGET")
    assert len(text) < 1700


def test_isolated_cycle_rejected():
    with pytest.raises(ValueError, match="Cycle"):
        build_threads_from_post_nodes({"a": {"post_id": "a", "parent_post_id": "b"},
                                       "b": {"post_id": "b", "parent_post_id": "a"}})


def test_missing_parent_flag():
    roots = build_threads_from_post_nodes({"b": {"post_id": "b", "parent_post_id": "missing"}})
    assert roots[0]["context_missing_parent"] is True


def test_keyword_boundaries():
    assert "ev" not in match_topics("Never believe every clever idea.")[0]
    assert "ev" in match_topics("I bought an EV.")[0]


def test_volume_zero_and_intraday_buckets():
    payload = {"timeline": [{"data": [
        {"date": "20260901000000", "value": 0, "norm": 10},
        {"date": "20260901120000", "value": 2, "norm": 10},
        {"date": "20260902000000", "value": 0, "norm": 0}]}]}
    rows = extract_volume_rows(payload, "test")
    assert len(rows) == 2
    assert rows[0]["volume"] == 2
    assert rows[0]["share_percent"] == 10
    assert rows[1]["volume"] == 0
    assert rows[1]["share_percent"] is None


def test_gdelt_saturated_day_is_split_with_budget():
    full = [{"url": f"https://example.org/{i}"} for i in range(250)]
    calls = []
    def fetch(params):
        calls.append(params)
        return {"articles": full if len(calls) == 1 else [{"url": "https://example.org/extra"}]}
    candidates, audit = discover("test", "2026-09-01", request_budget=2, fetch=fetch, sleep=lambda _: None)
    assert len(candidates) == 251
    assert audit["unvisited_windows"] == 1
    assert audit["request_windows"] == 2


def test_calendar_lag_does_not_compress_missing_days():
    oil = {"2026-01-01": 1, "2026-01-03": 2}
    sentiment = {"2026-01-03": 0.5, "2026-01-04": -0.5}
    assert aligned_pairs(oil, sentiment, 1) == [("2026-01-03", "2026-01-04", 2.0, -0.5)]


def test_constant_or_short_series_never_emit_nan():
    values = {f"2026-01-{i:02d}": 1 for i in range(1, 20)}
    result = compute_stats(values, values)
    assert result["correlation_pearson"] is None
    assert result["best_lag_days"] is None
    json.dumps(result, allow_nan=False)
    assert compute_stats({"2026-01-01": 1}, {"2026-01-01": 2}) is None


def test_weekend_brent_return_uses_previous_observation():
    docs = build_docs([{"period": "2026-09-04", "value": "100"},
                       {"period": "2026-09-07", "value": "105"}], datetime(2026, 9, 7).date())
    assert docs[0]["daily_return_pct"] == 5
    assert docs[0]["dataset_kind"] == "live"
    sparse = build_docs([{"period": "2026-09-07", "value": "105"}], datetime(2026, 9, 7).date())
    assert "daily_return_pct" not in sparse[0]


def test_http_retry_respects_retry_after_and_stops():
    rate = requests.Response()
    rate.status_code = 429
    rate.headers["Retry-After"] = "2"
    ok = requests.Response()
    ok.status_code = 200
    session, sleep = Mock(), Mock()
    session.get.side_effect = [rate, ok]
    assert get("https://example.org", session=session, sleep=sleep) is ok
    sleep.assert_called_once_with(2.0)
    assert retry_after("99999", 1) == 120


def test_http_auth_failure_does_not_retry():
    response = requests.Response()
    response.status_code = 401
    session = Mock()
    session.get.return_value = response
    with pytest.raises(requests.HTTPError):
        get("https://example.org", session=session, sleep=lambda _: None)
    assert session.get.call_count == 1


def test_secret_redaction():
    result = redact({"password": "unsafe", "nested": {"api_key": "unsafe"},
                     "message": "https://example.org/?token=unsafe&n=1"})
    assert "unsafe" not in json.dumps(result)


def test_job_manifest_secret_refs_budgets_and_unique_names(monkeypatch):
    monkeypatch.setenv("SENTIMENT_JOB_IMAGE", "registry.example/worker:abc123")
    first, second = job_manifest("social"), job_manifest("social")
    assert first["metadata"]["name"] != second["metadata"]["name"]
    spec = first["spec"]
    assert spec["activeDeadlineSeconds"] == 3600
    container = spec["template"]["spec"]["containers"][0]
    assert {"secretRef": {"name": "transport-secrets"}} in container["envFrom"]
    assert "--time-field" in container["args"]
    with pytest.raises(ValueError):
        job_manifest("social", {"es_password": "unsafe"})


@pytest.mark.parametrize("path, status", [
    ("/api/v1", 200), ("/api/v1/unknown", 404),
    ("/api/v1/social/sentiment?from=invalid", 400),
    ("/api/v1/social/sentiment?from=2026-09-02&to=2026-09-01", 400),
    ("/api/v1/social/sentiment?dataset_kind=unknown", 400),
])
def test_api_validation_without_database(path, status):
    result = create_app().test_client().get(path)
    assert result.status_code == status
    assert result.headers["X-Request-ID"]


def test_api_method_status():
    assert create_app().test_client().post("/api/v1/social/volume").status_code == 405


def test_api_failure_does_not_expose_credentials(monkeypatch):
    monkeypatch.setattr("backend.api.app.get_client", Mock(side_effect=RuntimeError("password=unsafe")))
    response = create_app().test_client().get("/api/v1/health")
    assert response.status_code == 503
    assert b"unsafe" not in response.data


def test_label_balance_separate_from_model_polarity():
    bucket = {"doc_count": 4, "labels": {"buckets": {label: {"doc_count": count}
        for label, count in [("positive", 1), ("neutral", 1), ("negative", 2)]}},
        "polarity": {"value": -0.9}, "threads": {"value": 2}}
    assert summary(bucket)["net_sentiment"] == -0.25


def test_import_invalid_date_rejected():
    with pytest.raises(ValueError):
        normalize_record({"platform": "bluesky", "post_id": "a", "raw_text": "EV", "created_at": "bad"},
                         source_dataset="test")


def test_mastodon_instances_cannot_collide():
    row = {"id": "1", "content": "<p>EV</p>", "account": {}, "created_at": "2026-09-01T00:00:00Z"}
    a = normalize_record(row, source_dataset="test", server_domain="a.example")
    b = normalize_record(row, source_dataset="test", server_domain="b.example")
    assert a["doc_id"] != b["doc_id"]
