"""Protect the topology-dependent migration and credential handling boundaries."""
import json
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from scripts.cloud_elasticsearch import Cluster
from scripts.render_deployment import generate


@pytest.mark.parametrize("replicas", [0, 1, 2])
def test_release_can_provision_indices_for_the_selected_topology(replicas):
    resources = generate("app:test", "model:test", "fission:test", es_replicas=replicas)
    job = resources["migration"][0]
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == ["--replicas", str(replicas)]
    assert {"secretRef": {"name": "transport-migration-secrets"}} in container["envFrom"]


def test_invalid_topology_rejected_before_rendering():
    with pytest.raises(ValueError):
        generate("app:test", "model:test", "fission:test", es_replicas=-1)


def test_secret_values_use_stdin_and_never_error_text(monkeypatch):
    cluster = object.__new__(Cluster)
    cluster.command = ["kubectl", "--kubeconfig", "explicit-file", "--context", "test"]
    runner = Mock(return_value=CompletedProcess([], 1, "", "server echoed sensitive-value"))
    monkeypatch.setattr("scripts.cloud_elasticsearch.subprocess.run", runner)
    with pytest.raises(RuntimeError) as error:
        cluster.put_secret("transport-secrets", {"ES_PASSWORD": "sensitive-value"})
    args, kwargs = runner.call_args
    assert "sensitive-value" not in str(args)
    assert "sensitive-value" not in str(error.value)
    assert json.loads(kwargs["input"])["kind"] == "Secret"
    assert "--server-side" in args[0]
