"""Stream a logical Elasticsearch index to NDJSON using shared secure settings."""
import argparse
import json
from pathlib import Path
from elasticsearch.helpers import scan
from backend.common.es import get_client
from backend.common.settings import index_name


def export_index(es, logical_index, output_path, page_size=500):
    if not 1 <= page_size <= 5000:
        raise ValueError("page_size must be 1..5000")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with output_path.open("w", encoding="utf-8") as stream:
        for hit in scan(es, index=index_name(logical_index), query={"query": {"match_all": {}}},
                        size=page_size, scroll="5m"):
            stream.write(json.dumps({"_id": hit["_id"], "_source": hit["_source"]}, ensure_ascii=False)+"\n")
            total += 1
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="social_discussion_posts_raw")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps({"exported": export_index(get_client(), args.index, args.output, args.page_size),
                      "output": args.output}))


if __name__ == "__main__":
    main()
