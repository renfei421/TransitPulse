"""Central Elasticsearch client and accurate, idempotent bulk-write accounting."""

from dataclasses import asdict, dataclass
import json
import requests
from elasticsearch import Elasticsearch

from .settings import ESSettings, index_name


def get_client():
    settings = ESSettings.from_env()
    options = {"request_timeout": settings.timeout, "verify_certs": bool(settings.verify)}
    if isinstance(settings.verify, str):
        options["ca_certs"] = settings.verify
    auth = settings.authentication()
    if auth:
        options["basic_auth"] = auth
    return Elasticsearch(settings.url, **options)


def request(method, path, body=None, **kwargs):
    settings = ESSettings.from_env()
    response = requests.request(
        method, f"{settings.url.rstrip('/')}/{path.lstrip('/')}",
        auth=settings.authentication(), verify=settings.verify,
        timeout=kwargs.pop("timeout", settings.timeout), json=body, **kwargs,
    )
    response.raise_for_status()
    return response


@dataclass
class WriteCounts:
    attempted: int = 0
    created: int = 0
    updated: int = 0
    noop: int = 0
    failed: int = 0

    @property
    def succeeded(self):
        return self.created + self.updated + self.noop

    def as_dict(self):
        return {**asdict(self), "succeeded":self.succeeded}

    def add(self, other):
        for field in asdict(self):
            setattr(self, field, getattr(self, field)+getattr(other, field))
        return self


class BulkWriteError(RuntimeError):
    def __init__(self, counts):
        self.counts = counts
        super().__init__(f"Elasticsearch bulk write failed for {counts.failed} of {counts.attempted} actions")


@dataclass
class CreateCounts:
    attempted: int = 0
    created: int = 0
    already_present: int = 0
    failed: int = 0

    def as_dict(self):
        return asdict(self)

    def add(self, other):
        for field in asdict(self):
            setattr(self, field, getattr(self, field) + getattr(other, field))
        return self


def create_documents(logical_index, docs, *, client=None):
    """Insert immutable first observations, preserving earlier source provenance.

    Only a create version conflict means a duplicate. Mapping/auth failures must
    never be counted as already present. Replays cannot overwrite prior batches.
    """
    docs = list(docs)
    counts = CreateCounts(attempted=len(docs))
    if not docs:
        return counts
    operations = []
    for doc in docs:
        operations.extend([{"create": {"_index": index_name(logical_index), "_id": doc["doc_id"]}}, doc])
    response = (client or get_client()).bulk(operations=operations)
    if len(response["items"]) != len(docs):
        raise RuntimeError("Elasticsearch returned an incomplete bulk response")
    for item in response["items"]:
        outcome = item.get("create", {})
        if (outcome.get("status") == 201 and outcome.get("result") == "created"
                and not outcome.get("error")):
            counts.created += 1
        elif (outcome.get("status") == 409
                and outcome.get("error", {}).get("type") == "version_conflict_engine_exception"):
            counts.already_present += 1
        else:
            counts.failed += 1
    if counts.failed:
        raise BulkWriteError(counts)
    return counts


def bulk_documents(logical_index, docs, id_field="doc_id", *, client=None, script=None):
    """Inspect every item; HTTP 200 alone does not mean an ES bulk write succeeded."""
    docs = list(docs)
    counts = WriteCounts(attempted=len(docs))
    if not docs:
        return counts
    operations = []
    for doc in docs:
        operations.append({"update":{"_index":index_name(logical_index), "_id":doc[id_field]}})
        operations.append(
            {"script":{"source":script,"lang":"painless","params":{"doc":doc}}, "upsert":doc}
            if script else {"doc":doc, "doc_as_upsert":True}
        )
    response = (client or get_client()).bulk(operations=operations)
    if len(response["items"]) != len(docs):
        raise RuntimeError("Elasticsearch returned an incomplete bulk response")
    for item in response["items"]:
        outcome = next(iter(item.values()))
        if outcome.get("error") or outcome.get("status", 500) >= 300:
            counts.failed += 1
        else:
            result = outcome.get("result", "updated")
            setattr(counts, result if result in {"created","updated","noop"} else "updated",
                    getattr(counts, result if result in {"created","updated","noop"} else "updated")+1)
    if counts.failed:
        raise BulkWriteError(counts)
    return counts


def json_safe(value):
    """Fail before sending NaN/Infinity into ES, logs or public HTTP responses."""
    return json.loads(json.dumps(value, allow_nan=False, default=str))
