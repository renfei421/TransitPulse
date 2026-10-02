"""Normalisation helpers for Bluesky and Mastodon posts."""

import hashlib
import html
import re
from datetime import datetime, timezone

from backend.parallel_harvester import config
from backend.parallel_harvester.topics import match_topics, infer_region


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def make_doc_id(platform, post_id):
    return stable_hash(f"{platform}|{post_id}")


def make_edge_id(seed_post_id, descendant_post_id):
    return stable_hash(f"{seed_post_id}|{descendant_post_id}")


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def tag_nearest_event(created_at):
    created_dt = parse_datetime(created_at)
    if created_dt is None:
        return {"nearest_event_id": None, "nearest_event_name": None, "days_from_event": None, "event_period": "unknown"}

    best_event = None
    best_delta = None
    for event in config.EVENTS:
        event_dt = parse_datetime(event["event_date"])
        if event_dt is None:
            continue
        delta = int((created_dt.date() - event_dt.date()).days)
        if best_delta is None or abs(delta) < abs(best_delta):
            best_delta = delta
            best_event = event

    if best_event is None:
        return {"nearest_event_id": None, "nearest_event_name": None, "days_from_event": None, "event_period": "unknown"}

    if -14 <= best_delta < 0:
        period = "pre_event_14d"
    elif 0 <= best_delta <= 14:
        period = "post_event_14d"
    elif 15 <= best_delta <= 30:
        period = "post_event_30d"
    else:
        period = "outside_event_window"

    return {
        "nearest_event_id": best_event["event_id"],
        "nearest_event_name": best_event["event_name"],
        "days_from_event": best_delta,
        "event_period": period,
    }

BLUESKY_EXTRACTORS = {
    "doc_id":                  lambda post, ctx: make_doc_id("bluesky", post.get("uri")),
    "platform":                lambda post, ctx: "bluesky",
    "server_domain":           lambda post, ctx: None,
    "post_id":                 lambda post, ctx: post.get("uri"),
    "post_url":                lambda post, ctx: post.get("uri"),
    "author_id_hash":          lambda post, ctx: stable_hash(ctx["author"].get("did") or ctx["author"].get("handle") or ""),
    "created_at":              lambda post, ctx: ctx["created_at"],
    "fetched_at":              lambda post, ctx: utc_now_iso(),
    "raw_text":                lambda post, ctx: ctx["raw_text"],
    "lang":                    lambda post, ctx: ctx["lang"],
    "source_query":            lambda post, ctx: ctx["source_query"],
    "matched_keywords":        lambda post, ctx: ctx["matched_keywords"],
    "candidate_topics":        lambda post, ctx: ctx["candidate_topics"],
    "topic_count":             lambda post, ctx: len(ctx["candidate_topics"]),
    "is_seed_post":            lambda post, ctx: ctx["is_seed_post"],
    "seed_type":               lambda post, ctx: None,
    "is_reply":                lambda post, ctx: bool(ctx["parent_post_id"]),
    "parent_post_id":          lambda post, ctx: ctx["parent_post_id"],
    "thread_root_id":          lambda post, ctx: ctx["thread_root_id"],
    "collection_method":       lambda post, ctx: ctx["collection_method"],
    "collection_methods":      lambda post, ctx: [ctx["collection_method"]] if ctx["collection_method"] else [],
    "reply_has_keyword":       lambda post, ctx: bool(ctx["candidate_topics"]),
    "direct_candidate_topics": lambda post, ctx: ctx["candidate_topics"],
    "like_count":              lambda post, ctx: int(post.get("likeCount") or 0),
    "reply_count":             lambda post, ctx: int(post.get("replyCount") or 0),
    "repost_count":            lambda post, ctx: int(post.get("repostCount") or 0),
    "inferred_region":         lambda post, ctx: ctx["region"]["inferred_region"],
    "region_confidence":       lambda post, ctx: ctx["region"]["region_confidence"],
    "region_source":           lambda post, ctx: ctx["region"]["region_source"],
    "nearest_event_id":        lambda post, ctx: ctx["event"]["nearest_event_id"],
    "nearest_event_name":      lambda post, ctx: ctx["event"]["nearest_event_name"],
    "days_from_event":         lambda post, ctx: ctx["event"]["days_from_event"],
    "event_period":            lambda post, ctx: ctx["event"]["event_period"],
}

MASTODON_EXTRACTORS = {
    "doc_id":                  lambda status, ctx: make_doc_id("mastodon", ctx["post_id"]),
    "platform":                lambda status, ctx: "mastodon",
    "server_domain":           lambda status, ctx: ctx["server_domain"],
    "post_id":                 lambda status, ctx: ctx["post_id"],
    "post_url":                lambda status, ctx: status.get("url") or status.get("uri"),
    "author_id_hash":          lambda status, ctx: stable_hash(str(ctx["server_domain"])+"|"+str(ctx["account"].get("id") or ctx["account"].get("acct") or "")),
    "created_at":              lambda status, ctx: ctx["created_at"],
    "fetched_at":              lambda status, ctx: utc_now_iso(),
    "raw_text":                lambda status, ctx: ctx["raw_text"],
    "lang":                    lambda status, ctx: status.get("language"),
    "source_query":            lambda status, ctx: ctx["source_query"],
    "matched_keywords":        lambda status, ctx: ctx["matched_keywords"],
    "candidate_topics":        lambda status, ctx: ctx["candidate_topics"],
    "topic_count":             lambda status, ctx: len(ctx["candidate_topics"]),
    "is_seed_post":            lambda status, ctx: ctx["is_seed_post"],
    "seed_type":               lambda status, ctx: None,
    "is_reply":                lambda status, ctx: bool(ctx["parent_post_id"]),
    "parent_post_id":          lambda status, ctx: ctx["parent_post_id"],
    "thread_root_id":          lambda status, ctx: None,
    "collection_method":       lambda status, ctx: ctx["collection_method"],
    "collection_methods":      lambda status, ctx: [ctx["collection_method"]] if ctx["collection_method"] else [],
    "reply_has_keyword":       lambda status, ctx: bool(ctx["candidate_topics"]),
    "direct_candidate_topics": lambda status, ctx: ctx["candidate_topics"],
    "like_count":              lambda status, ctx: int(status.get("favourites_count") or 0),
    "reply_count":             lambda status, ctx: int(status.get("replies_count") or 0),
    "repost_count":            lambda status, ctx: int(status.get("reblogs_count") or 0),
    "inferred_region":         lambda status, ctx: ctx["region"]["inferred_region"],
    "region_confidence":       lambda status, ctx: ctx["region"]["region_confidence"],
    "region_source":           lambda status, ctx: ctx["region"]["region_source"],
    "nearest_event_id":        lambda status, ctx: ctx["event"]["nearest_event_id"],
    "nearest_event_name":      lambda status, ctx: ctx["event"]["nearest_event_name"],
    "days_from_event":         lambda status, ctx: ctx["event"]["days_from_event"],
    "event_period":            lambda status, ctx: ctx["event"]["event_period"],
}


def normalise_bluesky_post(post, post_fields=None, source_query=None, is_seed_post=False, collection_method=None):
    post_id = post.get("uri")
    record = post.get("record") or {}
    author = post.get("author") or {}
    raw_text = record.get("text") or ""
    candidate_topics, matched_keywords = match_topics(raw_text)
    reply_info = record.get("reply") or {}
    parent_info = reply_info.get("parent") or {}
    root_info = reply_info.get("root") or {}
    parent_post_id = parent_info.get("uri")
    thread_root_id = root_info.get("uri") or post_id
    lang_values = record.get("langs") or []
    lang = lang_values[0] if lang_values else None
    created_at = record.get("createdAt") or post.get("indexedAt")

    ctx = {
        "record": record,
        "author": author,
        "raw_text": raw_text,
        "candidate_topics": candidate_topics,
        "matched_keywords": matched_keywords,
        "parent_post_id": parent_post_id,
        "thread_root_id": thread_root_id,
        "lang": lang,
        "created_at": created_at,
        "source_query": source_query,
        "is_seed_post": bool(is_seed_post),
        "collection_method": collection_method,
        "region": infer_region(raw_text),
        "event": tag_nearest_event(created_at),
    }

    fields = post_fields or BLUESKY_EXTRACTORS.keys()
    return {field: BLUESKY_EXTRACTORS[field](post, ctx) for field in fields if field in BLUESKY_EXTRACTORS}


def normalise_mastodon_status(status, post_fields=None, source_query=None, server_domain=None, is_seed_post=False, collection_method=None):
    status_id = str(status.get("id"))
    def scoped(value):
        return f"https://{server_domain}/statuses/{value}" if server_domain else str(value)
    raw_text = strip_html(status.get("content") or "")
    account = status.get("account") or {}
    parent_post_id = status.get("in_reply_to_id")
    parent_post_id = scoped(parent_post_id) if parent_post_id is not None else None
    hashtags = [tag.get("name", "") for tag in (status.get("tags") or []) if isinstance(tag, dict)]
    candidate_topics, matched_keywords = match_topics(raw_text, hashtags)
    created_at = status.get("created_at")

    ctx = {
        "account": account,
        "post_id": scoped(status_id),
        "raw_text": raw_text,
        "candidate_topics": candidate_topics,
        "matched_keywords": matched_keywords,
        "parent_post_id": parent_post_id,
        "created_at": created_at,
        "source_query": source_query,
        "server_domain": server_domain,
        "is_seed_post": bool(is_seed_post),
        "collection_method": collection_method,
        "region": infer_region(raw_text),
        "event": tag_nearest_event(created_at),
    }

    fields = post_fields or MASTODON_EXTRACTORS.keys()
    return {field: MASTODON_EXTRACTORS[field](status, ctx) for field in fields if field in MASTODON_EXTRACTORS}


def apply_seed_type(doc):
    doc["seed_type"] = "reply_seed" if doc.get("is_reply") else "main_post_seed"
    return doc


def make_edge(seed_doc, descendant_doc, depth_from_seed, edge_fields=None):
    edge = {
        "edge_id": make_edge_id(seed_doc["post_id"], descendant_doc["post_id"]),
        "platform": seed_doc["platform"],
        "seed_doc_id": seed_doc["doc_id"],
        "seed_post_id": seed_doc["post_id"],
        "seed_type": seed_doc.get("seed_type"),
        "seed_query": seed_doc.get("source_query"),
        "seed_candidate_topics": seed_doc.get("candidate_topics") or [],
        "seed_created_at": seed_doc.get("created_at"),
        "descendant_doc_id": descendant_doc["doc_id"],
        "descendant_post_id": descendant_doc["post_id"],
        "parent_post_id": descendant_doc.get("parent_post_id"),
        "thread_root_id": descendant_doc.get("thread_root_id"),
        "depth_from_seed": depth_from_seed,
        "descendant_has_keyword": descendant_doc.get("reply_has_keyword"),
        "descendant_direct_candidate_topics": descendant_doc.get("direct_candidate_topics") or [],
        "inherited_candidate_topics": seed_doc.get("candidate_topics") or [],
        "expanded_at": utc_now_iso(),
        "collection_method": "bounded_downward_reply_expansion",
    }
    if edge_fields:
        return {k: v for k, v in edge.items() if k in edge_fields}
    return edge
