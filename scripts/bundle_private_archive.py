"""Make a checksummed private recovery package; excludes provider credentials.

The ZIP contains original posts and must not be pushed to a public repository.
A SQLite backup API copy is used instead of copying a potentially live WAL file.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import zipfile

from scripts.archive_project import file_hash, validate_archive, write_json

ROOT = Path(__file__).resolve().parents[1]


def backup_sqlite(path, destination):
    # A sqlite3 Connection context manager controls transactions, not closure.
    # Explicit closure is required before Windows can remove the temporary file.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite backup integrity failure")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/retirement/transitpulse-private-recovery.zip"))
    args = parser.parse_args()
    destination = args.output.resolve()
    if not destination.is_relative_to((ROOT / "artifacts").resolve()) or destination.exists():
        raise ValueError("Use a new ZIP path under the ignored artifacts directory")
    archive = ROOT / "data/archives/iran-20260228-final"
    validate_archive(archive)
    if not json.loads((archive / "restore-verification.json").read_text())["passed"]:
        raise ValueError("Restore verification is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = set()
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    for name in names:
        if name and (ROOT / name).is_file():
            paths.add(ROOT / name)
    for path in (ROOT / "data").rglob("*"):
        if path.is_file() and "private" not in path.relative_to(ROOT / "data").parts and not path.name.endswith(("-wal", "-shm")):
            paths.add(path)
    for path in (ROOT / "artifacts").rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT / "artifacts")
        if relative.parts[0] in {"api-bundle-staging", "api-vendor-reuse", "retirement"}:
            continue
        if path.suffix in {".json", ".xml", ".log", ".zip", ".png", ".html", ".yaml", ".yml", ".mp4", ".srt"}:
            paths.add(path)
    inventory = []
    partial = destination.with_suffix(".partial")
    if partial.exists():
        raise ValueError("A partial bundle already exists; inspect it before retrying")
    with tempfile.TemporaryDirectory(prefix="transitpulse-bundle-") as temp:
        bundle = Path(temp) / "source-history.bundle"
        subprocess.run(["git", "bundle", "create", str(bundle), "--all"], cwd=ROOT, check=True, capture_output=True)
        with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as output:
            for path in sorted(paths):
                name = path.relative_to(ROOT).as_posix()
                actual = path
                if path.suffix == ".log":
                    # The local demo can continue serving while its log is archived.
                    actual = Path(temp) / (str(len(inventory)) + ".log")
                    actual.write_bytes(path.read_bytes())
                if path.suffix == ".sqlite":
                    actual = Path(temp) / (file_hash(path)[:16] + ".sqlite")
                    backup_sqlite(path, actual)
                method = zipfile.ZIP_STORED if path.suffix in {".gz", ".zip", ".png", ".mp4"} else zipfile.ZIP_DEFLATED
                output.write(actual, name, compress_type=method)
                inventory.append({"path": name, "bytes": actual.stat().st_size, "sha256": file_hash(actual)})
            output.write(bundle, "source-history.bundle")
            inventory.append({"path": "source-history.bundle", "bytes": bundle.stat().st_size, "sha256": file_hash(bundle)})
            output.writestr("PACKAGE-MANIFEST.json", json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                "private": True, "excluded": ["provider credentials", "kubeconfig", "ES security indices", "model weight caches"],
                "files": inventory}, ensure_ascii=False, indent=2))
        # Verify both ZIP CRC and every member's SHA-256, not just that the file exists.
        import hashlib
        with zipfile.ZipFile(partial) as output:
            for item in inventory:
                digest = hashlib.sha256()
                with output.open(item["path"]) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError("Recovery package member hash mismatch")
    partial.rename(destination)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "filename": destination.name,
              "bytes": destination.stat().st_size, "sha256": file_hash(destination), "members_verified": len(inventory),
              "contains_private_posts": True, "independent_copy_verified": False}
    write_json(destination.with_suffix(".manifest.json"), report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
