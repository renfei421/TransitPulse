"""Bounded public Mastodon pilot using the existing project's keyword bank.

No credentials, paid API, background collector or model inference. Raw text and
review worksheets stay in ignored data/. Committed reports contain counts only.
Run collect first, review the worksheet, then explicitly ingest the candidates.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from elasticsearch import Elasticsearch
import requests

from backend.common.es import create_documents, CreateCounts, BulkWriteError
from backend.common.time import timestamp
from backend.parallel_harvester.normalize import normalise_mastodon_status, make_doc_id
from backend.parallel_harvester.topics import (
    generate_queries, keyword_bank, match_topics, public_hashtag_seeds, region_evidence,
    country_text_hints, analysis_language_route, QUERY_PROFILES,
)
from scripts.cloud_elasticsearch import Cluster, CONTEXT, connection

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ("mastodon.social", "mastodon.au", "aus.social")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")


def sample_instance(host, seeds, pages):
    records, checks = [], []
    with requests.Session() as session:
        session.headers["User-Agent"] = "TransportAnalytics/0.2 (bounded public-timeline research pilot)"
        for seed in seeds:
            print(json.dumps({"collecting": host, "hashtag": seed["hashtag"]}), flush=True)
            cursor, seen = None, set()
            for page in range(pages):
                url = f"https://{host}/api/v1/timelines/tag/{seed['hashtag']}"
                params = {"limit": 40, **({"max_id": cursor} if cursor else {})}
                check = {"host": host, "hashtag": seed["hashtag"], "page": page+1}
                try:
                    response = session.get(url, params=params, timeout=20)
                    check["http_status"] = response.status_code
                    if response.status_code != 200:
                        checks.append(check)
                        # Do not keep calling an instance after a denial/rate limit.
                        if response.status_code in (401, 403, 429):
                            return records, checks
                        break
                    posts = response.json()
                    if not isinstance(posts, list):
                        raise ValueError("Expected a status list")
                    check["returned"] = len(posts)
                    checks.append(check)
                    for post in posts:
                        records.append({"host": host, "seed": seed, "post": post})
                    following = posts[-1].get("id") if posts else None
                    if not following or following in seen or len(posts) < 40:
                        break
                    seen.add(following)
                    cursor = following
                except (requests.RequestException, ValueError) as exc:
                    checks.append({**check, "error_type": type(exc).__name__})
                    break
                time.sleep(.75)
            time.sleep(.75)
    return records, checks


def deduplicate(records):
    """Canonical ActivityPub URI absorbs repeats across tags, pages and instances."""
    unique, invalid = {}, 0
    for item in records:
        original = item["post"].get("reblog") or item["post"]
        identity = original.get("uri") or original.get("url")
        if not identity:
            invalid += 1
            continue
        if identity not in unique:
            unique[identity] = {**item, "post": original, "canonical_uri": identity, "retrieved_via": []}
        origin = {"host": item["host"], "hashtag": item["seed"]["hashtag"]}
        if origin not in unique[identity]["retrieved_via"]:
            unique[identity]["retrieved_via"].append(origin)
    return list(unique.values()), invalid


def select_review(rows, per_stratum=15):
    groups = defaultdict(list)
    for row in rows:
        groups[row["review_stratum"]].append(row)
    # Hash ordering is deterministic and independent of text sentiment/author.
    return [row for key in sorted(groups) for row in sorted(groups[key], key=lambda r: r["doc"]["doc_id"])[:per_stratum]]


def record_review(directory):
    """Attach completed assistant labels, rejecting stale/misaligned worksheets."""
    path = directory/"review-sample.json"
    samples = json.loads(path.read_text(encoding="utf-8"))
    review = json.loads((directory/"review-labels.json").read_text(encoding="utf-8"))
    if review.get("sample_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError("Review labels refer to a different sample snapshot")
    expected = {r["sample_id"]: r["doc"]["doc_id"] for r in samples}
    labels = review["labels"]
    if len(labels) != len(expected) or {r["sample_id"] for r in labels} != set(expected):
        raise ValueError("Review sample must be covered exactly once")
    strata = {r["sample_id"]: r["review_stratum"] for r in samples}
    for label in labels:
        if (expected.get(label["sample_id"]) != label.get("doc_id") or
                strata[label["sample_id"]] != label.get("stratum") or
                label["topic_relevance"] not in {"related", "adjacent", "unrelated", "uncertain"}):
            raise ValueError("Invalid review identity or category")
    report = json.loads((directory/"report.json").read_text(encoding="utf-8"))
    report["review_sample"].update(reviewer=review["reviewer"], reviewer_type=review["reviewer_type"],
        reviewed_at_utc=review["reviewed_at_utc"], sample_sha256=review["sample_sha256"],
        labels=dict(Counter(r["topic_relevance"] for r in labels)),
        by_stratum={key: dict(Counter(r["topic_relevance"] for r in labels if r["stratum"] == key))
                    for key in sorted({r["stratum"] for r in labels})},
        interpretation="Assistant triage of a stratified sample, not human gold labels or accuracy",
        candidate_storage_policy="Raw candidates retained including reviewed noise; labels are diagnostic, not a hidden filter")
    save(directory/"report.json", report)
    print(json.dumps(report["review_sample"]))
    return report


def collect(directory, pages=2, profile="legacy_au", per_stratum=15):
    bank = keyword_bank(profile)
    directory.mkdir(parents=True, exist_ok=False)
    end = datetime.now(timezone.utc)
    start = end-timedelta(days=90)
    seeds = public_hashtag_seeds(profile)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda host: sample_instance(host, seeds, pages), HOSTS))
    fetched = [r for records, _ in results for r in records]
    checks = [c for _, calls in results for c in calls]
    unique, invalid = deduplicate(fetched)
    rows, outside, malformed = [], 0, 0
    dataset = directory.name
    for item in unique:
        post = item["post"]
        try:
            created = timestamp(post["created_at"])
            if not start <= created <= end:
                outside += 1
                continue
            doc = normalise_mastodon_status(post, server_domain=item["host"], is_seed_post=True,
                source_query="#"+item["seed"]["hashtag"], collection_method="public_tag_pilot")
            if not doc["raw_text"].strip():
                raise ValueError("empty text")
            legacy_topics, _ = match_topics(doc["raw_text"])
            tags = [t.get("name", "") for t in post.get("tags", [])]
            topics, words = match_topics(doc["raw_text"], tags, profile=profile)
            doc.update(source_record_id=doc["post_id"], post_id=item["canonical_uri"],
                       doc_id=make_doc_id("mastodon", item["canonical_uri"]),
                       source_query=["#"+r["hashtag"] for r in item["retrieved_via"]],
                       dataset_kind="live", source_dataset=dataset, schema_version=2,
                       collection_profile=profile, analysis_language_route=analysis_language_route(doc.get("lang")),
                       country_text_hints=country_text_hints(doc["raw_text"]),
                       candidate_topics=topics, direct_candidate_topics=topics,
                       matched_keywords=words, topic_count=len(topics))
            if profile == "global_en":
                # Global observations must not inherit the Australian study's nearest event.
                for field in ("nearest_event_id", "nearest_event_name", "days_from_event", "event_period"):
                    doc[field] = None
            # No thread expansion in this pilot; unresolved parent IDs remain explicit.
            doc["thread_root_id"] = None if doc["parent_post_id"] else doc["post_id"]
            geo = region_evidence(doc["raw_text"])
            if not doc["candidate_topics"]:
                group = "no_topic_match"
            elif not legacy_topics:
                group = "hashtag_added_candidate"
            elif geo["kind"] != "unknown":
                group = "regional_hint_candidate"
            else:
                group = "other_topic_candidate"
            rows.append({"doc": doc, "retrieved_via": item["retrieved_via"], "legacy_text_topics": legacy_topics,
                         "region_evidence": geo, "review_stratum": group})
        except (KeyError, ValueError, TypeError):
            malformed += 1
    candidates = [row for row in rows if row["doc"]["candidate_topics"]]
    selected = select_review(rows, per_stratum=per_stratum)
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "dataset": dataset,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "query_bank": {"profile": profile, "search_queries": len(generate_queries(profile)),
                       "keywords_by_topic": {k: len(v) for k,v in bank.items()},
                       "search_queries_sha256": hashlib.sha256(json.dumps(generate_queries(profile)).encode()).hexdigest(),
                       "keyword_bank_sha256": hashlib.sha256(json.dumps(bank, sort_keys=True).encode()).hexdigest(),
                       "adapter_version": "keyword-hashtag-pilot-v2", "tag_seeds": seeds},
        "budget": {"hosts": list(HOSTS), "pages_per_tag": pages, "max_requests": len(HOSTS)*len(seeds)*pages,
                   "max_returned_records": len(HOSTS)*len(seeds)*pages*40},
        "funnel": {"returned_records": len(fetched), "missing_identity": invalid,
                   "duplicates_removed": len(fetched)-invalid-len(unique), "unique_posts": len(unique),
                   "outside_window": outside, "malformed_or_empty": malformed, "valid_in_window": len(rows),
                   "original_text_rule_candidates": sum(bool(r["legacy_text_topics"]) for r in rows),
                   "topic_candidates_with_hashtags": len(candidates),
                   "geographic_evidence": dict(Counter(r["region_evidence"]["kind"] for r in candidates)),
                   "geographic_evidence_scope": "Australian text lexicon only, not global location coverage",
                   "country_mention_posts": sum(bool(r["doc"]["country_text_hints"]) for r in candidates),
                   "region_filter_dropped": 0},
        "topic_counts_overlap": dict(Counter(t for r in candidates for t in r["doc"]["candidate_topics"])),
        "languages": dict(Counter(r["doc"].get("lang") or "unknown" for r in candidates)),
        "analysis_language_routes": dict(Counter(r["doc"]["analysis_language_route"] for r in candidates)),
        "country_mentions_overlap": dict(Counter(c for r in candidates for c in r["doc"]["country_text_hints"])),
        "review_sample": {"selected": len(selected), "strata": dict(Counter(r["review_stratum"] for r in selected)),
                          "reviewer": "pending", "labels": "pending"},
        "sink": {"status": "not_run", "succeeded": None}, "requests": checks,
        "limits": ["Public hashtag adaptation is not execution of the full-text queries",
                   "English query profile does not guarantee English posts; provider language is unverified",
                   "Country hints are non-exhaustive text mentions, not country-specific opinions or author locations",
                   "Candidates are not all reviewed; geography is a partition, not an exclusion gate",
                   "No language filter; English model processing must be considered separately",
                   "Current 90-day pilot, not restoration of the original February-May study window",
                   "No author residence inferred; no thread expansion or model inference"]}
    save(directory/"records.json", rows)
    save(directory/"review-sample.json", [{"sample_id": f"S{i:03}", **r} for i,r in enumerate(selected, 1)])
    save(directory/"report.json", report)
    print(json.dumps({"directory": str(directory), "funnel": report["funnel"], "review_sample": report["review_sample"]}))


def ingest(directory, kubeconfig):
    rows = json.loads((directory/"records.json").read_text(encoding="utf-8"))
    report = record_review(directory)
    docs = [row["doc"] for row in rows if row["doc"]["candidate_topics"]]
    cloud = Cluster(kubeconfig, CONTEXT)
    credentials = cloud.secret("transport-secrets", "default")
    prefix = os.environ.get("ES_INDEX_PREFIX")
    os.environ["ES_INDEX_PREFIX"] = "v2_"
    total = CreateCounts()
    previous_sink = report["sink"]
    attempt = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "mode": "create_only"}
    try:
        with connection(cloud) as (_, endpoint, ca), Elasticsearch(endpoint,
                basic_auth=(credentials["ES_USER"], credentials["ES_PASSWORD"]), ca_certs=str(ca)) as es:
            for offset in range(0, len(docs), 200):
                try:
                    total.add(create_documents("social_discussion_posts_raw", docs[offset:offset+200], client=es))
                except BulkWriteError as exc:
                    total.add(exc.counts)
                    raise
            # Read-back is real-time (_mget), independent of refresh privileges.
            readback, stored_sources, stored_routes = 0, Counter(), Counter()
            for offset in range(0, len(docs), 200):
                result = es.mget(index="v2_social_discussion_posts_raw", ids=[d["doc_id"] for d in docs[offset:offset+200]],
                                 source=["source_dataset", "analysis_language_route", "lang"])
                for doc in result["docs"]:
                    if doc.get("found"):
                        readback += 1
                        source = doc["_source"]
                        stored_sources[source.get("source_dataset") or "unknown"] += 1
                        if source.get("source_dataset") == report["dataset"]:
                            stored_routes[source.get("analysis_language_route") or analysis_language_route(source.get("lang"))] += 1
            if readback != len(docs):
                raise RuntimeError("Not all source records could be read back")
            report["sink"] = {**attempt, "status": "completed", "index": "v2_social_discussion_posts_raw", **total.as_dict(),
                              "read_back": readback, "first_source_dataset_counts": dict(stored_sources),
                              "this_dataset_stored_language_routes": dict(stored_routes),
                              "dataset_kind": "live", "processed_documents_this_run": 0}
    except Exception as exc:
        report["sink"] = {**attempt, "status": "failed", **total.as_dict(), "error_type": type(exc).__name__,
                          "note": "Partial writes may exist; transport errors can leave unknown outcomes. Safe to replay."}
        raise
    finally:
        if prefix is None:
            os.environ.pop("ES_INDEX_PREFIX", None)
        else:
            os.environ["ES_INDEX_PREFIX"] = prefix
        if previous_sink.get("status") != "not_run":
            report.setdefault("sink_history", []).append(previous_sink)
        save(directory/"report.json", report)
    print(json.dumps(report["sink"]))


def verify(directory, kubeconfig, baseline_directory=None):
    """Read-only verification of first-source provenance and the actual cloud API.

    Baseline comes first: overlap must still belong to the earlier observation.
    The aggregate API may include other batches, so only per-dataset counts are
    asserted exactly. No refresh, model run, reindexing or data deletion occurs.
    """
    expected, snapshots = {}, []
    for folder in ([baseline_directory] if baseline_directory else []) + [directory]:
        path = folder/"records.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        snapshots.append({"dataset": folder.name, "records_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for row in rows:
            doc = row["doc"]
            if doc["candidate_topics"]:
                expected.setdefault(doc["doc_id"], doc)
    cloud = Cluster(kubeconfig, CONTEXT)
    credentials = cloud.secret("transport-secrets", "default")
    sources, languages = Counter(), Counter()
    ids = list(expected)
    with connection(cloud) as (_, endpoint, ca), Elasticsearch(endpoint,
            basic_auth=(credentials["ES_USER"], credentials["ES_PASSWORD"]), ca_certs=str(ca)) as es:
        for offset in range(0, len(ids), 200):
            response = es.mget(index="v2_social_discussion_posts_raw", ids=ids[offset:offset+200],
                              source=["source_dataset", "raw_text", "lang"])
            for row in response["docs"]:
                original = expected[row["_id"]]
                if (not row.get("found") or row["_source"].get("source_dataset") != original["source_dataset"]
                        or row["_source"].get("raw_text") != original["raw_text"]):
                    raise RuntimeError("A first observation's provenance or text was not preserved")
                sources[row["_source"]["source_dataset"]] += 1
                languages[analysis_language_route(row["_source"].get("lang"))] += 1
        if sum(sources.values()) != len(expected):
            raise RuntimeError("Incomplete verification response")
        total = es.count(index="v2_social_discussion_posts_raw")["count"]
    # Pad UTC observation days for the currently deployed API's Sydney calendar.
    dates = [timestamp(d["created_at"]) for d in expected.values()]
    params = {"from": (min(dates)-timedelta(days=1)).date().isoformat(),
              "to": (max(dates)+timedelta(days=1)).date().isoformat(), "dataset_kind": "live"}
    from scripts.cloud_fission import router
    api = []
    with router(cloud) as (session, base):
        for dataset in [None, *sources]:
            query = {**params, **({"source_dataset": dataset} if dataset else {})}
            response = session.get(base+"/api/v1/quality", params=query, timeout=40)
            response.raise_for_status()
            body = response.json()
            if dataset and body["data"]["raw_documents"] != sources[dataset]:
                raise RuntimeError("Cloud API disagrees with stored dataset counts")
            api.append({"params": query, "status": response.status_code, "body": body})
    evidence = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "context": CONTEXT,
                "input_snapshots": snapshots, "verified_first_observations": len(expected),
                "stored_first_source_counts": dict(sources), "raw_index_total": total,
                "language_routes_from_provider_metadata": dict(languages), "cloud_api": api,
                "limits": ["Language metadata not independently verified", "Raw candidates include noise",
                           "Country hints are not author locations", "This check does not run model processing"]}
    save(directory/"verification.json", evidence)
    print(json.dumps({k: evidence[k] for k in ("verified_first_observations", "stored_first_source_counts",
                                               "raw_index_total", "language_routes_from_provider_metadata")}))


def analyse(directory, baseline_directory):
    """Measure incremental query yield without claiming independent opinions."""
    rows = json.loads((directory/"records.json").read_text(encoding="utf-8"))
    old = json.loads((baseline_directory/"records.json").read_text(encoding="utf-8"))
    old_ids = {r["doc"]["doc_id"] for r in old if r["doc"]["candidate_topics"]}
    report = json.loads((directory/"report.json").read_text(encoding="utf-8"))
    candidates = [r for r in rows if r["doc"]["candidate_topics"]]
    contributions = []
    for seed in report["query_bank"]["tag_seeds"]:
        tag = seed["hashtag"]
        found = [r for r in candidates if any(o["hashtag"] == tag for o in r["retrieved_via"])]
        requests = [c for c in report["requests"] if c["hashtag"] == tag]
        contributions.append({"hashtag": tag, "requests": len(requests),
            "returned": sum(c.get("returned", 0) for c in requests), "in_window_candidates": len(found),
            "new_vs_previous_batch": sum(r["doc"]["doc_id"] not in old_ids for r in found)})
    text_counts = Counter(re.sub(r"\s+", " ", r["doc"]["raw_text"]).strip().casefold() for r in candidates)
    report["incremental_analysis"] = {
        "comparison_dataset": baseline_directory.name, "per_tag_counts_overlap": contributions,
        "canonical_uri_unique_candidates": len(candidates), "exact_normalized_text_unique": len(text_counts),
        "extra_posts_with_identical_normalized_text": sum(n-1 for n in text_counts.values()),
        "normalization": "Whitespace collapse and Unicode casefold; URLs kept; no semantic deduplication",
        "limits": ["Per-tag rows overlap and cannot be summed",
                   "Two captures have different queries and timestamps; this is observed incremental yield, not a causal A/B estimate",
                   "Different post IDs, or even different text, do not establish independent people or opinions"]}
    save(directory/"report.json", report)
    print(json.dumps(report["incremental_analysis"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "review", "ingest", "verify", "analyse"))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--pages", type=int, choices=range(1,4), default=2)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--baseline-directory", type=Path)
    parser.add_argument("--profile", choices=QUERY_PROFILES, default="legacy_au")
    parser.add_argument("--per-stratum", type=int, choices=range(1, 51), default=15)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    if args.action == "collect":
        collect(directory, args.pages, args.profile, args.per_stratum)
    elif args.action == "review":
        record_review(directory)
    elif args.action == "analyse":
        if not args.baseline_directory:
            parser.error("analyse requires --baseline-directory")
        analyse(directory, args.baseline_directory)
    elif args.action == "verify" and args.kubeconfig:
        verify(directory, args.kubeconfig, args.baseline_directory)
    elif args.kubeconfig:
        ingest(directory, args.kubeconfig)
    else:
        parser.error("ingest/verify requires --kubeconfig")
