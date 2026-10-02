"""Fetch checksum-locked upstream Fission/metrics manifests; no cluster mutation."""
import hashlib
import json
from pathlib import Path
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]


def verified_download(path, entry):
    content = path.read_bytes() if path.exists() else urllib.request.urlopen(entry["url"], timeout=60).read()
    if hashlib.sha256(content).hexdigest() != entry["sha256"]:
        raise ValueError("Upstream checksum mismatch: " + path.name)
    path.write_bytes(content)
    return content


def prepare():
    lock = json.loads((ROOT/"deploy/cloud/fission-lock.json").read_text())
    directory = ROOT/"artifacts/fission-install"
    directory.mkdir(parents=True, exist_ok=True)
    crds = []
    for name, entry in lock["files"].items():
        content = verified_download(directory/name, entry)
        if name.startswith("fission.io_"):
            crds.append(yaml.safe_load(content))
    (directory/"fission-crds.yaml").write_text(yaml.safe_dump_all(crds, sort_keys=False),
                                               encoding="utf-8", newline="\n")
    metrics = list(yaml.safe_load_all((directory/"metrics-server.yaml").read_text()))
    deployment = next(d for d in metrics if d["kind"] == "Deployment")
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = lock["metrics_server_image"]
    (directory/"metrics-server-rendered.yaml").write_text(yaml.safe_dump_all(metrics, sort_keys=False),
                                                          encoding="utf-8", newline="\n")
    print(f"Verified {len(lock['files'])} upstream files; manifests prepared in {directory}")


if __name__ == "__main__":
    prepare()
