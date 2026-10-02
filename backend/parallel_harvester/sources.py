"""Platform-specific API calls plus shared harvesting workflow."""

from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
import time

import requests

from backend.parallel_harvester import config
from backend.common.http import get as retry_get
from backend.common.logging import get_logger
from backend.common.es import WriteCounts
from backend.parallel_harvester.es_client import bulk_index, bulk_upsert_posts
from backend.parallel_harvester.normalize import (
    apply_seed_type,
    make_edge,
    normalise_bluesky_post,
    normalise_mastodon_status,
)

log = get_logger("harvester")
study_window = ContextVar("study_window", default=None)


def get_window():
    return study_window.get() or (config.STUDY_START, config.STUDY_END)


def server_domain(base_url):
    return base_url.replace("https://", "").replace("http://", "").strip("/")


def parse_utc_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def in_study_window(created_at):
    created_dt = parse_utc_datetime(created_at)
    start, end = get_window()
    start_dt = parse_utc_datetime(start)
    end_dt = parse_utc_datetime(end)
    if created_dt is None or start_dt is None or end_dt is None:
        return False
    return start_dt <= created_dt <= end_dt


def should_keep_doc(doc):
    return in_study_window(doc.get("created_at"))


def retry_after_seconds(response, default_seconds):
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return default_seconds
    try:
        seconds = float(retry_after)
    except ValueError:
        return default_seconds
    return max(0.0, min(seconds, config.MASTODON_RETRY_AFTER_CAP_SECONDS))


def mastodon_get(endpoint, headers, params=None):
    for attempt in range(config.MASTODON_MAX_RETRIES + 1):
        response = requests.get(endpoint, headers=headers, params=params, timeout=config.REQUEST_TIMEOUT)
        if response.status_code != 429:
            response.raise_for_status()
            return response

        wait_seconds = retry_after_seconds(response, config.MASTODON_RETRY_BACKOFF_SECONDS)
        if attempt >= config.MASTODON_MAX_RETRIES:
            response.raise_for_status()
        log.info(f"Mastodon rate limited (429). Waiting {wait_seconds:g}s before retry {attempt + 1}/{config.MASTODON_MAX_RETRIES}.")
        time.sleep(wait_seconds)

    raise RuntimeError("Mastodon request retry loop exited unexpectedly.")


def seed_limits(seed_doc):
    if seed_doc.get("seed_type") == "reply_seed":
        return config.REPLY_SEED_MAX_DEPTH, config.REPLY_SEED_MAX_REPLIES, config.REPLY_SEED_MAX_API_CALLS
    return config.MAIN_POST_MAX_DEPTH, config.MAIN_POST_MAX_REPLIES, config.MAIN_POST_MAX_API_CALLS


def expand_seed(seed_doc, headers, platform_label, get_direct_replies, normalise_reply, sleep_seconds, edges_fields):
    max_depth, max_replies, max_calls = seed_limits(seed_doc)
    queue = deque([(seed_doc["post_id"], 0)])
    expanded = set()
    saved = set()
    nodes = []
    edges = []
    api_calls = 0
    reply_count = 0

    while queue:
        current_id, current_depth = queue.popleft()
        if current_id in expanded or current_depth >= max_depth:
            continue
        if api_calls >= max_calls or reply_count >= max_replies:
            break
        try:
            api_calls += 1
            direct_replies = get_direct_replies(current_id, headers)
            expanded.add(current_id)
            time.sleep(sleep_seconds)
        except Exception as error:
            log.info(f"{platform_label} reply expansion failed for {current_id}: {error}")
            continue

        next_depth = current_depth + 1
        for reply in direct_replies:
            if reply_count >= max_replies:
                break
            reply_doc = normalise_reply(reply, seed_doc)
            reply_id = reply_doc.get("post_id")
            if not reply_id:
                continue
            if not should_keep_doc(reply_doc):
                continue
            if reply_id not in saved:
                nodes.append(reply_doc)
                edges.append(make_edge(seed_doc, reply_doc, next_depth, edge_fields=edges_fields))
                saved.add(reply_id)
                reply_count += 1
            if next_depth < max_depth:
                queue.append((reply_id, next_depth))

    return {"nodes": nodes, "edges": edges, "api_calls": api_calls, "reply_count": reply_count}


def harvest_discussions(platform_label, queries, headers, search_query, expand_seed_for_platform, posts_fields, edges_fields, max_seeds=None):
    seed_count = 0
    seed_fetched = 0
    seed_writes = WriteCounts()
    node_writes = WriteCounts()
    edge_writes = WriteCounts()
    query_errors = 0
    seen_seeds = set()

    def summary():
        return {
            "seed_fetched":seed_fetched, "seed_inserted":seed_writes.created,
            "reply_nodes_inserted":node_writes.created, "reply_edges_inserted":edge_writes.created,
            "seeds_processed":seed_count, "queries_failed":query_errors,
            "writes":{"seeds":seed_writes.as_dict(), "replies":node_writes.as_dict(), "edges":edge_writes.as_dict()},
        }

    for query in queries:
        log.info(f"{platform_label} search query: {query}")
        try:
            seed_docs = search_query(query, headers, posts_fields)
        except Exception as error:
            query_errors += 1
            log.info(f"{platform_label} search skipped for [{query}]: {error}")
            continue
        seed_fetched += len(seed_docs)
        if seed_docs:
            seed_writes.add(bulk_upsert_posts(config.POSTS_INDEX, seed_docs))
        for seed_doc in seed_docs:
            if seed_doc["post_id"] in seen_seeds:
                continue
            if max_seeds is not None and seed_count >= max_seeds:
                return summary()
            seen_seeds.add(seed_doc["post_id"])
            seed_count += 1
            log.info(f"Expanding {platform_label} seed {seed_count}: {seed_doc['post_id']} ({seed_doc.get('seed_type')}) [query: {query}]")
            result = expand_seed_for_platform(seed_doc, headers, posts_fields, edges_fields)
            if result["nodes"]:
                node_writes.add(bulk_upsert_posts(config.POSTS_INDEX, result["nodes"]))
            if result["edges"]:
                edge_writes.add(bulk_index(config.EDGES_INDEX, result["edges"], "edge_id"))
            log.info("seed_complete", extra={"fields":{"platform":platform_label,"replies":result["reply_count"],"api_calls":result["api_calls"]}})

    return summary()


def harvest_result(seed_fetched, seed_inserted, node_inserted, edge_inserted, seed_count):
    return {
        "seed_fetched": seed_fetched,
        "seed_inserted": seed_inserted,
        "reply_nodes_inserted": node_inserted,
        "reply_edges_inserted": edge_inserted,
        "seeds_processed": seed_count,
    }


def create_bluesky_headers():
    if not config.BLUESKY_HANDLE or not config.BLUESKY_APP_PASSWORD:
        log.info("Bluesky credentials are missing. Configure the mounted credentials Secret.")
        return {}
    endpoint = f"{config.BLUESKY_BASE_URL.rstrip('/')}/xrpc/com.atproto.server.createSession"
    response = requests.post(
        endpoint,
        json={"identifier": config.BLUESKY_HANDLE, "password": config.BLUESKY_APP_PASSWORD},
        timeout=config.REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    access_jwt = response.json().get("accessJwt")
    if not access_jwt:
        raise RuntimeError("Bluesky session did not return accessJwt.")
    return {"Authorization": f"Bearer {access_jwt}"}


def search_bluesky_query(query, headers, posts_fields):
    base_urls = [config.BLUESKY_PUBLIC_BASE_URL.rstrip("/"), config.BLUESKY_BASE_URL.rstrip("/")]
    start, end = get_window()
    last_error = None
    for base_url in base_urls:
        output = []
        cursor = None
        seen_cursors = set()
        try:
            for _ in range(config.MAX_PAGES_PER_QUERY):
                endpoint = f"{base_url}/xrpc/app.bsky.feed.searchPosts"
                params = {
                    "q": query,
                    "limit": config.PAGE_SIZE_BLUESKY,
                    "sort": "latest",
                    "since": start,
                    "until": end,
                }
                if cursor:
                    params["cursor"] = cursor
                request_headers = headers if base_url == config.BLUESKY_BASE_URL.rstrip("/") else {}
                response = retry_get(endpoint, headers=request_headers, params=params, timeout=config.REQUEST_TIMEOUT)
                if response.status_code in [401, 403] and base_url != base_urls[-1]:
                    raise RuntimeError(f"search failed: {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                posts = payload.get("posts") or []
                for post in posts:
                    doc = normalise_bluesky_post(post, post_fields=posts_fields, source_query=query, is_seed_post=True, collection_method="keyword_search_seed")
                    if doc.get("candidate_topics") and should_keep_doc(doc):
                        output.append(apply_seed_type(doc))
                cursor = payload.get("cursor")
                if not cursor or not posts or cursor in seen_cursors:
                    break
                seen_cursors.add(cursor)
                last_time = posts[-1].get("record", {}).get("createdAt") or posts[-1].get("indexedAt")
                if last_time and parse_utc_datetime(last_time) < parse_utc_datetime(start):
                    break
                time.sleep(config.SLEEP_SECONDS)
            return output
        except Exception as error:
            last_error = error
            log.info(f"Bluesky search fallback for [{query}]: {error}")
    raise RuntimeError("All Bluesky search endpoints failed") from last_error


def get_bluesky_direct_replies(uri, headers):
    endpoint = f"{config.BLUESKY_PUBLIC_BASE_URL.rstrip('/')}/xrpc/app.bsky.feed.getPostThread"
    response = retry_get(endpoint, params={"uri": uri, "depth": 1, "parentHeight": 0}, timeout=config.REQUEST_TIMEOUT)
    response.raise_for_status()
    thread = response.json().get("thread") or {}
    replies = thread.get("replies") or []
    return [node.get("post") for node in replies if node.get("post")]


def expand_bluesky_seed(seed_doc, headers, posts_fields, edges_fields):
    return expand_seed(
        seed_doc,
        headers,
        platform_label="Bluesky",
        get_direct_replies=get_bluesky_direct_replies,
        normalise_reply=lambda reply_post, seed: normalise_bluesky_post(
            reply_post,
            post_fields=posts_fields,
            source_query=seed.get("source_query"),
            is_seed_post=False,
            collection_method="downward_reply_expansion",
        ),
        sleep_seconds=config.SLEEP_SECONDS,
        edges_fields=edges_fields,
    )


def harvest_bluesky_discussions(queries, posts_fields, edges_fields, max_seeds=None):
    headers = create_bluesky_headers()
    return harvest_discussions("Bluesky", queries, headers, search_bluesky_query, expand_bluesky_seed, posts_fields, edges_fields, max_seeds=max_seeds)


def get_mastodon_headers():
    if config.MASTODON_ACCESS_TOKEN:
        return {"Authorization": f"Bearer {config.MASTODON_ACCESS_TOKEN}"}
    log.info("Mastodon token is missing. Search may be limited or fail.")
    return {}


def search_mastodon_query(query, headers, posts_fields):
    endpoint = f"{config.MASTODON_BASE_URL.rstrip('/')}/api/v2/search"
    output = []
    max_id = None
    seen_cursors = set()
    start, _ = get_window()
    for _ in range(config.MAX_PAGES_PER_QUERY):
        params = {"q": query, "type": "statuses", "limit": config.PAGE_SIZE_MASTODON, "resolve": "true"}
        if max_id:
            params["max_id"] = max_id
        response = mastodon_get(endpoint, headers=headers, params=params)
        statuses = response.json().get("statuses") or []
        if not statuses:
            break
        for status in statuses:
            doc = normalise_mastodon_status(
                status,
                post_fields=posts_fields,
                source_query=query,
                server_domain=server_domain(config.MASTODON_BASE_URL),
                is_seed_post=True,
                collection_method="keyword_search_seed",
            )
            if doc.get("candidate_topics") and should_keep_doc(doc):
                output.append(apply_seed_type(doc))
        max_id = statuses[-1].get("id")
        if not max_id or max_id in seen_cursors:
            break
        seen_cursors.add(max_id)
        last_time = statuses[-1].get("created_at")
        if last_time and parse_utc_datetime(last_time) < parse_utc_datetime(start):
            break
        time.sleep(config.MASTODON_SLEEP_SECONDS)
    return output


def get_mastodon_direct_replies(status_id, headers):
    status_id = str(status_id).rstrip("/").rsplit("/", 1)[-1]
    endpoint = f"{config.MASTODON_BASE_URL.rstrip('/')}/api/v1/statuses/{status_id}/context"
    response = mastodon_get(endpoint, headers=headers)
    descendants = response.json().get("descendants") or []
    return [status for status in descendants if str(status.get("in_reply_to_id")) == str(status_id)]


def expand_mastodon_seed(seed_doc, headers, posts_fields, edges_fields):
    return expand_seed(
        seed_doc,
        headers,
        platform_label="Mastodon",
        get_direct_replies=get_mastodon_direct_replies,
        normalise_reply=lambda reply_status, seed: normalise_mastodon_status(
            reply_status,
            post_fields=posts_fields,
            source_query=seed.get("source_query"),
            server_domain=seed.get("server_domain"),
            is_seed_post=False,
            collection_method="downward_reply_expansion",
        ),
        sleep_seconds=config.MASTODON_SLEEP_SECONDS,
        edges_fields=edges_fields,
    )


def harvest_mastodon_discussions(queries, posts_fields, edges_fields, max_seeds=None):
    headers = get_mastodon_headers()
    return harvest_discussions("Mastodon", queries, headers, search_mastodon_query, expand_mastodon_seed, posts_fields, edges_fields, max_seeds=max_seeds)
