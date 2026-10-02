"""Compare VADER, fixed RoBERTa and contextual RoBERTa on thread-grouped labels."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import time
from backend.data_process.sentiment import SentimentModel, annotate_thread, LABELS
from backend.data_process.threads import build_threads_from_post_nodes


def metrics(truth, predictions):
    if len(truth) != len(predictions) or not truth:
        raise ValueError("Nonempty aligned truth and predictions required")
    matrix = {actual: {predicted: 0 for predicted in LABELS} for actual in LABELS}
    for actual, predicted in zip(truth, predictions):
        if actual not in LABELS or predicted not in LABELS:
            raise ValueError("Labels must be negative, neutral or positive")
        matrix[actual][predicted] += 1
    per_class = {}
    for label in LABELS:
        true_positive = matrix[label][label]
        predicted_count = sum(matrix[actual][label] for actual in LABELS)
        actual_count = sum(matrix[label].values())
        precision = true_positive/predicted_count if predicted_count else 0
        recall = true_positive/actual_count if actual_count else 0
        f1 = 2*precision*recall/(precision+recall) if precision+recall else 0
        per_class[label] = {"precision": precision, "recall": recall, "f1": f1, "support": actual_count}
    return {"n": len(truth), "accuracy": sum(matrix[x][x] for x in LABELS)/len(truth),
            "macro_f1": sum(per_class[x]["f1"] for x in LABELS)/len(LABELS),
            "per_class": per_class, "confusion_matrix": matrix}


def group_split(identifier, seed=42):
    value = int(hashlib.sha256(f"{seed}|{identifier}".encode()).hexdigest()[:8], 16) % 10
    return "test" if value < 2 else "dev"


def bootstrap(rows, predicted, iterations=500, seed=42):
    groups = defaultdict(list)
    for row in rows:
        groups[row["thread_root_id"]].append(row)
    keys = sorted(groups)
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sample = [row for key in rng.choices(keys, k=len(keys)) for row in groups[key]]
        estimates.append(metrics([r["label"] for r in sample], [predicted[r["post_id"]] for r in sample])["macro_f1"])
    estimates.sort()
    return {"low": estimates[int(iterations*.025)], "high": estimates[min(iterations-1, int(iterations*.975))],
            "unit": "thread_cluster", "iterations": iterations, "independent_threads": len(keys),
            "small_sample_warning": len(keys) < 30}


def load_dataset(path, allow_illustrative=False):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    identifiers = set()
    for row in rows:
        if row.get("label") not in LABELS or not row.get("post_id") or not row.get("thread_root_id") or not row.get("text"):
            raise ValueError("Each row needs post_id, thread_root_id, text and a three-class label")
        if row["post_id"] in identifiers:
            raise ValueError("Duplicate post IDs in evaluation set")
        identifiers.add(row["post_id"])
        if row.get("label_source") != "human_adjudicated" and not allow_illustrative:
            raise ValueError("Evaluation claims require human_adjudicated labels; use --allow-illustrative for software smoke tests only")
    if not rows:
        raise ValueError("Empty dataset")
    return rows


def evaluate(path, *, split="test", allow_illustrative=False, vader_only=False):
    rows = load_dataset(path, allow_illustrative)
    selected = [r for r in rows if split == "all" or group_split(r["thread_root_id"]) == split]
    if not selected:
        raise ValueError("Selected split has no labels; supply more threads")
    # All labels for one thread stay in one split. No fitting or parameter tuning
    # is performed by this script; choose rules on dev, then freeze and run test.
    nodes = {row["post_id"]: {"post_id": row["post_id"], "raw_text": row["text"],
              "parent_post_id": row.get("parent_post_id"), "thread_root_id": row["thread_root_id"],
              "candidate_topics": row.get("candidate_topics", [])} for row in rows}
    for row in rows:
        if row.get("parent_post_id") in nodes and nodes[row["parent_post_id"]]["thread_root_id"] != row["thread_root_id"]:
            raise ValueError("Parent/child thread IDs disagree; split leakage risk")
    forest = build_threads_from_post_nodes(nodes)
    configurations = [("vader_local", False, False)]
    if not vader_only:
        configurations += [("roberta_local", True, False), ("roberta_context", True, True)]
    model_cache, results, prediction_rows = {}, {}, []
    for name, transformer, context in configurations:
        if transformer not in model_cache:
            model_cache[transformer] = SentimentModel(use_transformers=transformer)
        model = model_cache[transformer]
        model.cache.clear()
        started = time.perf_counter()
        predicted = {}
        # Batch the local texts across threads before tree-wise annotation.
        model.classify_many([row["text"] for row in rows])
        for tree in forest:
            _, output = annotate_thread(tree, model, use_context=context)
            predicted.update({row["post_id"]: row["contextual_sentiment_label"] for row in output})
        elapsed = time.perf_counter()-started
        result = metrics([row["label"] for row in selected], [predicted[row["post_id"]] for row in selected])
        result.update(model=model.model_name, revision=model.revision,
                      inference_seconds_all_rows=elapsed, rows_inferred=len(rows),
                      macro_f1_ci_95=bootstrap(selected, predicted))
        slices = {}
        for group in ("root", "reply"):
            subset = [row for row in selected if bool(row.get("parent_post_id")) == (group == "reply")]
            if subset:
                slices[group] = metrics([row["label"] for row in subset], [predicted[row["post_id"]] for row in subset])
        result["slices"] = slices
        results[name] = result
        for row in selected:
            prediction_rows.append({"post_id": row["post_id"], "thread_root_id": row["thread_root_id"],
                                   "variant": name, "truth": row["label"], "prediction": predicted[row["post_id"]]})
    return {"dataset_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "claim_status": "illustrative_software_check_only" if allow_illustrative else "human_label_evaluation",
            "split": split, "split_seed": 42, "selected_rows": len(selected),
            "independent_threads": len({row["thread_root_id"] for row in selected}),
            "variants": results, "predictions": prediction_rows,
            "limits": "Context inheritance is a heuristic, not a stance model. No fine-tuning occurred. "
                      "Test labels must remain unseen during rule/model selection."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="test")
    parser.add_argument("--allow-illustrative", action="store_true")
    parser.add_argument("--vader-only", action="store_true")
    parser.add_argument("--output", default="artifacts/evaluation.json")
    args = vars(parser.parse_args())
    output = Path(args.pop("output"))
    result = evaluate(**args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "predictions"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
