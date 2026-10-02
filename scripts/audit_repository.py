"""Focused tracked-source checks for accidental secrets and deprecated deployment practices.

This is a regression guard, not a comprehensive security certification.
Never print a detected credential value.
"""
import ast
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def source_files():
    try:
        names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                        cwd=ROOT).decode().split("\0")
    except FileNotFoundError:
        names = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*") if path.is_file()
                 and not any(part.startswith(".") or part in {"artifacts", "data", "__pycache__"}
                             for part in path.relative_to(ROOT).parts)]
    for name in sorted(set(names)):
        if not name:
            continue
        path = ROOT/name
        if path.is_file() and path.suffix in {".py", ".sh", ".yaml", ".yml", ".toml", ".json", ".ipynb"}:
            yield path


def audit():
    issues = []
    for path in source_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(("test/", "deploy/schemas/")) or path.name == "audit_repository.py":
            continue
        text = path.read_text(encoding="utf-8-sig")
        patterns = {
            "disabled_tls": r"verify(?:_certs)?\s*=\s*False",
            "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            "github_token": r"gh[pousr]_[A-Za-z0-9]{30,}",
            "gitlab_token": r"glpat-[A-Za-z0-9_-]{20,}",
            "inline_python_configmap": r"--from-file=(?:compute|health_check)\.py",
        }
        for label, pattern in patterns.items():
            for match in re.finditer(pattern, text):
                issues.append(f"{relative}:{text.count(chr(10), 0, match.start())+1}:{label}")
        if path.suffix == ".py":
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    sensitive = any(isinstance(target, ast.Name) and re.search(r"PASSWORD|API_KEY|ACCESS_TOKEN|ES_PASS$", target.id)
                                    for target in node.targets)
                    if sensitive and node.value.value:
                        issues.append(f"{relative}:{node.lineno}:hardcoded_credential")
    if issues:
        raise SystemExit("\n".join(issues))
    print("Tracked-source credential/TLS/deployment regression checks passed")


if __name__ == "__main__":
    audit()
