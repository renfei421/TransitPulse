"""News storage; schema creation is an explicit migration, not a runtime action."""
import hashlib
from elasticsearch.helpers import scan
from backend.common.es import bulk_documents, get_client
from backend.common.settings import index_name
from backend.news_harvester import config


def stable_hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def ensure_indexes():
    client = get_client()
    for logical in (config.VOLUME_INDEX, config.NEWS_INDEX):
        if not client.indices.exists(index=index_name(logical)):
            raise RuntimeError("Required indices are missing; run python -m database.migrate")


def bulk_upsert(index, docs, id_func):
    records = [{**doc, "_write_id":id_func(doc)} for doc in docs]
    # _write_id is stripped from the stored document by choosing the native id
    # field wherever possible; volume records retain a stable id for traceability.
    records = [{**{k:v for k,v in doc.items() if k != "_write_id"}, "id":doc["_write_id"]} for doc in records]
    return bulk_documents(index, records, "id").as_dict()


def search_all(index, body, page_size=1000):
    query = {k:v for k,v in body.items() if k not in {"size", "sort"}}
    return [hit["_source"] for hit in scan(get_client(), index=index_name(index), query=query, size=page_size)]


def doc_exists(index, doc_id):
    return bool(get_client().exists(index=index_name(index), id=doc_id))
