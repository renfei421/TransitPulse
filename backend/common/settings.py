"""Read non-secret settings from environment and credentials from env/Secret files.

Fission's --secret mounts keys at /secrets/<namespace>/<secret>/<key>.
Kubernetes Jobs inject the same keys with secretKeyRef. Neither path requires
credentials in source code, command-line arguments or query strings.
"""

from dataclasses import dataclass
import os
from pathlib import Path


def secret(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    explicit = os.getenv(f"{name}_FILE")
    if explicit:
        return Path(explicit).read_text(encoding="utf-8").strip()
    root = Path(os.getenv("SECRET_DIR", "/secrets/default/transport-secrets"))
    path = root / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() else default


def boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value.lower() in {"true", "1"}


@dataclass(frozen=True)
class ESSettings:
    url: str
    username: str
    password: str
    verify: bool | str
    prefix: str = ""
    timeout: float = 30

    @classmethod
    def from_env(cls):
        verify: bool | str = boolean("ES_VERIFY_CERTS", True)
        if verify and os.getenv("ES_CA_CERT"):
            verify = os.environ["ES_CA_CERT"]
        return cls(
            url=os.getenv("ES_HOST", "https://elasticsearch-es-http.elastic.svc:9200"),
            username=secret("ES_USER", "elastic"),
            password=secret("ES_PASSWORD", secret("ES_PASS")),
            verify=verify,
            prefix=os.getenv("ES_INDEX_PREFIX", ""),
            timeout=float(os.getenv("ES_TIMEOUT", "30")),
        )

    def authentication(self):
        if not self.password:
            if not boolean("ES_ALLOW_ANONYMOUS", False):
                raise ValueError("ES_PASSWORD is required; use ES_ALLOW_ANONYMOUS=true only for local tests")
            return None
        return self.username, self.password


def index_name(logical: str) -> str:
    """Prefix all indices for isolated integration tests and parallel environments."""
    return os.getenv("ES_INDEX_PREFIX", "") + logical
