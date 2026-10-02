import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from backend.ingestion.backfill import run as backfill
from backend.ingestion.jetstream import parse_event
from backend.common.es import WriteCounts


def post(uri="at://did:plc:test/app.bsky.feed.post/1"):
    return {"uri": uri, "author": {"did": "did:plc:test"},
            "record": {"text": "Melbourne tram fares are expensive", "createdAt": "2026-09-01T12:00:00Z", "langs": ["en"]}}


def test_backfill_resumes_page_cursor_without_rewriting_prior_page(tmp_path, monkeypatch):
    checkpoint = tmp_path/"cursor.json"
    monkeypatch.setattr("backend.ingestion.backfill.bulk_documents",
                        lambda index, docs, **kwargs: WriteCounts(attempted=len(docs), created=len(docs)))
    fetch = Mock(return_value={"posts": [post()], "cursor": "page-two"})
    first = backfill("2026-09-01", "2026-09-01", ["tram Melbourne"], checkpoint=checkpoint,
                     request_budget=1, fetch=fetch, es=Mock(), sleep=lambda _: None)
    assert first["writes"]["created"] == 1
    second_fetch = Mock(return_value={"posts": [post("at://did:plc:test/app.bsky.feed.post/2")]})
    second = backfill("2026-09-01", "2026-09-01", ["tram Melbourne"], checkpoint=checkpoint,
                      request_budget=1, fetch=second_fetch, es=Mock(), sleep=lambda _: None)
    assert second_fetch.call_args.args[0]["cursor"] == "page-two"
    assert second["completed_windows"] == 1


def test_backfill_repeated_cursor_is_reported_not_infinite(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.ingestion.backfill.bulk_documents",
                        lambda index, docs, **kwargs: WriteCounts(attempted=len(docs), noop=len(docs)))
    fetch = Mock(return_value={"posts": [post()], "cursor": "same"})
    result = backfill("2026-09-01", "2026-09-01", ["tram"], checkpoint=tmp_path/"c.json",
                      request_budget=10, fetch=fetch, es=Mock(), sleep=lambda _: None)
    assert fetch.call_count == 2
    assert result["capped_windows"] == 1


def test_backfill_sink_failure_cannot_advance_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.ingestion.backfill.bulk_documents", Mock(side_effect=RuntimeError("sink failed")))
    checkpoint = tmp_path/"c.json"
    with pytest.raises(RuntimeError):
        backfill("2026-09-01", "2026-09-01", ["tram"], checkpoint=checkpoint,
                 fetch=lambda _: {"posts": [post()], "cursor": "later"}, es=Mock(), sleep=lambda _: None)
    assert not checkpoint.exists()


def event(operation="create"):
    return {"did": "did:plc:test", "time_us": 1790790000000000, "kind": "commit",
            "commit": {"collection": "app.bsky.feed.post", "rkey": "1", "operation": operation,
                       "record": post()["record"]}}


def test_jetstream_create_and_delete_share_identity():
    operation, doc = parse_event(event())
    deletion, uri = parse_event(event("delete"))
    assert operation == "create" and deletion == "delete"
    assert uri == doc["post_id"]
    assert doc["dataset_kind"] == "live"


def test_jetstream_other_event_ignored():
    assert parse_event({"kind": "identity", "did": "did:plc:test"}) == (None, None)


def test_archive_never_includes_virtualenv_or_private_config(tmp_path, monkeypatch):
    from scripts.package_fission import build
    from zipfile import ZipFile
    path = tmp_path/"functions.zip"
    build(path)
    with ZipFile(path) as package:
        assert "backend/api/app.py" in package.namelist()
        assert not any(".venv" in name or "config_private" in name or "__pycache__" in name for name in package.namelist())
