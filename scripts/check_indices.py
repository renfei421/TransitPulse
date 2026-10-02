"""Read-only index counts and schema metadata."""
import json
from backend.common.es import get_client
from backend.common.settings import index_name
from database.migrate import definitions


def main():
    es = get_client()
    for logical in definitions():
        name = index_name(logical)
        if es.indices.exists(index=name):
            print(json.dumps({"index": name, "count": es.count(index=name)["count"],
                              "schema": es.indices.get_mapping(index=name)[name]["mappings"].get("_meta")}))
        else:
            print(json.dumps({"index": name, "status": "missing"}))


if __name__ == "__main__":
    main()
