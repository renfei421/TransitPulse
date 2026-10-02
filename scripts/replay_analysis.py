"""Recompute the frozen experiment from the full private archive without network calls."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from scripts.archive_project import archive_rows, validate_archive, write_json
from scripts.analyze_event_experiment import aggregate, render_figures, verify_scoring
from scripts.portfolio_snapshot import assert_equivalent


def run(archive, experiment, output):
    manifest = validate_archive(archive)
    read = lambda name: json.loads((experiment / name).read_text(encoding="utf-8"))
    plan, cohort, reference = read("plan.json"), read("cohort.json"), read("analysis.json")
    expected = {}
    with (experiment / "input.ndjson").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            expected[row["doc_id"]] = hashlib.sha256(row["raw_text"].encode()).hexdigest()
    seen, review, attempts = set(), defaultdict(list), Counter()
    costs = []
    def sources(index):
        for row in archive_rows(archive / manifest["indices"][index]["file"]):
            if row["_source"].get("experiment_id") == plan["experiment_id"]:
                yield row["_source"]
    def processed():
        for row in sources("v2_social_posts_jev"):
            doc_id = row["doc_id"]
            if doc_id in seen or doc_id not in expected:
                raise ValueError("Unexpected or duplicate result ID")
            seen.add(doc_id)
            if row["jev"]["input_sha256"] != expected[doc_id] or row["model_name"] != plan["model"]:
                raise ValueError("Input fingerprint/model mismatch")
            if row["model_revision"] != plan["model"] or row["jev"]["contract_sha256"] != reference["contract_sha256"]:
                raise ValueError("Frozen model/rubric contract mismatch")
            verify_scoring(row, plan["confidence_threshold"])
            for target in row["target_sentiments"]:
                stratum = (row["platform"], row["experiment_phase"], target["target"], target["accepted"])
                rank = hashlib.sha256(("human-review-v1|" + doc_id + "|" + target["target"]).encode()).hexdigest()
                selected = review[stratum]
                selected.append((rank, row, target))
                selected.sort(key=lambda item: item[0])
                del selected[5:]
            yield row
    result = aggregate(plan, sources("v2_social_discussion_posts_raw"), processed(),
                       read("oil-prices.json"), cohort.get("collection_gaps", []))
    if seen != set(expected) or len(seen) != cohort["eligible_records"]:
        raise ValueError("Incomplete result cohort")
    for field, value in result.items():
        assert_equivalent(reference[field], value, field)
    for row in sources("v2_jev_inference_attempts"):
        attempts[row["status"]] += 1
        costs.append(row["accounted_usd"])
    assert_equivalent(sum(costs), reference["inference"]["accounted_cost_usd"])
    assert_equivalent(dict(attempts), reference["inference"]["statuses"])
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "analysis.json", result)
    figure = render_figures(result, read("oil-prices.json"), output)
    # Blind task file excludes model answers. These are assignments, NOT human labels.
    tasks, predictions = [], []
    for stratum, rows in sorted(review.items()):
        for rank, row, target in rows:
            task_id = "H-" + rank[:16]
            tasks.append({"task_id": task_id, "doc_id": row["doc_id"], "platform": row["platform"],
                "phase": row["experiment_phase"], "text": row["raw_text"], "target": target["target"],
                "reviewer_1_state": None, "reviewer_1_label": None, "reviewer_2_state": None,
                "reviewer_2_label": None, "adjudicated_state": None, "adjudicated_label": None, "notes": ""})
            predictions.append({"task_id": task_id, "selection_stratum": list(stratum), "prediction": target})
    write_json(output / "human-review-tasks.private.json", tasks)
    write_json(output / "human-review-predictions.private.json", predictions)
    evidence = {"passed": True, "network_calls": 0, "provider_calls": 0, "processed_verified": len(seen),
        "aggregate_fields_compared": list(result), "receipts": dict(attempts), "accounted_cost_usd": sum(costs),
        "figure": figure.name, "human_review": {"status": "unlabelled", "task_count": len(tasks),
        "unique_posts": len({r["doc_id"] for r in tasks}), "selection": "up to 5 hash-ranked tasks per platform/phase/target/model-acceptance stratum; not population-representative"}}
    write_json(Path("docs/evidence/offline-analysis-verification.json"), evidence)
    print(json.dumps(evidence))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("data/archives/iran-20260228-final"))
    parser.add_argument("--experiment", type=Path, default=Path("data/experiments/iran-20260228"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/offline-analysis"))
    args = parser.parse_args()
    run(args.archive, args.experiment, args.output)


if __name__ == "__main__":
    main()
