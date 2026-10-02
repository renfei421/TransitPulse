import json
from pathlib import Path
import pytest
from backend.evaluation.evaluate import metrics, group_split, load_dataset, bootstrap


def test_metrics_confusion_and_macro_average():
    result = metrics(["positive", "neutral", "negative"], ["positive", "neutral", "positive"])
    assert result["accuracy"] == 2/3
    assert result["per_class"]["positive"]["f1"] == pytest.approx(2/3)
    assert result["macro_f1"] == pytest.approx(5/9)
    assert result["confusion_matrix"]["negative"]["positive"] == 1


def test_split_stable_per_thread():
    assert group_split("thread-a") == group_split("thread-a")
    assert set(group_split(str(i)) for i in range(100)) == {"dev", "test"}


def test_illustrative_labels_cannot_be_presented_as_human_gold(tmp_path):
    path = tmp_path/"labels.ndjson"
    path.write_text(json.dumps({"post_id": "a", "thread_root_id": "a", "text": "great",
                               "label": "positive", "label_source": "illustrative"}))
    with pytest.raises(ValueError, match="human_adjudicated"):
        load_dataset(path)
    assert load_dataset(path, allow_illustrative=True)[0]["label"] == "positive"


def test_cluster_bootstrap_reports_independent_sample_size():
    rows = [{"post_id": str(i), "thread_root_id": "a", "label": "positive"} for i in range(20)]
    result = bootstrap(rows, {str(i): "positive" for i in range(20)}, iterations=40)
    assert result["independent_threads"] == 1
    assert result["small_sample_warning"]


@pytest.mark.model
def test_real_pinned_model_probabilities_and_target_retention():
    import os
    if os.getenv("RUN_MODEL_TESTS") != "1":
        pytest.skip("Set RUN_MODEL_TESTS=1 to load the pinned model")
    from backend.data_process.sentiment import SentimentModel, MODEL_REVISION, build_context
    model = SentimentModel()
    results = model.classify_many(["This is wonderful and excellent!", "This is terrible and awful!"])
    assert [row["label"] for row in results] == ["positive", "negative"]
    assert results[0]["polarity"] > 0 and results[1]["polarity"] < 0
    assert all(row["revision"] == MODEL_REVISION for row in results)
    assert all(sum(row["probabilities"].values()) == pytest.approx(1) for row in results)
    context = build_context("neutral "*10000, None, "background "*10000, "TARGETEND terrible")
    tokens = model.pipe.tokenizer(context, truncation=True, max_length=512)
    retained = model.pipe.tokenizer.decode(tokens["input_ids"])
    assert "TARGETEND" in retained
