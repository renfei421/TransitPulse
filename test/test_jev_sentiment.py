"""Protocol fixtures exercise contract/maths; none claim live Jev accuracy."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from backend.data_process.jev_sentiment import (
    MODEL, ENDPOINT, QUESTIONS, JevSentimentModel, JevError,
    validate_response, measurement, processed_document,
)


def fixture_response():
    answers = {}
    for name, question in QUESTIONS.items():
        if question["type"] == "score":
            answers[name] = {"type": "score", "score": 1.65, "confidence": .8,
                "probabilities": {"0": .1, "1": .15, "2": .75},
                "legend": {str(i): text for i, text in enumerate(question["criteria"])}}
        else:
            chosen = ("related" if name == "relevance" else "opinion" if name == "content_type" else
                      "single" if name == "tone_state" else "expressed")
            answers[name] = {"type": "choice", "choice": chosen, "confidence": 1.,
                "probabilities": {key: float(key == chosen) for key in question["criteria"]}}
    return {"model": MODEL, "answers": answers, "usage": {"input_tokens": 1000, "output_tokens": 100}}


def set_choice(body, name, choice):
    body["answers"][name].update(choice=choice, probabilities={key: float(key == choice) for key in QUESTIONS[name]["criteria"]})


def http(monkeypatch, body=None):
    post = Mock()
    post.return_value.status_code = 200
    post.return_value.json.return_value = body or fixture_response()
    monkeypatch.setattr("backend.data_process.jev_sentiment.requests.post", post)
    return post


def test_native_score_normalizes_probability_direction_and_keeps_confidence_distinct():
    body = validate_response(fixture_response())
    value = measurement(body["answers"]["tone"], body["answers"]["tone_state"], threshold=.6, eligible_state="single")
    assert value["polarity"] == pytest.approx(.65)
    assert value["ordinal_score"] == 1.65 and value["label"] == "positive"
    assert value["confidence"] == .8 and value["polarity"] != value["confidence"]


@pytest.mark.parametrize("change", ["probability_sum", "score", "model", "question", "nan", "boolean", "legend"])
def test_invalid_provider_data_is_not_silently_repaired(change):
    body = fixture_response()
    tone = body["answers"]["tone"]
    if change == "probability_sum": tone["probabilities"]["0"] = .2
    elif change == "score": tone["score"] = .65
    elif change == "model": body["model"] = "jev-latest"
    elif change == "question": del body["answers"]["relevance"]
    elif change == "nan": tone["confidence"] = float("nan")
    elif change == "boolean": tone["probabilities"]["0"] = True
    elif change == "legend": tone["legend"]["0"] = "positive"
    with pytest.raises(JevError):
        validate_response(body)


def test_mixed_and_uncertain_are_null_not_factual_neutral(monkeypatch, tmp_path):
    body = fixture_response()
    set_choice(body, "tone_state", "mixed")
    set_choice(body, "public_transport_state", "factual_only")
    http(monkeypatch, body)
    result = JevSentimentModel(api_key="fixture-only", cache_directory=tmp_path).classify("Fuel is expensive but the bus is wonderful.")
    assert result["label"] == "mixed" and result["polarity"] is None
    assert result["targets"]["public_transport"]["polarity"] is None
    assert result["targets"]["ev"]["polarity"] == pytest.approx(.65)
    # This tests separate gates, not whether the mock has selected the right EV target.


def test_live_hundredth_rounding_is_accepted_without_rewriting_provider_values():
    body = fixture_response()
    tone = body["answers"]["tone"]
    # Observed in a genuine API response: the independently rounded score is
    # 1.01 while the displayed probabilities imply 1.02.
    tone.update(score=1.01, probabilities={"0": 0., "1": .98, "2": .02})
    original = deepcopy(body)
    assert validate_response(body) == original
    result = measurement(tone, body["answers"]["tone_state"], threshold=.6, eligible_state="single")
    assert result["polarity"] == .02 and result["ordinal_score"] == 1.01
    assert result["score_expectation_delta"] == pytest.approx(-.01)


def test_rounded_probability_mass_is_retained_but_material_or_precise_errors_fail():
    body = fixture_response()
    tone = body["answers"]["tone"]
    tone.update(score=1.65, probabilities={"0": .09, "1": .15, "2": .75})
    original = deepcopy(body)
    assert validate_response(body) == original
    tone["score"] = 1.60
    with pytest.raises(JevError, match="expectation"):
        validate_response(body)
    tone.update(score=1.651, probabilities={"0": .1, "1": .15, "2": .75})
    with pytest.raises(JevError, match="expectation"):
        validate_response(body)
    tone.update(score=1.65, probabilities={"0": .08, "1": .15, "2": .75})
    with pytest.raises(JevError, match="sum"):
        validate_response(body)


def test_small_rounded_choice_disagreement_is_preserved_and_abstains():
    body=fixture_response()
    state=body['answers']['oil_vehicle_state']
    state.update(choice='expressed',confidence=.9,probabilities={
        'expressed':.40,'factual_only':.08,'mixed':0.,'insufficient':.11,'not_mentioned':.41000000000000003})
    original=deepcopy(body)
    assert validate_response(body)==original
    result=measurement(body['answers']['oil_vehicle_tone'],state,threshold=.6,eligible_state='expressed')
    assert result['polarity'] is None and not result['accepted']
    assert result['review_reasons']==['choice_probability_disagreement']
    assert result['state_choice_probability_gap']==pytest.approx(.01)
    state['probabilities'].update(expressed=.39,not_mentioned=.42)
    with pytest.raises(JevError,match='Choice'):
        validate_response(body)
    state['probabilities'].update(expressed=.404,not_mentioned=.406)
    with pytest.raises(JevError,match='Choice'):
        validate_response(body)


def test_bimodal_zero_is_not_confident_neutral():
    body = fixture_response()
    tone = body["answers"]["tone"]
    tone.update(score=1, confidence=0., probabilities={"0": .5, "1": 0., "2": .5})
    result = measurement(tone, body["answers"]["tone_state"], threshold=.6, eligible_state="single")
    assert result["unfiltered_polarity"] == 0 and result["polarity"] is None
    assert result["label"] == "unclassified"


def test_cache_reuses_model_decision_but_recomputes_threshold(monkeypatch, tmp_path):
    post = http(monkeypatch)
    a = JevSentimentModel(api_key="fixture-only", cache_directory=tmp_path, confidence_threshold=.6)
    assert a.classify("The bus service is wonderful.")["polarity"] == pytest.approx(.65)
    b = JevSentimentModel(api_key="fixture-only", cache_directory=tmp_path, confidence_threshold=.9)
    result = b.classify("The bus service is wonderful.")
    assert result["cache_hit"] and result["polarity"] is None and b.calls == 0
    assert post.call_count == 1
    assert post.call_args.args == (ENDPOINT,)
    assert post.call_args.kwargs["allow_redirects"] is False
    assert "fixture-only" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


def test_budget_and_missing_key_fail_without_http(monkeypatch, tmp_path):
    post = http(monkeypatch)
    monkeypatch.setattr("backend.data_process.jev_sentiment.secret", lambda _: "")
    with pytest.raises(JevError, match="TYPESAFE_API_KEY"):
        JevSentimentModel(cache_directory=tmp_path)
    model = JevSentimentModel(api_key="fixture-only", cache_directory=tmp_path, budget_usd=.0000001)
    with pytest.raises(JevError, match="budget"):
        model.classify("A bus service review.")
    post.assert_not_called()


def test_rate_limit_stops_without_retry_or_fallback(monkeypatch, tmp_path):
    post = http(monkeypatch)
    post.return_value.status_code = 429
    model = JevSentimentModel(api_key="fixture-only", cache_directory=tmp_path)
    with pytest.raises(JevError, match="429"):
        model.classify("The bus was late.")
    with pytest.raises(JevError, match="stopped"):
        model.classify("The train was late.")
    assert post.call_count == 1


def test_truncation_and_topic_gates_do_not_claim_usable_target_scores(monkeypatch, tmp_path):
    body = fixture_response()
    set_choice(body, "relevance", "unrelated")
    http(monkeypatch, body)
    result = JevSentimentModel(api_key="fixture-only", cache_directory=tmp_path).classify("x"*6001)
    assert result["input_truncated"] and result["polarity"] is None
    assert all(t["polarity"] is None for t in result["targets"].values())


def test_offline_request_preparation_never_needs_a_key(monkeypatch, tmp_path):
    from scripts.run_jev_pilot import run
    post = http(monkeypatch)
    source = tmp_path/"sample.json"
    source.write_text(json.dumps([{"doc": {"doc_id": "a", "raw_text": "I like buses."}}]))
    report = run(source, tmp_path/"prepared")
    assert report["mode"] == "prepare_only" and report["live_calls"] == 0
    assert json.loads((tmp_path/"prepared/requests.json").read_text())[0]["request"]["model"] == MODEL
    post.assert_not_called()


def test_pilot_records_latency_and_replay_cost_separately(monkeypatch, tmp_path):
    from scripts.run_jev_pilot import run
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.data_process.jev_sentiment.secret", lambda _: "fixture-only")
    post = http(monkeypatch)
    source = tmp_path / "sample.json"
    source.write_text(json.dumps([{"doc_id": "a", "raw_text": "I like buses."}]))
    first = run(source, tmp_path / "first", live=True)
    replay = run(source, tmp_path / "replay", live=True)
    assert first["live_calls"] == 1 and first["measurements"][0]["cache_hit"] is False
    assert first["estimated_cost_usd_new_responses"] > 0
    assert replay["live_calls"] == 0 and replay["cache_hits"] == 1
    assert replay["estimated_cost_usd_new_responses"] == 0
    assert replay["measurements"][0]["original_request_latency_seconds"] == first["measurements"][0]["original_request_latency_seconds"]
    assert post.call_count == 1


@pytest.mark.integration
def test_jev_candidate_persists_numeric_polarity_separate_from_probability_and_baseline(es_service, monkeypatch, tmp_path):
    from backend.common.es import bulk_documents
    from backend.common.settings import index_name
    http(monkeypatch)
    text = "I like the bus service."
    result = JevSentimentModel(api_key="fixture-only", cache_directory=tmp_path).classify(text)
    original = {"doc_id": "jev-fixture", "raw_text": text, "dataset_kind": "synthetic", "source_dataset": "jev-contract-test",
                "nearest_event_id": "legacy-au-event"}
    row = processed_document(original, result)
    assert row["raw_text"] == original["raw_text"] and row["schema_version"] == 2
    assert row["nearest_event_id"] is None and not row["input_truncated"]
    bulk_documents("social_posts_jev", [row], client=es_service)
    stored = es_service.get(index=index_name("social_posts_jev"), id=row["doc_id"])["_source"]
    assert stored["contextual_sentiment_polarity"] == pytest.approx(.65)
    assert stored["contextual_sentiment_score"] == .8
    assert stored["jev"]["targets"]["public_transport"]["polarity"] == pytest.approx(.65)
    es_service.indices.refresh(index=index_name("social_posts_jev"))
    assert es_service.count(index=index_name("social_posts_jev"), query={"nested": {
        "path": "target_sentiments", "query": {"bool": {"filter": [
            {"term": {"target_sentiments.target": "public_transport"}},
            {"term": {"target_sentiments.accepted": True}},
            {"range": {"target_sentiments.score": {"gt": .6}}}]}}}})["count"] == 1
    assert not es_service.exists(index=index_name("social_posts_processed"), id=row["doc_id"])
