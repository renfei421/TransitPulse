"""Prepare or run at most 30 Jev evaluations; does not switch cloud API traffic.

Input is an ignored JSON list of raw documents or review entries containing doc.
Credentials come only from environment/Secret files. --live is an explicit opt-in.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from backend.data_process.jev_sentiment import (
    JevSentimentModel, JevError, request_payload, processed_document,
    MODEL, RUBRIC_VERSION, SCORING_VERSION, VALIDATION_VERSION, CONTRACT_SHA256,
)
from scripts.audit_search_reuse import save


def run(input_path, directory, *, limit=30, live=False, budget=.5, threshold=.6):
    if not 1 <= limit <= 30:
        raise ValueError("Pilot limit must be 1..30")
    source = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(source, list):
        raise ValueError("Expected a list of raw posts or review entries")
    docs = [row.get("doc", row) for row in source[:limit]]
    if not docs or any(not isinstance(d.get("raw_text"), str) or not d.get("doc_id") for d in docs):
        raise ValueError("Each pilot row needs doc_id and raw_text")
    if len({d["doc_id"] for d in docs}) != len(docs):
        raise ValueError("Duplicate sample IDs; deduplicate before evaluating")
    report = {"prepared_at_utc": datetime.now(timezone.utc).isoformat(), "model": MODEL,
        "rubric_version": RUBRIC_VERSION, "scoring_version": SCORING_VERSION, "contract_sha256": CONTRACT_SHA256,
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(), "records": len(docs),
        "mode": "live" if live else "prepare_only", "live_calls": 0, "confidence_threshold": threshold,
        "validation_version": VALIDATION_VERSION,
        "limits": ["Threshold is provisional, not calibrated on project human labels",
                   "No cloud write or traffic switch", "Contract validation is not semantic accuracy"]}
    if not live:
        save(directory/"requests.json", [{"doc_id": d["doc_id"], "request": request_payload(d["raw_text"])} for d in docs])
        save(directory/"report.json", report)
        print(json.dumps(report))
        return report
    client = JevSentimentModel(budget_usd=budget, confidence_threshold=threshold)
    outputs, errors, measurements = [], [], []
    identifier = "jev_"+uuid4().hex
    for doc in docs:
        try:
            result = client.classify(doc["raw_text"])
            measurements.append({"doc_id": doc["doc_id"], "cache_hit": result["cache_hit"],
                                 "original_request_latency_seconds": result["latency_seconds"],
                                 "usage": result["usage"]})
            row = processed_document(doc, result)
            row.update(processing_run_id=identifier, processed_at=datetime.now(timezone.utc).isoformat())
            outputs.append(row)
            save(directory/"results"/(hashlib.sha256(doc["doc_id"].encode()).hexdigest()+".json"), row)
        except Exception as exc:
            errors.append({"doc_id": doc["doc_id"], "error_type": type(exc).__name__,
                           "reason": str(exc) if isinstance(exc, JevError) else "Network or response error"})
        report.update(live_calls=client.calls, cache_hits=client.cache_hits, succeeded=len(outputs), failed=len(errors),
                      reserved_usd=client.reserved, estimated_cost_usd_new_responses=client.estimated_cost,
                      errors=errors, measurements=measurements)
        save(directory/"report.json", report)
        if client.stopped:
            break
    report.update(unattempted=len(docs)-len(outputs)-len(errors),
                  labels=dict(Counter(r["contextual_sentiment_label"] for r in outputs)),
                  scored=sum(r["contextual_sentiment_polarity"] is not None for r in outputs))
    save(directory/"processed-candidates.json", outputs)
    save(directory/"report.json", report)
    print(json.dumps(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--budget", type=float, default=.5)
    parser.add_argument("--threshold", type=float, default=.6)
    args = parser.parse_args()
    report = run(args.input, args.directory, limit=args.limit, live=args.live, budget=args.budget, threshold=args.threshold)
    if report.get("failed") or report.get("unattempted"):
        raise SystemExit(1)
