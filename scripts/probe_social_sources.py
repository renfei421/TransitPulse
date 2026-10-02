"""Bounded, unauthenticated API feasibility samples; never a platform census.

Retains only aggregate counts/statuses/date ranges, not post text or identities.
No API keys, paid access, posting, or Elasticsearch writes are used.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import time

import requests
from websockets.sync.client import connect

from backend.parallel_harvester.topics import match_topics

STRICT = re.compile(r"\b(petrol|fuel prices?|oil prices?|diesel|electric vehicles?|electric cars?|"
                    r"public transport|myki|ptv|v/?line|ev charging|fuel excise|hormuz)\b", re.I)
REGION = re.compile(r"\b(australia|australian|melbourne|sydney|brisbane|perth|adelaide|canberra|"
                    r"hobart|darwin|queensland|new south wales)\b", re.I)


def mastodon(host):
    records, checks = {}, []
    with requests.Session() as session:
        session.headers["User-Agent"] = "TransportAnalyticsFeasibility/0.2 (bounded academic-project API probe)"
        for tag in ("petrol", "publictransport", "electricvehicles", "australia"):
            url = f"https://{host}/api/v1/timelines/tag/{tag}"
            try:
                response = session.get(url, params={"limit": 40}, timeout=15)
                row = {"url": url, "http_status": response.status_code}
                if response.ok:
                    posts = response.json()
                    if not isinstance(posts, list):
                        raise ValueError("Unexpected API response")
                    dates = []
                    for p in posts:
                        text = html.unescape(re.sub(r"<[^>]+>", " ", p.get("content", "")))
                        records[p.get("uri", p["id"])] = text
                        if p.get("created_at"):
                            dates.append(p["created_at"])
                    row.update(returned=len(posts), oldest=min(dates, default=None), newest=max(dates, default=None))
                checks.append(row)
            except (requests.RequestException, ValueError) as exc:
                checks.append({"url": url, "error_type": type(exc).__name__})
            time.sleep(.5)
    return {"platform": "mastodon", "host": host, "checks": checks, "unique_sampled": len(records),
            "strict_topic_candidates": sum(bool(STRICT.search(t)) for t in records.values()),
            "topic_and_australian_text_marker": sum(bool(STRICT.search(t) and REGION.search(t)) for t in records.values())}


def bluesky_search():
    checks = []
    for query in ("petrol Australia", "public transport Melbourne", "electric vehicle Australia"):
        try:
            response = requests.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                params={"q": query, "sort": "latest", "limit": 100}, timeout=15)
            row = {"query": query, "http_status": response.status_code}
            if response.ok:
                data = response.json()
                row.update(returned=len(data.get("posts", [])), hits_total_reported=data.get("hitsTotal"),
                           has_cursor=bool(data.get("cursor")))
            checks.append(row)
        except requests.RequestException as exc:
            checks.append({"query": query, "error_type": type(exc).__name__})
        time.sleep(.5)
    return {"platform": "bluesky_search", "checks": checks}


def jetstream(seconds=60):
    url = "wss://jetstream1.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post"
    stats = {"platform": "bluesky_jetstream_legacy_v1", "url": url, "budget_seconds": seconds,
             "events": 0, "bytes": 0, "unique_posts": 0, "broad_topic_candidates": 0,
             "strict_topic_candidates": 0, "strict_topic_with_australian_text_marker": 0}
    ids = set()
    started = time.monotonic()
    try:
        with connect(url, open_timeout=15, close_timeout=3, max_size=1024*1024) as stream:
            deadline = time.monotonic()+seconds
            while time.monotonic()<deadline and stats["events"]<10000 and stats["bytes"]<20*1024*1024:
                try:
                    raw = stream.recv(timeout=min(5, max(.01, deadline-time.monotonic())))
                except TimeoutError:
                    continue
                stats["bytes"] += len(raw.encode() if isinstance(raw, str) else raw)
                event = json.loads(raw)
                stats["events"] += 1
                commit = event.get("commit", {})
                if commit.get("operation") not in ("create", "update"):
                    continue
                key = (event.get("did"), commit.get("rkey"))
                if key in ids:
                    continue
                ids.add(key)
                record = commit.get("record", {})
                text = record.get("text", "")
                if not text:
                    continue
                stats["unique_posts"] += 1
                stats["broad_topic_candidates"] += bool(match_topics(text)[0])
                stats["strict_topic_candidates"] += bool(STRICT.search(text))
                stats["strict_topic_with_australian_text_marker"] += bool(STRICT.search(text) and REGION.search(text))
        stats["connected"] = True
    except Exception as exc:
        stats.update(connected=False, error_type=type(exc).__name__)
    stats["wall_seconds"] = round(time.monotonic()-started, 2)
    return stats


def main():
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(mastodon, host) for host in ("mastodon.social", "mastodon.au", "aus.social")]
        futures += [executor.submit(bluesky_search), executor.submit(jetstream)]
        results = []
        for f in futures:
            result = f.result()
            results.append(result)
            print(json.dumps(result), flush=True)
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "results": results,
              "limits": ["Small convenience samples, not total platform volumes or relevance precision estimates",
                         "Text markers are not verified residence; candidate posts require relevance auditing",
                         "Different APIs and sampling windows cannot be compared as equivalent per-day rates",
                         "No Reddit approval, YouTube API key, Threads token or X paid access supplied"]}
    path = Path("artifacts/social-api-feasibility.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
