"""Compatibility name for the shared authenticated Elasticsearch client."""
from backend.common.es import get_client
from backend.common.logging import get_logger

get_es_client = get_client


def test_connection():
    try:
        return bool(get_client().ping())
    except Exception:
        get_logger("api").exception("elasticsearch_unavailable")
        return False
