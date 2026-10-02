"""Capture aggregate-only cloud evidence, verify local API parity, build offline HTML."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import requests

from scripts.archive_project import write_json
from scripts.cloud_elasticsearch import Cluster, CONTEXT
from scripts.cloud_fission import router
from scripts.serve_event_dashboard import configuration

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs/evidence/replay-dashboard-snapshot.json"


def cases(config):
    common = {"from": config["from"], "to": config["to"], "dataset_kind": "live"}
    social = {**common, "timezone": "UTC", "model": config["model"], "experiment_id": config["experiment_id"]}
    result = [("experiments/status", {"experiment_id": config["experiment_id"]}, {}),
              ("quality", social, {}), ("oil/prices", {**common, "source_dataset": "eia_brent_daily"}, {})]
    for platform in ("bluesky", "mastodon"):
        for topic in ("fuel_price", "public_transport", "ev", "oil_vehicle"):
            filters = {"platform": platform, "topic": topic}
            result.append(("social/target-sentiment", {**social, **filters}, filters))
    return result


def key(resource, params):
    return resource + "|" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def fetch(session, base, resource, params):
    response = session.get(base + "/api/v1/" + resource, params=params, timeout=60, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError(f"API {resource} returned {response.status_code}")
    body = response.json()
    # Request UUID is intentionally different between servers.
    return {"data": body["data"], "meta": body.get("meta", {})}


def capture(directory, kubeconfig):
    config = configuration(directory)
    config["mode"] = "snapshot"
    saved = {"captured_at": datetime.now(timezone.utc).isoformat(), "configuration": config,
             "description": "Frozen aggregate responses from the completed cloud experiment; no posts or provider credentials",
             "responses": {}}
    with router(Cluster(kubeconfig, CONTEXT)) as (session, base):
        for resource, params, ui in cases(config):
            saved["responses"][key(resource, ui)] = fetch(session, base, resource, params)
    write_json(SNAPSHOT, saved)
    build()


def assert_equivalent(left, right, path="root"):
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)):
        if not math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-10):
            raise ValueError("Numeric API difference at " + path)
    elif isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            raise ValueError("API key difference at " + path)
        for k in left:
            assert_equivalent(left[k], right[k], path + "." + k)
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            raise ValueError("API length difference at " + path)
        for i, (a, b) in enumerate(zip(left, right)):
            assert_equivalent(a, b, path + f"[{i}]")
    elif left != right:
        raise ValueError("API value difference at " + path)


def verify(base):
    from urllib.parse import urlsplit
    if urlsplit(base).hostname != "127.0.0.1" or urlsplit(base).scheme != "http":
        raise ValueError("Verify against a local restored API")
    saved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    checks = []
    with requests.Session() as session:
        session.trust_env = False
        for resource, params, ui in cases(saved["configuration"]):
            actual = fetch(session, base, resource, params)
            assert_equivalent(saved["responses"][key(resource, ui)], actual)
            checks.append({"resource": resource, "filters": ui, "passed": True})
    evidence = {"verified_at": datetime.now(timezone.utc).isoformat(), "passed": True, "checks": checks,
                "scope": "All fields in 11 cloud/local aggregate responses; numeric tolerance 1e-10; request_id excluded"}
    write_json(ROOT / "docs/evidence/local-replay-api-parity.json", evidence)
    print(json.dumps({"api_parity_checks_passed": len(checks)}))


def build():
    saved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    content = (ROOT / "frontend/event_dashboard.html").read_text(encoding="utf-8")
    payload = json.dumps(saved, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    content = content.replace("<script>\n", "<script>\nwindow.TRANSITPULSE_SNAPSHOT=" + payload + ";\n", 1)
    destination = ROOT / "frontend/transitpulse.html"
    destination.write_text(content, encoding="utf-8")
    print(json.dumps({"offline_portfolio": str(destination), "bytes": destination.stat().st_size}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "verify", "build"))
    parser.add_argument("--directory", type=Path, default=ROOT / "data/experiments/iran-20260228")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--url", default="http://127.0.0.1:9092")
    args = parser.parse_args()
    if args.action == "capture":
        if not args.kubeconfig:
            parser.error("capture requires --kubeconfig")
        capture(args.directory, args.kubeconfig)
    elif args.action == "verify":
        verify(args.url)
    else:
        build()


if __name__ == "__main__":
    main()
