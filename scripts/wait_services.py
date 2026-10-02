"""Bounded CI service readiness; unavailable configured services fail the job."""
import os
import time
import requests
import redis


def main():
    deadline = time.monotonic()+120
    while time.monotonic() < deadline:
        try:
            requests.get(os.environ["TEST_ES_URL"]+"/_cluster/health", timeout=3).raise_for_status()
            assert redis.Redis.from_url(os.environ["TEST_REDIS_URL"]).ping()
            return
        except (requests.RequestException, redis.RedisError, AssertionError):
            time.sleep(2)
    raise RuntimeError("Elasticsearch/Redis did not become ready within 120 seconds")


if __name__ == "__main__":
    main()
