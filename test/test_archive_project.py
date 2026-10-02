import json
import sqlite3
from unittest.mock import Mock, patch

import pytest

from scripts.archive_project import fingerprint, local_client, portable_settings, restore, validate_archive
from scripts.bundle_private_archive import backup_sqlite


def test_fingerprint_detects_source_id_and_duplicate_changes_but_not_scan_order():
    rows = [{"_id": "a", "_source": {"value": 1}}, {"_id": "b", "_source": {"value": 2}}]
    expected = fingerprint(rows)
    assert fingerprint(reversed(rows)) == expected
    assert fingerprint(rows + rows[:1]) != expected
    assert fingerprint([{**rows[0], "_id": "z"}, rows[1]]) != expected
    assert fingerprint([{"_id": "a", "_source": {"value": 9}}, rows[1]]) != expected


def test_restore_strips_cloud_identity_allocation_and_write_blocks():
    settings = portable_settings({"index": {"uuid": "cloud", "version": {"created": "8"},
        "blocks": {"write": "true"}, "routing": {"allocation": {"require": {"zone": "sgp1"}}},
        "mapping": {"total_fields": {"limit": "2000"}}, "number_of_shards": "1"}})
    assert set(settings) == {"mapping", "number_of_shards", "number_of_replicas", "refresh_interval"}
    assert settings["mapping"]["total_fields"]["limit"] == "2000"


@pytest.mark.parametrize("url", ["https://cloud.example", "http://localhost:9200", "http://127.0.0.1:9200/path"])
def test_restore_rejects_nonlocal_or_path_endpoints(url):
    with pytest.raises(ValueError):
        local_client(url)


def test_incomplete_archive_cannot_be_restored(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"complete": False}))
    with pytest.raises(ValueError, match="Incomplete"):
        validate_archive(tmp_path)


def test_restore_preflights_all_names_before_creating_any_index(tmp_path):
    (tmp_path / "index-metadata.json").write_text("{}")
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.info.return_value = {"version": {"number": "8.19.22"}}
    client.indices.exists.side_effect = [False, True]
    with patch("scripts.archive_project.validate_archive", return_value={"es_version": "8.19.22", "indices": {"v2_a": {}, "v2_b": {}}}), patch("scripts.archive_project.local_client", return_value=client):
        with pytest.raises(ValueError, match="already contains"):
            restore(tmp_path, "http://127.0.0.1:19222")
    client.indices.create.assert_not_called()


def test_sqlite_snapshot_is_consistent_and_releases_windows_file_handles(tmp_path):
    source, destination = tmp_path / "source.sqlite", tmp_path / "snapshot.sqlite"
    db = sqlite3.connect(source)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE records (value TEXT)")
    db.execute("INSERT INTO records VALUES ('preserved')")
    db.commit()
    backup_sqlite(source, destination)
    snapshot = sqlite3.connect(destination)
    assert snapshot.execute("SELECT value FROM records").fetchall() == [("preserved",)]
    snapshot.close()
    destination.unlink()  # Windows rejects this if backup_sqlite leaked a handle.
    assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 1
    db.close()
