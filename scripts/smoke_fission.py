"""Exercise the real Fission runtime's specialization and HTTP path in Docker."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4
from zipfile import ZipFile
import requests
from scripts.package_fission import build


def run(image="transport-fission:review"):
    build()
    name = "transport-fission-smoke-"+uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="transport-fission-") as directory:
        with ZipFile("artifacts/transport-functions.zip") as package:
            package.extractall(directory)  # our own allowlisted archive
        subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "--network", "transport-analytics_default",
            "-p", "127.0.0.1::8888", "-e", "ES_HOST=http://elasticsearch:9200",
            "-e", "ES_ALLOW_ANONYMOUS=true", "-e", "ES_INDEX_PREFIX=demo_v2_",
            "--mount", f"type=bind,source={directory},target=/userfunc", image], check=True, capture_output=True)
        try:
            detail = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
            port = detail["NetworkSettings"]["Ports"]["8888/tcp"][0]["HostPort"]
            base = "http://127.0.0.1:"+port
            for _ in range(30):
                try:
                    if requests.get(base+"/healthz", timeout=1).status_code < 500:
                        break
                except requests.RequestException:
                    time.sleep(.5)
            result = requests.post(base+"/v2/specialize", json={
                "filepath": "/userfunc", "functionName": "backend.api.app.main"}, timeout=20)
            assert result.status_code == 200, result.text
            meta = requests.get(base+"/api/v1/meta", timeout=5)
            assert meta.status_code == 200, meta.text
            data = requests.get(base+"/api/v1/social/sentiment", params={
                "from": "2026-08-01", "to": "2026-09-29", "dataset_kind": "synthetic"}, timeout=10)
            assert data.status_code == 200, data.text
            health = requests.get(base+"/api/v1/health", timeout=5)
            assert health.status_code == 200, health.text
            output = {"runtime_image": image, "specialization": "passed",
                      "metadata_status": meta.status_code, "health_status": health.status_code,
                      "analytics_rows": len(data.json()["data"]), "kubernetes_deployment": "not tested by this check"}
            Path("artifacts").mkdir(exist_ok=True)
            Path("artifacts/fission-smoke.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
            print(json.dumps(output))
            return output
        except Exception:
            print(subprocess.check_output(["docker", "logs", name], stderr=subprocess.STDOUT).decode(errors="replace")[-6000:])
            raise
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="transport-fission:review")
    run(parser.parse_args().image)
