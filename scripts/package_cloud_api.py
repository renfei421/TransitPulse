"""Package API source with Linux dependencies using the exact public Fission runtime.

The deployment archive is private to Fission storage; no public image registry or
cluster-side pip install is required. Runtime Flask comes from the pinned image.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from scripts.package_fission import build

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = "ghcr.io/fission/python-env@sha256:446f2cd7c2a8c835deb925586d8f761aed288e0eba4437230996573612bab266"
ENTRYPOINT = '''"""Load bundled API dependencies before application imports."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent / "vendor"))
from backend.api.app import main
'''


def package(output="artifacts/transport-api-linux.zip", vendor=None):
    output = (ROOT/output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cloud-api-") as temp:
        staging = Path(temp)
        if vendor is None:
            subprocess.run(["docker", "run", "--rm", "--mount", f"type=bind,source={staging},target=/out",
                "--mount", f"type=bind,source={ROOT/'requirements-fission.txt'},target=/requirements.txt,readonly",
                "--entrypoint", "sh", RUNTIME, "-c",
                "pip install --no-cache-dir --require-hashes --no-compile --target /out/vendor -r /requirements.txt"],
                check=True, timeout=300)
            vendor = staging/"vendor"
        else:
            vendor = Path(vendor).resolve()
        if not (vendor/"elasticsearch/__init__.py").is_file():
            raise ValueError("Missing built Linux API dependencies")
        source = staging/"source.zip"
        build(source)
        with ZipFile(output, "w", ZIP_DEFLATED) as dest, ZipFile(source) as src:
            entries = {name: src.read(name) for name in src.namelist()}
            entries["cloud_entrypoint.py"] = ENTRYPOINT.encode()
            for p in sorted(vendor.rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                    entries["vendor/"+p.relative_to(vendor).as_posix()] = p.read_bytes()
            for name, content in sorted(entries.items()):
                info = ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                dest.writestr(info, content)
    result = {"archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "bytes": output.stat().st_size, "runtime_image": RUNTIME,
              "dependency_lock_sha256": hashlib.sha256((ROOT/"requirements-fission.txt").read_bytes()).hexdigest()}
    (ROOT/"artifacts/cloud-api-package.json").write_text(json.dumps(result, indent=2)+"\n",
                                                       encoding="utf-8", newline="\n")
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/transport-api-linux.zip")
    parser.add_argument("--vendor", help="Reuse dependencies already built in the pinned Linux runtime")
    package(**vars(parser.parse_args()))
