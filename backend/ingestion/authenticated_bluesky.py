"""Read-only AppView requests through the account's authenticated Bluesky PDS.

Tokens stay in memory; requests never follow redirects carrying credentials.
429 responses use bounded backoff. Error messages contain no response body/key.
"""
import time
import requests
from backend.common.settings import secret


class SourceError(RuntimeError):
    pass


class BlueskyReader:
    BASE = "https://bsky.social"

    def __init__(self, *, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.sleep = sleep
        self.token = None
        self.authenticated_at = 0.0

    def authenticate(self):
        handle, password = secret("BLUESKY_HANDLE"), secret("BLUESKY_APP_PASSWORD")
        if not handle or not password:
            raise SourceError("Bluesky handle and App Password are required")
        try:
            r = self.session.post(self.BASE + "/xrpc/com.atproto.server.createSession",
                json={"identifier": handle.strip(), "password": password.strip()},
                timeout=(10, 30), allow_redirects=False)
        except requests.RequestException as exc:
            raise SourceError("Bluesky authentication transport error") from exc
        if r.status_code != 200:
            raise SourceError(f"Bluesky authentication HTTP {r.status_code}")
        self.token = r.json().get("accessJwt")
        if not self.token:
            raise SourceError("Bluesky authentication returned no access token")
        self.authenticated_at = time.monotonic()

    def search(self, params):
        if not self.token or time.monotonic() - self.authenticated_at > 90 * 60:
            self.authenticate()
        for attempt in range(4):
            try:
                r = self.session.get(self.BASE + "/xrpc/app.bsky.feed.searchPosts",
                    params=params, headers={"Authorization": "Bearer " + self.token,
                    "atproto-proxy": "did:web:api.bsky.app#bsky_appview"},
                    timeout=(10, 30), allow_redirects=False)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise SourceError("Bluesky search transport error") from exc
                self.sleep(2 ** attempt)
                continue
            if r.status_code == 401 and attempt == 0:
                self.authenticate()
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                wait = r.headers.get("Retry-After", "")
                self.sleep(min(60, max(2 ** (attempt + 1), int(wait) if wait.isdigit() else 0)))
                continue
            if r.status_code != 200:
                raise SourceError(f"Bluesky search HTTP {r.status_code}")
            result = r.json()
            if not isinstance(result.get("posts"), list):
                raise SourceError("Bluesky search schema mismatch")
            return result
        raise SourceError("Bluesky search retry budget exhausted")
