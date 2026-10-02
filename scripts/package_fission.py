"""Build a deterministic archive from an allowlist; never include credentials or data."""
import argparse
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]


def build(destination="artifacts/transport-functions.zip"):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
        for top in ("backend", "database"):
            for path in sorted((ROOT/top).rglob("*")):
                if path.is_file() and path.suffix in {".py", ".json"} and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(ROOT).parts):
                    if path.name == "config_private.py":
                        continue
                    name = path.relative_to(ROOT).as_posix()
                    info = ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    archive.writestr(info, path.read_bytes())
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"{destination}: sha256:{checksum}")
    return checksum


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/transport-functions.zip")
    build(parser.parse_args().output)
