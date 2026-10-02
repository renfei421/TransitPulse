"""Read-only restored API checks, including real pagination and legacy-model selection."""
import json
from datetime import datetime, timezone

import requests

from scripts.archive_project import write_json


def main():
    base = "http://127.0.0.1:9092/api/v1/"
    session = requests.Session()
    session.trust_env = False
    checks = []
    def get(path, params=None, expected=200):
        response = session.get(base + path, params=params, timeout=30)
        if response.status_code != expected:
            raise ValueError(f"{path}: {response.status_code}")
        checks.append({"resource": path, "status": response.status_code})
        return response.json()
    if get("meta")["data"]["default_social_model"] != "jev-1.13.0":
        raise ValueError("Default model changed")
    get("health")
    get("openapi.json")
    common = {"from": "2026-01-28", "to": "2026-04-28", "timezone": "UTC", "experiment_id": "iran-20260228-v1"}
    first = get("social/posts", {**common, "limit": 8})["data"]
    second = get("social/posts", {**common, "limit": 8, "cursor": first["next_cursor"]})["data"]
    ids = set()
    for row in first["items"] + second["items"]:
        if row["doc_id"] in ids or not row["raw_text"] or row["model_name"] != "jev-1.13.0":
            raise ValueError("Pagination/model/source mismatch")
        ids.add(row["doc_id"])
    if len(ids) != 16:
        raise ValueError("Expected two complete pages")
    if get("quality", common)["data"]["processed_documents"] != 90576:
        raise ValueError("Cohort count changed")
    old = {"from": "2026-01-01", "to": "2026-12-31", "model": "cardiffnlp/twitter-roberta-base-sentiment-latest", "limit": 2}
    baseline = get("social/posts", old)["data"]["items"]
    if len(baseline) != 2 or any(row["model_name"] != old["model"] for row in baseline):
        raise ValueError("Baseline access changed")
    get("social/target-sentiment", {**common, "model": old["model"], "topic": "fuel_price"}, 400)
    get("social/posts", {"limit": 101}, 400)
    get("unknown", expected=404)
    if get("experiments/status", {"experiment_id": common["experiment_id"]})["data"]["status"] != "complete":
        raise ValueError("Experiment state changed")
    write_json("docs/evidence/local-replay-routes.json", {"verified_at": datetime.now(timezone.utc).isoformat(),
        "passed": True, "checks": checks, "distinct_paginated_posts": len(ids), "old_baseline_examples": len(baseline)})
    print(json.dumps({"local_route_checks": len(checks), "distinct_paginated_posts": len(ids)}))


if __name__ == "__main__":
    main()
