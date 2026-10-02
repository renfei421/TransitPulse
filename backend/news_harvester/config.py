"""Configuration for direct-to-Elasticsearch GDELT news harvester.

Strict two-index version.

Only two Elasticsearch indexes are used:
- gdelt_news_volume_raw
- gdelt_news_raw

Only the requested output fields are written to Elasticsearch.
"""

import os
from datetime import date, timedelta
from backend.common.settings import secret

ES_HOST = os.getenv("ES_HOST", "https://localhost:9200")
ES_USER = os.getenv("ES_USER", "elastic")
ES_PASSWORD = secret("ES_PASSWORD")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

GDELT_DOC_API = os.getenv("GDELT_DOC_API", "https://api.gdeltproject.org/api/v2/doc/doc")
GDELT_QUERY = os.getenv(
    "GDELT_QUERY",
    '(Iran OR Iranian OR Hormuz OR "Strait of Hormuz") sourcelang:eng',
)
GDELT_KEYWORD = os.getenv("GDELT_KEYWORD", "iran_hormuz")
GDELT_START_DATE = os.getenv("GDELT_START_DATE", (date.today()-timedelta(days=30)).isoformat())
GDELT_END_DATE = os.getenv("GDELT_END_DATE", "")

TARGET_ARTICLES = int(os.getenv("TARGET_ARTICLES", "5000"))
MIN_PER_DAY = int(os.getenv("MIN_PER_DAY", "10"))
TIME_BUDGET_MINUTES = int(os.getenv("TIME_BUDGET_MINUTES", "480"))

GDELT_SLEEP_SECONDS = float(os.getenv("GDELT_SLEEP_SECONDS", "10"))
ARTICLE_SLEEP_SECONDS = float(os.getenv("ARTICLE_SLEEP_SECONDS", "3"))
ARTICLE_TIMEOUT_SECONDS = int(os.getenv("ARTICLE_TIMEOUT_SECONDS", "12"))
MIN_TEXT_CHARS = int(os.getenv("MIN_TEXT_CHARS", "400"))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
INITIAL_BACKOFF_SECONDS = float(os.getenv("INITIAL_BACKOFF_SECONDS", "30"))
MAX_BACKOFF_SECONDS = float(os.getenv("MAX_BACKOFF_SECONDS", "300"))

VOLUME_INDEX = os.getenv("GDELT_VOLUME_INDEX", "gdelt_news_volume_raw")
NEWS_INDEX = os.getenv("GDELT_NEWS_INDEX", "gdelt_news_raw")
