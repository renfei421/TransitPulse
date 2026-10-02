"""Opt-in real services; each test owns an isolated prefix and cleans only it."""
import os
from uuid import uuid4
import pytest


@pytest.fixture
def es_service(monkeypatch):
    url = os.getenv("TEST_ES_URL")
    if not url:
        pytest.skip("Set TEST_ES_URL for real Elasticsearch integration tests")
    monkeypatch.setenv("ES_HOST", url)
    monkeypatch.setenv("ES_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("ES_PASSWORD", raising=False)
    monkeypatch.delenv("ES_PASS", raising=False)
    prefix = "test_transport_" + uuid4().hex[:12] + "_"
    monkeypatch.setenv("ES_INDEX_PREFIX", prefix)
    from backend.common.es import get_client
    from database.migrate import migrate, definitions
    es = get_client()
    assert es.ping(), "TEST_ES_URL was provided but Elasticsearch is unavailable"
    migrate(es)
    yield es
    # Explicit owned index names only: no wildcard deletion.
    for logical in definitions():
        es.indices.delete(index=prefix+logical, ignore_unavailable=True)
    es.close()


@pytest.fixture
def redis_queue():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_REDIS_URL for real Redis integration tests")
    import redis
    from backend.ingestion.queue import WorkQueue
    client = redis.Redis.from_url(url, decode_responses=True)
    assert client.ping()
    prefix = "test_transport_" + uuid4().hex[:12]
    queue = WorkQueue(client, prefix=prefix)
    queue.ensure_group()
    yield queue
    keys = list(client.scan_iter(match=prefix+":*"))
    if keys:
        client.delete(*keys)
    client.close()
