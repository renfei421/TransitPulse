"""Unit tests for the parallel_harvester package.

Run with:
    cd test/test_harvester
    pytest test_harvester.py -v
"""

import sys
import os
from datetime import timezone
from unittest.mock import MagicMock, patch

import pytest

HARVESTER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "parallel_harvester"))
sys.path.insert(0, HARVESTER_DIR)


# ===========================================================================
# topics.py — pure functions, no I/O
# ===========================================================================

class TestMatchTopics:
    def setup_method(self):
        from backend.parallel_harvester.topics import match_topics
        self.match = match_topics

    def test_single_topic(self):
        topics, kws = self.match("petrol price in Melbourne")
        assert "fuel_price" in topics
        assert "petrol price" in kws

    def test_multiple_topics(self):
        topics, _ = self.match("EV charging and public transport fare")
        assert "ev" in topics
        assert "public_transport" in topics

    def test_no_match(self):
        topics, kws = self.match("the weather is nice today")
        assert topics == []
        assert kws == []

    def test_case_insensitive(self):
        topics, _ = self.match("TESLA is an ELECTRIC VEHICLE")
        assert "ev" in topics

    def test_topics_sorted(self):
        topics, _ = self.match("petrol myki EV")
        assert topics == sorted(topics)

    def test_keywords_deduplicated(self):
        _, kws = self.match("tram tram train train")
        assert len(kws) == len(set(kws))


class TestInferRegion:
    def setup_method(self):
        from backend.parallel_harvester.topics import infer_region
        self.infer = infer_region

    def test_victoria(self):
        r = self.infer("myki card is free in Melbourne today")
        assert r["inferred_region"] == "Victoria_or_Melbourne"
        assert r["region_confidence"] > 0

    def test_nsw(self):
        r = self.infer("opal card on the Sydney trains")
        assert r["inferred_region"] == "New_South_Wales_or_Sydney"

    def test_no_region(self):
        r = self.infer("some random text with no location")
        assert r["inferred_region"] is None
        assert r["region_confidence"] == 0.0
        assert r["region_source"] is None

    def test_australia_generic(self):
        r = self.infer("fuel prices are rising across Australia")
        assert r["inferred_region"] == "Australia"

    def test_word_boundary_no_false_positive(self):
        # "act" inside "contact" should NOT match ACT_or_Canberra
        r = self.infer("please contact us for details")
        assert r["inferred_region"] != "ACT_or_Canberra"


class TestGenerateQueries:
    def setup_method(self):
        from backend.parallel_harvester.topics import generate_queries
        self.queries = generate_queries()

    def test_returns_nonempty_list(self):
        assert isinstance(self.queries, list)
        assert len(self.queries) > 0

    def test_no_duplicates(self):
        lowered = [q.lower() for q in self.queries]
        assert len(lowered) == len(set(lowered))

    def test_all_stripped_strings(self):
        for q in self.queries:
            assert isinstance(q, str)
            assert q.strip() == q


class TestSanitiseSearchQuery:
    def setup_method(self):
        from backend.parallel_harvester.topics import sanitise_search_query
        self.san = sanitise_search_query

    def test_removes_quotes_from_short_tokens(self):
        assert self.san('"EV" Australia') == "EV Australia"
        assert self.san('"BYD" Melbourne') == "BYD Melbourne"

    def test_preserves_long_quoted_phrases(self):
        result = self.san('"electric vehicle" Australia')
        assert '"electric vehicle"' in result

    def test_collapses_extra_spaces(self):
        assert self.san("fuel   price  Australia") == "fuel price Australia"


# ===========================================================================
# normalize.py — pure functions
# ===========================================================================

class TestHashHelpers:
    def setup_method(self):
        import normalize as n
        self.n = n

    def test_stable_hash_deterministic(self):
        assert self.n.stable_hash("hello") == self.n.stable_hash("hello")

    def test_stable_hash_different_inputs(self):
        assert self.n.stable_hash("a") != self.n.stable_hash("b")

    def test_make_doc_id_is_sha256_hex(self):
        doc_id = self.n.make_doc_id("bluesky", "at://did:plc:abc/123")
        assert isinstance(doc_id, str) and len(doc_id) == 64

    def test_make_edge_id_is_sha256_hex(self):
        eid = self.n.make_edge_id("post_a", "post_b")
        assert isinstance(eid, str) and len(eid) == 64


class TestStripHtml:
    def setup_method(self):
        from backend.parallel_harvester.normalize import strip_html
        self.strip = strip_html

    def test_removes_tags(self):
        assert self.strip("<p>Hello <b>world</b></p>") == "Hello world"

    def test_unescapes_entities(self):
        assert self.strip("Tom &amp; Jerry") == "Tom & Jerry"

    def test_collapses_whitespace(self):
        assert self.strip("<p>  too   much  space  </p>") == "too much space"

    def test_none_input(self):
        assert self.strip(None) == ""


class TestParseDatetime:
    def setup_method(self):
        from backend.parallel_harvester.normalize import parse_datetime
        self.parse = parse_datetime

    def test_parses_z_suffix(self):
        dt = self.parse("2026-03-01T12:00:00Z")
        assert dt is not None and dt.year == 2026

    def test_parses_offset(self):
        dt = self.parse("2026-03-01T12:00:00+10:00")
        assert dt is not None and dt.tzinfo is not None

    def test_none_returns_none(self):
        assert self.parse(None) is None

    def test_empty_returns_none(self):
        assert self.parse("") is None

    def test_naive_gets_utc(self):
        dt = self.parse("2026-03-01T12:00:00")
        assert dt.tzinfo == timezone.utc


class TestTagNearestEvent:
    def setup_method(self):
        from backend.parallel_harvester.normalize import tag_nearest_event
        self.tag = tag_nearest_event

    def test_pre_event_window(self):
        # 8 days before iran_oil_shock (2026-02-28)
        r = self.tag("2026-02-20T00:00:00Z")
        assert r["nearest_event_id"] == "iran_oil_shock"
        assert r["event_period"] == "pre_event_14d"
        assert r["days_from_event"] < 0

    def test_post_event_14d(self):
        # 5 days after iran_oil_shock
        r = self.tag("2026-03-05T00:00:00Z")
        assert r["nearest_event_id"] == "iran_oil_shock"
        assert r["event_period"] == "post_event_14d"
        assert r["days_from_event"] >= 0

    def test_outside_window(self):
        r = self.tag("2026-01-01T00:00:00Z")
        assert r["event_period"] == "outside_event_window"

    def test_none_input(self):
        r = self.tag(None)
        assert r["nearest_event_id"] is None
        assert r["event_period"] == "unknown"


class TestNormaliseBlueskyPost:
    def setup_method(self):
        from backend.parallel_harvester.normalize import normalise_bluesky_post
        self.norm = normalise_bluesky_post

    def _post(self, text="petrol price in Melbourne", uri="at://did:plc:abc/post/1"):
        return {
            "uri": uri,
            "author": {"did": "did:plc:abc", "handle": "user.bsky.social"},
            "record": {"text": text, "createdAt": "2026-03-01T10:00:00Z", "langs": ["en"]},
            "likeCount": 5,
            "replyCount": 2,
            "repostCount": 1,
        }

    def test_doc_id_is_hash(self):
        assert len(self.norm(self._post())["doc_id"]) == 64

    def test_platform(self):
        assert self.norm(self._post())["platform"] == "bluesky"

    def test_topics_extracted(self):
        doc = self.norm(self._post("petrol price in Melbourne"))
        assert "fuel_price" in doc["candidate_topics"]

    def test_is_seed_post_true(self):
        assert self.norm(self._post(), is_seed_post=True)["is_seed_post"] is True

    def test_is_seed_post_false(self):
        assert self.norm(self._post(), is_seed_post=False)["is_seed_post"] is False

    def test_reply_detection(self):
        post = self._post()
        post["record"]["reply"] = {
            "parent": {"uri": "at://did:plc:abc/post/0"},
            "root": {"uri": "at://did:plc:abc/post/root"},
        }
        doc = self.norm(post)
        assert doc["is_reply"] is True
        assert doc["parent_post_id"] == "at://did:plc:abc/post/0"

    def test_not_a_reply(self):
        doc = self.norm(self._post())
        assert doc["is_reply"] is False
        assert doc["parent_post_id"] is None

    def test_field_filtering(self):
        doc = self.norm(self._post(), post_fields=["doc_id", "platform"])
        assert set(doc.keys()) == {"doc_id", "platform"}

    def test_counts_are_integers(self):
        doc = self.norm(self._post())
        assert isinstance(doc["like_count"], int)
        assert isinstance(doc["reply_count"], int)
        assert isinstance(doc["repost_count"], int)


class TestNormaliseMastodonStatus:
    def setup_method(self):
        from backend.parallel_harvester.normalize import normalise_mastodon_status
        self.norm = normalise_mastodon_status

    def _status(self, content="<p>fuel prices in Melbourne</p>", status_id="12345"):
        return {
            "id": status_id,
            "content": content,
            "created_at": "2026-03-01T10:00:00Z",
            "url": "https://mastodon.social/@user/12345",
            "account": {"id": "99", "acct": "user@mastodon.social"},
            "favourites_count": 3,
            "replies_count": 1,
            "reblogs_count": 0,
            "in_reply_to_id": None,
            "language": "en",
        }

    def test_platform(self):
        assert self.norm(self._status())["platform"] == "mastodon"

    def test_html_stripped(self):
        doc = self.norm(self._status("<p>fuel prices in Melbourne</p>"))
        assert "<p>" not in doc["raw_text"]

    def test_topics_matched_after_strip(self):
        doc = self.norm(self._status("<p>petrol price in Melbourne</p>"))
        assert "fuel_price" in doc["candidate_topics"]

    def test_reply_detection(self):
        s = self._status()
        s["in_reply_to_id"] = "11111"
        doc = self.norm(s)
        assert doc["is_reply"] is True
        assert doc["parent_post_id"] == "11111"

    def test_not_a_reply(self):
        doc = self.norm(self._status())
        assert doc["is_reply"] is False
        assert doc["parent_post_id"] is None

    def test_counts_are_integers(self):
        doc = self.norm(self._status())
        assert isinstance(doc["like_count"], int)
        assert isinstance(doc["reply_count"], int)


class TestMakeEdge:
    def setup_method(self):
        from backend.parallel_harvester.normalize import make_edge
        self.make_edge = make_edge

    def _seed(self):
        return {
            "doc_id": "seed_doc",
            "post_id": "seed_post",
            "platform": "bluesky",
            "seed_type": "main_post_seed",
            "source_query": "EV Australia",
            "candidate_topics": ["ev"],
            "created_at": "2026-03-01T10:00:00Z",
        }

    def _desc(self):
        return {
            "doc_id": "desc_doc",
            "post_id": "desc_post",
            "parent_post_id": "seed_post",
            "thread_root_id": "seed_post",
            "reply_has_keyword": True,
            "direct_candidate_topics": ["ev"],
        }

    def test_edge_fields(self):
        edge = self.make_edge(self._seed(), self._desc(), depth_from_seed=1)
        assert edge["seed_post_id"] == "seed_post"
        assert edge["descendant_post_id"] == "desc_post"
        assert edge["depth_from_seed"] == 1

    def test_edge_id_is_hash(self):
        assert len(self.make_edge(self._seed(), self._desc(), depth_from_seed=1)["edge_id"]) == 64

    def test_field_filtering(self):
        edge = self.make_edge(self._seed(), self._desc(), depth_from_seed=1,
                              edge_fields=["edge_id", "platform"])
        assert set(edge.keys()) == {"edge_id", "platform"}


# ===========================================================================
# sources.py — mock network calls
# ===========================================================================

class TestInStudyWindow:
    def setup_method(self):
        from backend.parallel_harvester import config
        config.STUDY_START = "2026-02-14T00:00:00Z"
        config.STUDY_END = "2026-05-04T23:59:59Z"
        from backend.parallel_harvester.sources import in_study_window
        self.in_window = in_study_window

    def test_within(self):
        assert self.in_window("2026-03-15T12:00:00Z") is True

    def test_before(self):
        assert self.in_window("2026-01-01T00:00:00Z") is False

    def test_after(self):
        assert self.in_window("2026-06-01T00:00:00Z") is False

    def test_on_start_boundary(self):
        assert self.in_window("2026-02-14T00:00:00Z") is True

    def test_none(self):
        assert self.in_window(None) is False


class TestServerDomain:
    def test_strips_https(self):
        from backend.parallel_harvester.sources import server_domain
        assert server_domain("https://mastodon.social") == "mastodon.social"

    def test_strips_http_and_trailing_slash(self):
        from backend.parallel_harvester.sources import server_domain
        assert server_domain("http://example.org/") == "example.org"


class TestRetryAfterSeconds:
    def setup_method(self):
        from backend.parallel_harvester import config
        config.MASTODON_RETRY_AFTER_CAP_SECONDS = 120
        from backend.parallel_harvester.sources import retry_after_seconds
        self.retry = retry_after_seconds

    def test_uses_header(self):
        resp = MagicMock()
        resp.headers = {"Retry-After": "45"}
        assert self.retry(resp, default_seconds=30) == 45.0

    def test_falls_back_to_default(self):
        resp = MagicMock()
        resp.headers = {}
        assert self.retry(resp, default_seconds=30) == 30

    def test_capped_at_max(self):
        from backend.parallel_harvester import config
        config.MASTODON_RETRY_AFTER_CAP_SECONDS = 60
        resp = MagicMock()
        resp.headers = {"Retry-After": "9999"}
        assert self.retry(resp, default_seconds=30) == 60.0


class TestMastodonGet:
    def setup_method(self):
        from backend.parallel_harvester import config
        config.MASTODON_MAX_RETRIES = 2
        config.MASTODON_RETRY_BACKOFF_SECONDS = 0.01
        config.MASTODON_RETRY_AFTER_CAP_SECONDS = 120
        config.REQUEST_TIMEOUT = 5

    @patch("backend.parallel_harvester.sources.time.sleep")
    @patch("backend.parallel_harvester.sources.requests.get")
    def test_success_first_try(self, mock_get, mock_sleep):
        from backend.parallel_harvester.sources import mastodon_get
        ok = MagicMock(status_code=200)
        ok.raise_for_status = MagicMock()
        mock_get.return_value = ok

        assert mastodon_get("https://example.org/api", headers={}) is ok
        mock_sleep.assert_not_called()

    @patch("backend.parallel_harvester.sources.time.sleep")
    @patch("backend.parallel_harvester.sources.requests.get")
    def test_retries_on_429_then_succeeds(self, mock_get, mock_sleep):
        from backend.parallel_harvester.sources import mastodon_get
        limited = MagicMock(status_code=429)
        limited.headers = {}
        limited.raise_for_status = MagicMock(side_effect=Exception("429"))
        ok = MagicMock(status_code=200)
        ok.raise_for_status = MagicMock()
        mock_get.side_effect = [limited, ok]

        assert mastodon_get("https://example.org/api", headers={}) is ok
        assert mock_sleep.call_count == 1


class TestExpandSeed:
    def setup_method(self):
        from backend.parallel_harvester import config
        config.STUDY_START = "2026-01-01T00:00:00Z"
        config.STUDY_END = "2026-12-31T23:59:59Z"
        config.MAIN_POST_MAX_DEPTH = 2
        config.MAIN_POST_MAX_REPLIES = 100
        config.MAIN_POST_MAX_API_CALLS = 50
        config.REPLY_SEED_MAX_DEPTH = 2
        config.REPLY_SEED_MAX_REPLIES = 50
        config.REPLY_SEED_MAX_API_CALLS = 20

    def _seed(self):
        return {
            "post_id": "root",
            "doc_id": "root_doc",
            "platform": "bluesky",
            "seed_type": "main_post_seed",
            "source_query": "EV",
            "candidate_topics": ["ev"],
            "created_at": "2026-03-01T10:00:00Z",
        }

    def _reply_doc(self, post_id, parent_id="root"):
        return {
            "post_id": post_id,
            "doc_id": f"doc_{post_id}",
            "platform": "bluesky",
            "parent_post_id": parent_id,
            "thread_root_id": "root",
            "created_at": "2026-03-01T11:00:00Z",
            "reply_has_keyword": True,
            "direct_candidate_topics": ["ev"],
        }

    @patch("backend.parallel_harvester.sources.time.sleep")
    def test_returns_replies_and_edges(self, _sleep):
        from backend.parallel_harvester.sources import expand_seed
        replies_map = {"root": [{"post_id": "r1"}, {"post_id": "r2"}], "r1": [], "r2": []}

        result = expand_seed(
            seed_doc=self._seed(), headers={}, platform_label="Test",
            get_direct_replies=lambda pid, h: replies_map.get(pid, []),
            normalise_reply=lambda r, s: self._reply_doc(r["post_id"], s["post_id"]),
            sleep_seconds=0, edges_fields=None,
        )
        assert len(result["nodes"]) == 2
        assert len(result["edges"]) == 2
        assert result["reply_count"] == 2

    @patch("backend.parallel_harvester.sources.time.sleep")
    def test_deduplicates_same_reply(self, _sleep):
        from backend.parallel_harvester.sources import expand_seed

        result = expand_seed(
            seed_doc=self._seed(), headers={}, platform_label="Test",
            get_direct_replies=lambda pid, h: [{"post_id": "r1"}, {"post_id": "r1"}] if pid == "root" else [],
            normalise_reply=lambda r, s: self._reply_doc(r["post_id"]),
            sleep_seconds=0, edges_fields=None,
        )
        assert result["reply_count"] == 1
        assert len(result["nodes"]) == 1

    @patch("backend.parallel_harvester.sources.time.sleep")
    def test_skips_posts_outside_study_window(self, _sleep):
        from backend.parallel_harvester.sources import expand_seed
        from backend.parallel_harvester import config
        config.STUDY_START = "2026-04-01T00:00:00Z"

        def old_reply(r, s):
            doc = self._reply_doc(r["post_id"])
            doc["created_at"] = "2026-01-01T00:00:00Z"
            return doc

        result = expand_seed(
            seed_doc=self._seed(), headers={}, platform_label="Test",
            get_direct_replies=lambda pid, h: [{"post_id": "r1"}] if pid == "root" else [],
            normalise_reply=old_reply,
            sleep_seconds=0, edges_fields=None,
        )
        assert result["reply_count"] == 0
        assert result["nodes"] == []

    @patch("backend.parallel_harvester.sources.time.sleep")
    def test_handles_api_error_gracefully(self, _sleep):
        from backend.parallel_harvester.sources import expand_seed

        def raise_error(pid, h):
            raise RuntimeError("Network error")

        result = expand_seed(
            seed_doc=self._seed(), headers={}, platform_label="Test",
            get_direct_replies=raise_error,
            normalise_reply=lambda r, s: r,
            sleep_seconds=0, edges_fields=None,
        )
        assert result["nodes"] == []
        # Failed requests still consume the bounded API-call budget.
        assert result["api_calls"] == 1


# ===========================================================================
# app.py — run doc construction (no Flask/ES needed)
# ===========================================================================

class TestMakeRunDoc:
    def setup_method(self):
        from backend.parallel_harvester.app import make_run_doc
        self.make = make_run_doc

    def test_success_doc(self):
        doc = self.make("bluesky", "2026-03-01T10:00:00Z", "2026-03-01T10:05:00Z", "success",
                        result={"seed_fetched": 10, "seed_inserted": 10,
                                "reply_nodes_inserted": 50, "reply_edges_inserted": 50,
                                "query_count": 3})
        assert doc["platform"] == "bluesky"
        assert doc["status"] == "success"
        assert doc["seed_records_fetched"] == 10
        assert doc["reply_nodes_inserted"] == 50
        assert doc["error_message"] is None

    def test_failed_doc(self):
        doc = self.make("mastodon", "2026-03-01T10:00:00Z", "2026-03-01T10:01:00Z", "failed",
                        error_message="Connection refused")
        assert doc["status"] == "failed"
        assert "Connection refused" in doc["error_message"]
        assert doc["seed_records_fetched"] == 0

    def test_run_id_contains_platform(self):
        doc = self.make("bluesky", "2026-03-01T10:00:00Z", "2026-03-01T10:01:00Z", "success")
        assert "bluesky" in doc["run_id"]
