"""Reproducible synthetic demonstration, isolated from real research data."""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import random
from backend.common.es import get_client, bulk_documents
from backend.common.settings import index_name
from backend.common.time import utc_now
from backend.data_process.sentiment import SentimentModel
from backend.data_process.cloud_sentiment.cloud_sentiment_pipeline import process_targets, batched
from backend.data_process.cloud_sentiment.news_sentiment_pipeline import run_pipeline as process_news, parse_args as news_args
from database.migrate import migrate

START = "2026-08-01"
END = "2026-09-29"


def documents(days=60, per_day=12, seed=42):
    randomizer = random.Random(seed)
    for day in range(days):
        stamp = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)+timedelta(days=day)
        for n in range(per_day):
            topic = ("fuel_price", "ev", "public_transport")[n % 3]
            subject = {"fuel_price": "Petrol prices", "ev": "Electric cars", "public_transport": "Melbourne trams"}[topic]
            tone = randomizer.choice(("are excellent and wonderful.", "are awful and terrible.", "were discussed today."))
            identifier = f"demo-{day}-{n}"
            parent = f"demo-{day}-{n-1}" if n % 4 == 3 else None
            topics = [] if parent else [topic]
            yield {"doc_id": identifier, "post_id": identifier, "platform": "bluesky" if n % 2 else "mastodon",
                "author_id_hash": f"synthetic-author-{n}", "created_at": stamp.isoformat(),
                "fetched_at": "2026-09-30T00:00:00Z", "raw_text": "exactly" if parent else f"{subject} {tone}",
                "candidate_topics": topics, "direct_candidate_topics": topics,
                "parent_post_id": parent, "thread_root_id": parent or identifier, "is_reply": bool(parent),
                "dataset_kind": "synthetic", "source_dataset": "seeded_demo_v1", "schema_version": 2}


def seed(es=None, transformer=False):
    prefix = os.getenv("ES_INDEX_PREFIX", "")
    if not prefix.startswith("demo_"):
        raise ValueError("Demo requires a dedicated ES_INDEX_PREFIX beginning with demo_")
    es = es or get_client()
    migrate(es)
    rows = list(documents())
    for group in batched(rows, 100):
        bulk_documents("social_discussion_posts_raw", group, client=es)
    es.indices.refresh(index=index_name("social_discussion_posts_raw"))
    model = SentimentModel(use_transformers=transformer)
    for group in batched(rows, 100):
        processed = process_targets(group, es, model)
        bulk_documents("social_posts_processed", processed, client=es)
    oils, volumes, news = [], [], []
    previous = 80
    for day in range(60):
        stamp = (date(2026, 8, 1)+timedelta(days=day)).isoformat()
        price = 80 + 8*math.sin(day/8) + day*0.05
        oils.append({"id": "demo-brent-"+stamp, "date": stamp, "ticker": "BRENT",
                     "price": price, "daily_return_pct": (price/previous-1)*100,
                     "dataset_kind": "synthetic", "fetched_at": "2026-09-30T00:00:00Z"})
        previous = price
        volumes.append({"id": "demo-volume-"+stamp, "date": stamp, "keyword": "iran_hormuz",
                        "volume": 100+day % 13, "share_percent": 1+day % 10/10,
                        "dataset_kind": "synthetic", "fetched_at": "2026-09-30T00:00:00Z"})
        news.append({"id": "demo-news-"+stamp, "time": stamp+"T10:00:00Z",
                     "title": "Synthetic article: energy and transport",
                     "text": "Transport services are excellent." if day % 2 else "Rising fuel costs are terrible.",
                     "url": "https://example.invalid/demo/"+stamp, "source_domain": "example.invalid",
                     "fetched_at": "2026-09-30T00:00:00Z", "dataset_kind": "synthetic"})
    for index, docs in (("oil_prices_raw", oils), ("gdelt_news_volume_raw", volumes), ("gdelt_news_raw", news)):
        bulk_documents(index, docs, "id", client=es)
        es.indices.refresh(index=index_name(index))
    process_news(news_args(["--no-transformers"]), es=es, model=model)
    for index in ("social_posts_processed", "news_processed"):
        es.indices.refresh(index=index_name(index))
    from backend.oil_sentiment_corr.compute import run as compute
    compute(START, END, es, dataset_kind="synthetic", model_name=model.model_name)
    es.indices.refresh(index=index_name("oil_sentiment_corr_results"))
    return {"dataset_kind": "synthetic", "social_records": len(rows), "news_records": len(news),
            "start": START, "end": END, "model": model.model_name, "index_prefix": prefix}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--transformer", action="store_true")
    parser.add_argument("--port", type=int, default=9090)
    args = parser.parse_args()
    os.environ.setdefault("ES_HOST", "http://127.0.0.1:19200")
    os.environ.setdefault("ES_ALLOW_ANONYMOUS", "true")
    os.environ.setdefault("ES_INDEX_PREFIX", "demo_v2_")
    print(json.dumps(seed(transformer=args.transformer)), flush=True)
    if args.serve:
        from backend.api.app import create_app
        create_app().run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
