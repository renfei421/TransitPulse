from backend.parallel_harvester.normalize import normalise_mastodon_status
from backend.parallel_harvester.topics import (
    TOPIC_KEYWORDS, generate_queries, infer_region, match_topics, public_hashtag_seeds, region_evidence,
)
from scripts.audit_search_reuse import deduplicate, select_review, record_review
from backend.parallel_harvester.topics import analysis_language_route, country_text_hints
from backend.common.es import create_documents, BulkWriteError
from unittest.mock import Mock
import pytest


def test_compact_hashtags_reuse_keywords_without_substring_false_positives():
    assert match_topics("plain electricvehicles prose")[0] == []
    assert match_topics("Never believe every idea.", ["clever"])[0] == []
    topics, words = match_topics("An update", ["ElectricVehicles", "PublicTransport", "FuelPrices"])
    assert set(topics) == {"ev", "public_transport", "fuel_price"}
    assert "electric vehicles" in words


def test_mastodon_normalizer_reads_structured_hashtags():
    doc = normalise_mastodon_status({"id": "1", "content": "<p>#<span>ElectricVehicles</span></p>",
        "tags": [{"name": "ElectricVehicles"}], "created_at": "2026-09-30T00:00:00Z"}, server_domain="a.example")
    assert "ev" in doc["candidate_topics"]


def test_ambiguous_geography_is_retained_as_evidence_not_residence():
    text = "We must act now; the Victoria train is late."
    assert region_evidence(text)["kind"] == "ambiguous_text_hint"
    assert infer_region(text)["inferred_region"] is None
    assert region_evidence("EVs are useful")["kind"] == "unknown"
    assert infer_region("Please act now in Sydney")["inferred_region"] == "New_South_Wales_or_Sydney"


def test_adapter_uses_existing_terms_and_keeps_query_bank():
    assert len(generate_queries()) == 58
    assert all(s["keyword"] in TOPIC_KEYWORDS[s["topic"]] for s in public_hashtag_seeds())


def test_federated_uri_deduplicates_without_colliding_local_numbers():
    seed = {"hashtag": "ev"}
    records = [{"host": h, "seed": seed, "post": {"id": "1", "uri": u}}
               for h, u in [("a.example", "https://origin.example/1"),
                            ("b.example", "https://origin.example/1"),
                            ("a.example", "https://another.example/1")]]
    records += [{"host": "c.example", "seed": seed, "post": {"id": "1"}}]
    unique, invalid = deduplicate(records)
    assert len(unique) == 2 and invalid == 1
    assert len(unique[0]["retrieved_via"]) == 2


def test_review_selection_is_stable_and_includes_nonmatches():
    rows = [{"doc": {"doc_id": str(i)}, "review_stratum": group}
            for group in ("no_topic_match", "other_topic_candidate") for i in (3, 2, 1)]
    selected = select_review(rows, per_stratum=2)
    assert selected == select_review(list(reversed(rows)), per_stratum=2)
    assert len(selected) == 4
    assert any(r["review_stratum"] == "no_topic_match" for r in selected)


def test_reviews_cannot_be_attached_to_a_changed_sample(tmp_path):
    import json
    import pytest
    (tmp_path/"review-sample.json").write_text('[]')
    (tmp_path/"review-labels.json").write_text(json.dumps({"sample_sha256": "stale-snapshot"}))
    with pytest.raises(ValueError, match="different sample snapshot"):
        record_review(tmp_path)


def test_global_profile_preserves_legacy_and_excludes_local_products_from_main_queries():
    assert len(generate_queries()) == 58
    queries = generate_queries("global_en")
    assert len(queries) == len(set(q.lower() for q in queries))
    assert "fuel price" in queries and "public transit" in queries and "gas prices" in queries
    assert all(not q.endswith(("Australia", "Melbourne", "Victoria")) for q in queries)
    assert "myki" not in queries and "PTV" not in queries and "free PT" not in queries
    assert "myki Melbourne" in generate_queries("regional_supplement")
    assert match_topics("Mass transit", profile="legacy_au")[0] == []
    assert match_topics("Mass transit", profile="global_en")[0] == ["public_transport"]
    for seed in public_hashtag_seeds("global_en"):
        assert seed["topic"] in match_topics("update", [seed["hashtag"]], profile="global_en")[0]
    with pytest.raises(ValueError):
        generate_queries("glboal")


def test_language_and_geography_are_explicit_routing_hints():
    assert analysis_language_route("en-GB") == "english_candidate"
    assert analysis_language_route("de") == "other_language"
    assert analysis_language_route(None) == "needs_language_review"
    assert country_text_hints("Let us drive in a bus.") == []
    assert country_text_hints("Fuel prices in Canada compared with the United States") == ["CA", "US"]
    assert country_text_hints("Melbourne") == []  # This limited lexicon does not geocode cities.


def test_create_bulk_only_accepts_real_version_conflicts_and_reports_partial_failure():
    client = Mock()
    client.bulk.return_value = {"items": [
        {"create": {"status": 201, "result": "created"}},
        {"create": {"status": 409, "error": {"type": "version_conflict_engine_exception"}}},
        {"create": {"status": 400, "error": {"type": "mapper_parsing_exception"}}},
        {"create": {"status": 409, "error": {"type": "unexpected_error"}}},
    ]}
    with pytest.raises(BulkWriteError) as failure:
        create_documents("social_discussion_posts_raw", [{"doc_id": str(i)} for i in range(4)], client=client)
    assert failure.value.counts.as_dict() == {"attempted": 4, "created": 1, "already_present": 1, "failed": 2}
    assert all("create" in op for op in client.bulk.call_args.kwargs["operations"][::2])
    client.bulk.return_value = {"items": []}
    with pytest.raises(RuntimeError, match="incomplete"):
        create_documents("social_discussion_posts_raw", [{"doc_id": "a"}], client=client)


def test_global_capture_keeps_foreign_languages_and_does_not_inherit_australian_events(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone
    from scripts import audit_search_reuse as pilot
    now = datetime.now(timezone.utc).isoformat()
    posts = [
        {"id": "1", "uri": "https://origin.example/1", "created_at": now, "language": "fr",
         "content": "Canada", "tags": [{"name": "PublicTransit"}]},
        {"id": "2", "uri": "https://origin.example/2", "created_at": now,
         "content": "A personal diary", "tags": []},
    ]
    monkeypatch.setattr(pilot, "sample_instance", lambda host, seeds, pages:
                        ([{"host": host, "seed": seeds[0], "post": p} for p in posts], []))
    output = tmp_path/"global"
    pilot.collect(output, profile="global_en", per_stratum=1)
    rows = json.loads((output/"records.json").read_text())
    assert len(rows) == 2  # Three instances coalesce by canonical URI.
    doc = next(r["doc"] for r in rows if r["doc"]["post_id"].endswith("/1"))
    assert doc["candidate_topics"] == ["public_transport"]
    assert doc["analysis_language_route"] == "other_language"
    assert doc["country_text_hints"] == ["CA"]
    assert doc["inferred_region"] is None
    assert all(doc[k] is None for k in ("nearest_event_id", "nearest_event_name", "days_from_event", "event_period"))
    report = json.loads((output/"report.json").read_text())
    assert report["funnel"]["returned_records"] == 6 and report["funnel"]["topic_candidates_with_hashtags"] == 1
    assert report["review_sample"]["strata"]["no_topic_match"] == 1
