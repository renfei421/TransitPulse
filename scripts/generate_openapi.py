"""Publish the versioned API's bounded filters and stable resource names."""
import json
from pathlib import Path
from backend.api.app import RESOURCES
from backend.api.queries import RESOURCE_FILTERS


def generate():
    common = [
        {"name": name, "in": "query", "schema": {"type": "string", "format": "date"},
         "description": "Inclusive Australia/Sydney calendar date; maximum window 367 days"}
        for name in ("from", "to")]
    common.extend([
        {"name": "dataset_kind", "in": "query", "schema": {"type": "string", "default": "observed",
         "enum": ["observed", "live", "imported", "synthetic", "all"]},
         "description": "Observed excludes synthetic and fixture records. All is an explicit diagnostic opt-in."},
        {"name": "model", "in": "query", "schema": {"type": "string", "maxLength": 200}},
        {"name": "source_dataset", "in": "query", "schema": {"type": "string", "maxLength": 200}},
        {"name": "platform", "in": "query", "schema": {"type": "string", "maxLength": 200}},
        {"name": "topic", "in": "query", "schema": {"type": "string", "enum": ["fuel_price", "ev", "public_transport"]}},
    ])
    paths = {}
    for name in RESOURCES:
        params = [p for p in common if p["name"] in RESOURCE_FILTERS[name] | {"from", "to"}]
        if name == "social/posts":
            params += [{"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}},
                       {"name": "cursor", "in": "query", "schema": {"type": "string", "maxLength": 1024},
                        "description": "Opaque next_cursor from the previous page; preserve all other filters"}]
        if name == "news/volume":
            params += [{"name": "keyword", "in": "query", "schema": {"type": "string", "default": "iran_hormuz"}}]
        if name == "analyses/oil-sentiment":
            params = [p for p in params if p["name"] in {"from", "to", "dataset_kind", "model"}]
        paths["/api/v1/"+name] = {"get": {"operationId": name.replace("/", "_"),
            "summary": name.replace("/", " ").capitalize(), "parameters": params,
            "responses": {str(code): {"description": meaning} for code, meaning in (
                (200, "JSON envelope: data, meta, request_id"), (400, "Invalid filters or dates"),
                (404, "No such resource or precomputed result"), (503, "Dependency unavailable"))}}}
    for name in ("meta", "health", "openapi.json"):
        paths["/api/v1/"+name] = {"get": {"responses": {"200": {"description": name}}}}
    doc = {"openapi": "3.1.0", "info": {"title": "Transport discourse analytics", "version": "1.0.0",
        "description": "Observational sample analytics. Sentiment balance=(positive-negative)/classified. "
                       "Model confidence is not sentiment direction. Topic groups overlap. No causal claims."},
        "paths": paths}
    Path("backend/api/openapi.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


if __name__ == "__main__":
    generate()
