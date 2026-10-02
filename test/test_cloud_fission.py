"""Cloud acceptance guards that can be checked without a Kubernetes account."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from scripts import cloud_fission
from scripts.prepare_fission_install import verified_download


def test_tampered_cached_install_manifest_is_rejected(tmp_path):
    path = tmp_path/"manifest.yaml"
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verified_download(path, {"url": "https://unused.invalid", "sha256": hashlib.sha256(b"expected").hexdigest()})


def test_cloud_api_profile_excludes_jobs_and_admin_credentials(tmp_path, monkeypatch):
    (tmp_path/"artifacts").mkdir()
    (tmp_path/"artifacts/transport-api-linux.zip").write_bytes(b"archive-existence-placeholder")
    (tmp_path/"specs").mkdir()
    (tmp_path/"specs/fission-deployment-config.yaml").write_text("name: test\n")
    monkeypatch.setattr(cloud_fission, "ROOT", tmp_path)
    path = cloud_fission.render()
    objects = list(yaml.safe_load_all((path/"specs/application.yaml").read_text()))
    assert {o["kind"] for o in objects} == {"Environment", "Package", "Function", "HTTPTrigger", "ArchiveUploadSpec"}
    assert [o["metadata"]["name"] for o in objects if o["kind"] == "Function"] == ["transport-api"]
    spec = next(o for o in objects if o["kind"] == "Environment")["spec"]["runtime"]["podspec"]
    assert spec["automountServiceAccountToken"] is False
    assert "transport-migration-secrets" not in json.dumps(objects)
    data = yaml.safe_load((path/"core.yaml").read_text())["data"]
    assert data["ES_HOST"] == "https://elasticsearch-es-http.elastic.svc:9200"
    assert data["ES_VERIFY_CERTS"] == "true"
    assert data["SOCIAL_ACTIVE_MODEL"] == "jev-1.13.0"
    assert not any("REDIS" in key or "PASSWORD" in key for key in data)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    assert {"name": "TRANSPORT_CONFIG_DIGEST", "value": digest} in spec["containers"][0]["env"]


def test_cloud_storage_and_routing_are_private_and_persistent():
    root = Path(__file__).resolve().parents[1]
    values = yaml.safe_load((root/"deploy/cloud/fission-values.yaml").read_text())
    assert values["routerServiceType"] == "ClusterIP"
    assert values["internalAuth"]["enabled"] is True
    resources = list(yaml.safe_load_all((root/"deploy/cloud/fission-storage.yaml").read_text()))
    claim = next(r for r in resources if r["kind"] == "PersistentVolumeClaim")
    assert claim["metadata"]["name"] == values["persistence"]["existingClaim"]
    assert claim["spec"]["storageClassName"] == "do-block-storage-retain"
