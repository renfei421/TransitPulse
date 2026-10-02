from copy import deepcopy
from unittest.mock import Mock

import pytest

from backend.data_process.llm_annotation import StructuredAnnotator, validate_annotation, AnnotationError, MODEL


def valid():
    return {"relevance": "related", "content_type": "personal_experience", "overall_tone": "mixed",
            "targets": [{"target": "fuel_price", "sentiment": "negative", "evidence": "Fuel is too expensive"}],
            "mode_switch": {"explicit": False, "from_mode": "unknown", "to_mode": "unknown", "evidence": ""},
            "needs_review": False}


def test_structured_annotation_requires_supported_evidence_and_no_invented_probability():
    text = "Fuel is too expensive so I take the bus."
    assert validate_annotation(valid(), text)["targets"][0]["sentiment"] == "negative"
    wrong = valid()
    wrong["targets"][0]["evidence"] = "I hate electric cars"
    with pytest.raises(AnnotationError, match="substring"):
        validate_annotation(wrong, text)
    wrong = valid()
    wrong["confidence"] = .99
    with pytest.raises(AnnotationError, match="fields"):
        validate_annotation(wrong, text)
    wrong = valid()
    wrong["targets"].append(deepcopy(wrong["targets"][0]))
    with pytest.raises(AnnotationError, match="Duplicate"):
        validate_annotation(wrong, text)


def test_budget_fails_before_http_and_does_not_send_a_key_to_other_hosts(tmp_path, monkeypatch):
    post = Mock()
    monkeypatch.setattr("backend.data_process.llm_annotation.requests.post", post)
    client = StructuredAnnotator(api_key="test-only", budget_usd=.000001, cache_directory=tmp_path)
    with pytest.raises(AnnotationError, match="budget"):
        client.annotate({"doc_id": "a", "raw_text": "Fuel is too expensive"})
    post.assert_not_called()


def test_refusal_is_not_cached_and_http_replay_uses_validated_cache(tmp_path, monkeypatch):
    import json
    post = Mock()
    monkeypatch.setattr("backend.data_process.llm_annotation.requests.post", post)
    post.return_value.status_code = 200
    post.return_value.json.return_value = {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "refusal", "refusal": "refused"}]}]}
    client = StructuredAnnotator(api_key="test-only", cache_directory=tmp_path)
    doc = {"doc_id": "a", "raw_text": "Fuel is too expensive"}
    with pytest.raises(AnnotationError, match="declined"):
        client.annotate(doc)
    assert not list(tmp_path.glob("*.json"))
    assert len(list((tmp_path/"attempts").glob("*.json"))) == 1
    post.return_value.json.return_value = {"status": "completed", "model": MODEL, "usage": {}, "output": [
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(valid())}]}]}
    assert not client.annotate(doc)["cache_hit"]
    assert client.annotate(doc)["cache_hit"]
    assert post.call_count == 2
    assert post.call_args.args == ("https://api.openai.com/v1/responses",)
    assert post.call_args.kwargs["json"]["store"] is False
