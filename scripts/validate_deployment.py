"""Offline checks supplement (not replace) Kubernetes admission and Fission validation."""
from pathlib import Path
import yaml
from scripts.render_deployment import generate
from backend.api.app import RESOURCES


def validate():
    root = Path(__file__).resolve().parents[1]
    specs = list(yaml.safe_load_all((root/"specs/application.yaml").read_text()))
    import json
    from jsonschema import Draft7Validator
    schemas = json.loads((root/"deploy/schemas/fission-v1.23.0.json").read_text())
    for item in specs:
        if item["kind"] in schemas:
            Draft7Validator(schemas[item["kind"]]).validate(item)
    packages = {item["metadata"]["name"] for item in specs if item["kind"] == "Package"}
    functions = {item["metadata"]["name"] for item in specs if item["kind"] == "Function"}
    envs = {item["metadata"]["name"] for item in specs if item["kind"] == "Environment"}
    for item in specs:
        kind, spec = item["kind"], item.get("spec", {})
        if kind == "Function":
            assert spec["package"]["packageref"]["name"] in packages
            assert spec["environment"]["name"] in envs
            assert spec["functionTimeout"] > 0
        if kind in {"HTTPTrigger", "TimeTrigger"}:
            assert spec["functionref"]["name"] in functions
        if kind == "Environment":
            assert not spec["runtime"]["image"].endswith(":latest")
            runtime = spec["runtime"]
            assert runtime["container"]["name"] == runtime["podspec"]["containers"][0]["name"]
    count = len(specs)
    for path in (root/"deploy/generated").glob("*.yaml"):
        for item in yaml.safe_load_all(path.read_text()):
            assert item["apiVersion"] and item["kind"] and item["metadata"]["name"]
            if item["kind"] == "CronJob":
                assert item["spec"]["concurrencyPolicy"] == "Forbid"
                assert item["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] > 0
            if item["kind"] == "ConfigMap":
                assert not any(key.endswith(".py") for key in item.get("data", {}))
                assert not any("PASSWORD" in key or "TOKEN" in key for key in item.get("data", {}))
            if item["kind"] == "Job" and item["metadata"]["name"] == "transport-schema-migration":
                credentials = item["spec"]["template"]["spec"]["containers"][0]["envFrom"]
                assert {"secretRef": {"name": "transport-migration-secrets"}} in credentials
                assert {"secretRef": {"name": "transport-secrets"}} not in credentials
            count += 1
    alternate = generate("app:review", "model:review", "runtime:review", "jobs", "functions", queue_mode=True)
    binding = next(r for r in alternate["core"] if r["kind"] == "RoleBinding")
    assert binding["metadata"]["namespace"] == "jobs" and binding["subjects"][0]["namespace"] == "functions"
    assert not any(r.get("metadata", {}).get("name") == "transport-social-job" and r["kind"] == "TimeTrigger"
                   for r in alternate["fission"])
    import json
    openapi = json.loads((root/"backend/api/openapi.json").read_text())
    assert all("/api/v1/"+resource in openapi["paths"] for resource in RESOURCES)
    operations = [route["get"]["operationId"] for route in openapi["paths"].values() if "get" in route and "operationId" in route["get"]]
    assert len(operations) == len(set(operations)), "OpenAPI operation IDs must be unique"
    print(f"Validated {count} declarative resources and API resource coverage offline")
    return count


if __name__ == "__main__":
    validate()
