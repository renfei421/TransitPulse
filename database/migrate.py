"""Explicit, additive schema provisioning. Never deletes indices or rewrites data.

Use a fresh ES_INDEX_PREFIX when adopting v2 metrics, then reprocess raw records.
Existing fields cannot be retyped by this command; Elasticsearch rejects an
incompatible update instead of silently changing the meaning of stored results.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from backend.common.es import get_client
from backend.common.settings import index_name

MAPPING_DIR=Path(__file__).parent/"mappings"


def definitions():
    return {p.stem:json.loads(p.read_text(encoding="utf-8")) for p in sorted(MAPPING_DIR.glob("*.json"))}


def migrate(client=None, *, replicas=0, shards=1):
    client=client or get_client()
    applied=[]
    for logical,source in definitions().items():
        definition=deepcopy(source)
        definition["settings"]={"number_of_shards":shards,"number_of_replicas":replicas}
        name=index_name(logical)
        if client.indices.exists(index=name):
            client.indices.put_mapping(index=name,body=definition["mappings"])
            action="mapping_updated"
        else:
            client.indices.create(index=name,body=definition)
            action="created"
        applied.append({"index":name,"action":action})
    return applied


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replicas",type=int,default=0,choices=range(0,4))
    parser.add_argument("--shards",type=int,default=1,choices=range(1,5))
    args=parser.parse_args()
    print(json.dumps(migrate(replicas=args.replicas,shards=args.shards),indent=2))


if __name__=="__main__":
    main()
