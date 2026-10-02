"""
Configuration for the post-comment harvester.

Credentials come from environment variables or mounted Kubernetes Secret files.
"""

import os
from backend.common.settings import secret

ES_HOST = os.getenv("ES_HOST", "https://elasticsearch-es-http.elastic.svc:9200")
ES_USER = os.getenv("ES_USER", "elastic")
ES_PASSWORD = secret("ES_PASSWORD")

STUDY_START = os.getenv("STUDY_START", "2026-02-14T00:00:00Z")
STUDY_END = os.getenv("STUDY_END", "2026-05-04T23:59:59Z")

BLUESKY_BASE_URL = os.getenv("BLUESKY_BASE_URL", "https://bsky.social")
BLUESKY_PUBLIC_BASE_URL = os.getenv("BLUESKY_PUBLIC_BASE_URL", "https://public.api.bsky.app")
BLUESKY_HANDLE = secret("BLUESKY_HANDLE")
BLUESKY_APP_PASSWORD = secret("BLUESKY_APP_PASSWORD")

MASTODON_BASE_URL = os.getenv("MASTODON_BASE_URL", "https://mastodon.social")
MASTODON_ACCESS_TOKEN = secret("MASTODON_ACCESS_TOKEN")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
PAGE_SIZE_BLUESKY = int(os.getenv("PAGE_SIZE_BLUESKY", "80"))
PAGE_SIZE_MASTODON = int(os.getenv("PAGE_SIZE_MASTODON", "20"))
MAX_PAGES_PER_QUERY = int(os.getenv("MAX_PAGES_PER_QUERY", "10"))
MAX_SEEDS_PER_RUN = int(os.getenv("MAX_SEEDS_PER_RUN", "20"))
DEFAULT_QUERY_LIMIT = int(os.getenv("DEFAULT_QUERY_LIMIT", "12"))

SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "0.2"))
MASTODON_SLEEP_SECONDS = float(os.getenv("MASTODON_SLEEP_SECONDS", "1.5"))

MASTODON_MAX_RETRIES = int(os.getenv("MASTODON_MAX_RETRIES", "3"))
MASTODON_RETRY_BACKOFF_SECONDS = float(os.getenv("MASTODON_RETRY_BACKOFF_SECONDS", "30"))
MASTODON_RETRY_AFTER_CAP_SECONDS = float(os.getenv("MASTODON_RETRY_AFTER_CAP_SECONDS", "120"))

MAIN_POST_MAX_DEPTH = int(os.getenv("MAIN_POST_MAX_DEPTH", "3"))
MAIN_POST_MAX_REPLIES = int(os.getenv("MAIN_POST_MAX_REPLIES", "1500"))
MAIN_POST_MAX_API_CALLS = int(os.getenv("MAIN_POST_MAX_API_CALLS", "500"))

REPLY_SEED_MAX_DEPTH = int(os.getenv("REPLY_SEED_MAX_DEPTH", "2"))
REPLY_SEED_MAX_REPLIES = int(os.getenv("REPLY_SEED_MAX_REPLIES", "500"))
REPLY_SEED_MAX_API_CALLS = int(os.getenv("REPLY_SEED_MAX_API_CALLS", "100"))

POSTS_INDEX = "social_discussion_posts_raw"
EDGES_INDEX = "social_discussion_edges_raw"
RUNS_INDEX = "post_comment_crawler_runs_raw"

EVENTS = [
    {"event_id": "iran_oil_shock", "event_name": "Iran oil shock", "event_date": "2026-02-28T00:00:00Z"},
    {"event_id": "vic_free_pt_start", "event_name": "Victoria free public transport starts", "event_date": "2026-03-31T00:00:00Z"},
    {"event_id": "vic_free_pt_extended", "event_name": "Victoria free public transport extended", "event_date": "2026-04-19T00:00:00Z"},
]

# No import-time private Python configuration: mounted secrets are data, not code.
