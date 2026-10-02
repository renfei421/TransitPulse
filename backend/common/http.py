"""Bounded retry for idempotent external reads, including Retry-After dates."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
import requests

from .logging import get_logger

log = get_logger("http")


def retry_after(value: str | None, default: float, cap: float = 120) -> float:
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = default
    return max(0, min(delay, cap))


def get(url, *, attempts=4, timeout=30, sleep=time.sleep, session=None, **kwargs):
    session = session or requests
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=timeout, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            response.raise_for_status()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt + 1 == attempts or (status and status not in {429, 500, 502, 503, 504}):
                raise
            headers = getattr(getattr(exc, "response", None), "headers", {})
            delay = retry_after(headers.get("Retry-After"), min(2**attempt, 30) + random.random())
            log.warning("upstream_retry", extra={"fields":{"attempt":attempt+1, "status":status, "delay_s":delay}})
            sleep(delay)
    raise ValueError("attempts must be positive")
