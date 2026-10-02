"""Resumable event-window collection with a transactional SQLite audit ledger.

Each page, normalized documents, and its cursor commit in one transaction.
Collected bodies stay under ignored data/. No sentiment-based sampling is used.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import requests

from backend.common.time import timestamp, utc_now
from backend.ingestion.authenticated_bluesky import BlueskyReader, SourceError
from backend.ingestion.import_ndjson import atomic_json, normalize_record
from backend.parallel_harvester.normalize import normalise_mastodon_status, make_doc_id
from backend.parallel_harvester.topics import match_topics, country_text_hints


def load_private(path):
    """Only load the named local file; never print or package its values."""
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                os.environ[key.strip()] = value.strip().strip('"').strip("'")


def bounds(plan):
    first = datetime.combine(date.fromisoformat(plan["start"]), datetime.min.time(), timezone.utc)
    last = datetime.combine(date.fromisoformat(plan["end"]) + timedelta(days=1), datetime.min.time(), timezone.utc)
    if first >= last or plan["timezone"] != "UTC":
        raise ValueError("An ordered UTC event window is required")
    return first, last


def phase(day, plan):
    if day < plan["event_date"]:
        return "pre"
    if day < plan["analysis"]["post_month_2"][0]:
        return "post_month_1"
    return "post_month_2"


def normalize(post, platform, plan, source_query, host=None):
    first, last = bounds(plan)
    if platform == "mastodon":
        post = post.get("reblog") or post
        if not post.get("uri"):
            raise ValueError("Mastodon record has no canonical identity")
        doc = normalise_mastodon_status(post, server_domain=host, source_query=source_query,
            is_seed_post=True, collection_method="historical_public_tag")
        doc["doc_id"] = make_doc_id("mastodon", post["uri"])
        doc["post_id"] = post["uri"]
    else:
        doc = normalize_record(post, source_dataset=plan["experiment_id"] + "_" + platform,
                               dataset_kind="live")
    created = timestamp(doc["created_at"])
    if not first <= created < last:
        return None
    topics, keywords = match_topics(doc["raw_text"], profile="global_en")
    doc.update(created_at=created.isoformat(), fetched_at=utc_now(), schema_version=2,
        dataset_kind="live", source_dataset=plan["experiment_id"] + "_" + platform,
        experiment_id=plan["experiment_id"], experiment_phase=phase(created.date().isoformat(), plan),
        analysis_timezone="UTC", collection_profile="global_en", source_query=source_query,
        candidate_topics=topics, direct_candidate_topics=topics, matched_keywords=keywords,
        country_text_hints=country_text_hints(doc["raw_text"]),
        nearest_event_id=plan["experiment_id"], nearest_event_name="US-Iran conflict onset",
        days_from_event=(created.date()-date.fromisoformat(plan["event_date"])).days,
        event_period=phase(created.date().isoformat(), plan))
    return doc


def open_ledger(directory, plan):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory/"collection.sqlite", timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, platform TEXT, query TEXT, day TEXT,
            host TEXT, cursor TEXT, pages INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', error TEXT);
        CREATE TABLE IF NOT EXISTS pages(task TEXT, page INTEGER, returned INTEGER, malformed INTEGER,
            outside INTEGER, accepted INTEGER, next_cursor TEXT, fetched_at TEXT, PRIMARY KEY(task,page));
        CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, platform TEXT, created_at TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS sightings(doc_id TEXT, task TEXT, PRIMARY KEY(doc_id,task));
    ''')
    signature = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    stored = db.execute("SELECT value FROM meta WHERE key='plan_sha256'").fetchone()
    if stored and stored[0] != signature:
        raise ValueError("Collection directory belongs to a different frozen plan")
    db.execute("INSERT OR IGNORE INTO meta VALUES('plan_sha256',?)", (signature,))
    first, last = bounds(plan)
    day = first.date()
    while day < last.date():
        for q in plan["bluesky_queries"]:
            key = "b|" + day.isoformat() + "|" + q
            db.execute("INSERT OR IGNORE INTO tasks(id,platform,query,day) VALUES(?,?,?,?)",
                       (key, "bluesky", q, day.isoformat()))
        day += timedelta(days=1)
    for host in plan["mastodon_hosts"]:
        for tag in plan["mastodon_tags"]:
            db.execute("INSERT OR IGNORE INTO tasks(id,platform,query,host) VALUES(?,?,?,?)",
                       ("m|"+host+"|"+tag, "mastodon", tag, host))
    db.commit()
    atomic_json(directory/"plan.json", plan)
    return db


def commit_page(db, task, posts, following, plan):
    key, platform, query, day, host, cursor, pages, _, _ = task
    docs, malformed, outside, dates = [], 0, 0, []
    for post in posts:
        try:
            original = post.get("reblog") or post
            source_time = original.get("created_at") if platform == "mastodon" else original.get("record", {}).get("createdAt")
            dates.append(timestamp(source_time))
            doc = normalize(post, platform, plan, query, host)
            if doc is None:
                outside += 1
            else:
                docs.append(doc)
        except (ValueError, KeyError, TypeError):
            malformed += 1
    first, _ = bounds(plan)
    seen = {row[0] for row in db.execute("SELECT next_cursor FROM pages WHERE task=?", (key,))}
    limit = plan["bluesky_pages_per_day_query"] if platform == "bluesky" else plan["mastodon_pages_per_host_tag"]
    status = "pending"
    if not posts or not following:
        status = "exhausted"
    elif platform == "mastodon" and dates and max(dates) < first:
        status = "passed_start"
    elif following == cursor or following in seen:
        status = "repeated_cursor"
    elif pages + 1 >= limit:
        status = "capped"
    with db:
        for doc in docs:
            db.execute("INSERT OR IGNORE INTO docs VALUES(?,?,?,?)", (doc["doc_id"], platform,
                doc["created_at"], json.dumps(doc, ensure_ascii=False, allow_nan=False)))
            db.execute("INSERT OR IGNORE INTO sightings VALUES(?,?)", (doc["doc_id"], key))
        db.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?,?)",
            (key, pages+1, len(posts), malformed, outside, len(docs), following, utc_now()))
        db.execute("UPDATE tasks SET cursor=?,pages=?,status=?,error=NULL WHERE id=?",
                   (following, pages+1, status, key))
    return status


def summarize(db, directory):
    summary = {"checked_at": utc_now(), "platforms": {}}
    for platform in ("bluesky", "mastodon"):
        totals = db.execute("SELECT COALESCE(SUM(p.returned),0),COALESCE(SUM(p.malformed),0),COALESCE(SUM(p.outside),0),COUNT(*) FROM pages p JOIN tasks t ON p.task=t.id WHERE t.platform=?", (platform,)).fetchone()
        rows = [json.loads(row[0]) for row in db.execute("SELECT body FROM docs WHERE platform=?", (platform,))]
        summary["platforms"][platform] = {"returned_occurrences":totals[0], "malformed":totals[1],
            "outside_event_window_occurrences":totals[2], "request_pages":totals[3],
            "unique_in_window":len(rows), "topic_keyword_matched":sum(bool(r["candidate_topics"]) for r in rows),
            "with_country_text_evidence":sum(bool(r["country_text_hints"]) for r in rows),
            "country_filter_applied":False,
            "task_statuses":dict(db.execute("SELECT status,COUNT(*) FROM tasks WHERE platform=? GROUP BY status", (platform,))),
            "by_phase":dict(Counter(r["experiment_phase"] for r in rows))}
    atomic_json(Path(directory)/"collection-summary.json", summary)
    return summary


def run(plan, directory, platform, *, page_budget=10000):
    db = open_ledger(directory, plan)
    reader, session = BlueskyReader(), requests.Session()
    session.headers["User-Agent"] = "TransportAnalyticsEventStudy/1.0 (bounded public data collection)"
    pages_this_run = 0
    try:
        while pages_this_run < page_budget:
            task = db.execute("SELECT * FROM tasks WHERE platform=? AND status='pending' ORDER BY id LIMIT 1", (platform,)).fetchone()
            if task is None:
                break
            key, _, query, day, host, cursor, pages, _, _ = task
            try:
                if platform == "bluesky":
                    params = {"q":query, "lang":"en", "sort":"latest", "limit":100,
                        "since":day+"T00:00:00Z", "until":(date.fromisoformat(day)+timedelta(days=1)).isoformat()+"T00:00:00Z"}
                    if cursor:
                        params["cursor"] = cursor
                    payload = reader.search(params)
                    posts, following = payload["posts"], payload.get("cursor")
                else:
                    params = {"limit":40, **({"max_id":cursor} if cursor else {})}
                    r = session.get(f"https://{host}/api/v1/timelines/tag/{query}", params=params,
                                    timeout=(10,30), allow_redirects=False)
                    if r.status_code != 200:
                        raise SourceError(f"Mastodon HTTP {r.status_code}")
                    posts = r.json()
                    if not isinstance(posts, list):
                        raise SourceError("Mastodon schema mismatch")
                    following = str(posts[-1]["id"]) if posts else None
                status = commit_page(db, task, posts, following, plan)
            except (requests.RequestException, SourceError, ValueError) as exc:
                # Leave this task pending; a failed upstream call must not mean empty data.
                reason = str(exc) if isinstance(exc, SourceError) else type(exc).__name__
                with db:
                    db.execute("UPDATE tasks SET error=? WHERE id=?", (reason,key))
                summarize(db,directory)
                raise SourceError(f"Collection paused: {reason}") from None
            pages_this_run += 1
            if pages_this_run % 20 == 0 or status != "pending":
                print(json.dumps({"platform":platform,"day":day,"query":query,"pages":pages+1,
                    "returned":len(posts),"task_status":status,"pages_this_run":pages_this_run}), flush=True)
            if pages_this_run % 50 == 0:
                summarize(db,directory)
            time.sleep(plan["request_pause_seconds"])
        result = summarize(db,directory)
        print(json.dumps(result),flush=True)
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--platform", choices=("bluesky","mastodon"), required=True)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--page-budget", type=int, default=10000)
    args = parser.parse_args()
    if args.credentials_file:
        load_private(args.credentials_file)
    run(json.loads(args.plan.read_text()),args.directory,args.platform,page_budget=args.page_budget)


if __name__ == "__main__":
    main()
