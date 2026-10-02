import argparse
import ast
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from backend.data_process.sentiment import (SentimentModel, annotate_thread, normalize_text, token_count, is_short_reply, detect_reply_relation, flip_sentiment, is_explicit_opinion, build_context)
from backend.data_process.threads import (assign_depths_and_sort, build_threads_from_post_nodes, build_threads_from_posts_and_edges)


AGREE_TERMS = {
    "agree", "agreed", "exactly", "same", "same here", "this", "true", "yes",
    "yep", "yeah", "correct", "+1", "100%", "fr", "real", "facts",
    "支持", "同意", "没错", "对", "确实", "是的", "赞同"
}

DISAGREE_TERMS = {
    "no", "nah", "nope", "wrong", "false", "disagree", "nonsense",
    "bullshit", "bs", "not true", "cap", "hard disagree",
    "不同意", "不对", "错", "胡扯", "放屁", "不是", "扯淡"
}

WEAK_SHORT_TERMS = {
    "lol", "lmao", "haha", "hahaha", "bro", "wow", "wild", "crazy",
    "interesting", "hmm", "ok", "okay", "?", "??", "！", "哈哈"
}














def parse_topics_from_meta(meta: str) -> List[str]:
    m = re.search(r"topics=(\[[^\]]*\])", meta)
    if not m:
        return []
    try:
        return ast.literal_eval(m.group(1))
    except Exception:
        return []


def parse_sentiment_from_meta(meta: str):
    m = re.search(r"sentiment=([a-zA-Z_]+)\(([-+]?\d*\.?\d+)\)", meta)
    if not m:
        return None, None
    return m.group(1).lower(), float(m.group(2))


def parse_md_reply_threads(path: Path) -> List[Dict]:
    threads = []
    stack = []

    current_seed_type = None
    current_seed_query = None
    current_seed_topics = []
    current_edge_count = None

    pending_node = None

    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        if line.startswith("## Thread"):
            stack = []
            pending_node = None
            current_seed_type = None
            current_seed_query = None
            current_seed_topics = []
            current_edge_count = None
            continue

        if line.startswith("Seed type:"):
            current_seed_type = line.split(":", 1)[1].strip().strip("`")
            continue

        if line.startswith("Seed query:"):
            current_seed_query = line.split(":", 1)[1].strip().strip("`")
            continue

        if line.startswith("Seed topics:"):
            value = line.split(":", 1)[1].strip()
            try:
                current_seed_topics = ast.literal_eval(value)
            except Exception:
                current_seed_topics = []
            continue

        if line.startswith("Descendant edge count:"):
            value = line.split(":", 1)[1].strip()
            try:
                current_edge_count = int(value)
            except Exception:
                current_edge_count = None
            continue

        m = re.match(r"^(\s*)-\s+\[(SEED|depth\s+\d+)\]\s+(.*)$", line)
        if m:
            label = m.group(2)
            meta = m.group(3)

            if label == "SEED":
                depth = 0
            else:
                depth = int(re.search(r"\d+", label).group(0))

            post_id_match = re.search(r"post_id=([^|]+)", meta)
            created_at_match = re.search(r"created_at=([^|]+)", meta)
            keyword_match = re.search(r"keyword=([^|]+)", meta)

            sentiment_label, sentiment_score = parse_sentiment_from_meta(meta)

            node = {
                "post_id": post_id_match.group(1).strip() if post_id_match else None,
                "created_at": created_at_match.group(1).strip() if created_at_match else None,
                "keyword": keyword_match.group(1).strip() if keyword_match else None,
                "candidate_topics": parse_topics_from_meta(meta),
                "original_sentiment_label": sentiment_label,
                "original_sentiment_score": sentiment_score,
                "raw_text": "",
                "children": [],
                "md_depth": depth,
            }

            if depth == 0:
                node["seed_type"] = current_seed_type
                node["seed_query"] = current_seed_query
                node["seed_topics"] = current_seed_topics
                node["descendant_edge_count"] = current_edge_count

                threads.append(node)
                stack = [node]

            else:
                while len(stack) > depth:
                    stack.pop()

                if not stack:
                    continue

                stack[-1].setdefault("children", []).append(node)

                if len(stack) == depth:
                    stack.append(node)
                else:
                    stack[depth] = node

            pending_node = node
            continue

        text_match = re.match(r"^\s*Text:\s*(.*)$", line)
        if text_match and pending_node is not None:
            pending_node["raw_text"] = text_match.group(1).strip()
            continue

        if pending_node is not None:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if not re.match(r"^(Seed type|Seed query|Seed topics|Descendant edge count):", stripped):
                    pending_node["raw_text"] = (
                        pending_node.get("raw_text", "") + " " + stripped
                    ).strip()

    return threads


def sort_key(node: Dict) -> str:
    return str(node.get("created_at") or "")


def node_from_ndjson_doc(doc: Dict) -> Dict:
    region_confidence = doc.get("inferred_region_confidence", doc.get("region_confidence"))
    return {
        "post_id": doc.get("post_id"),
        "thread_root_id": doc.get("thread_root_id"),
        "author_id_hash": doc.get("author_id_hash"),
        "fetched_at": doc.get("fetched_at"),
        "direct_candidate_topics": doc.get("direct_candidate_topics", doc.get("candidate_topics")) or [],
        "inherited_candidate_topics": doc.get("inherited_candidate_topics") or [],
        "dataset_kind": doc.get("dataset_kind", "live"),
        "source_dataset": doc.get("source_dataset"),
        "parent_post_id": doc.get("parent_post_id"),
        "created_at": doc.get("created_at"),
        "keyword": "seed" if doc.get("is_seed_post") else str(bool(doc.get("reply_has_keyword"))).lower(),
        "candidate_topics": doc.get("candidate_topics") or [],
        "original_sentiment_label": doc.get("sentiment_label"),
        "original_sentiment_score": doc.get("sentiment_score"),
        "raw_text": doc.get("raw_text") or "",
        "children": [],
        "md_depth": 0,
        "seed_type": doc.get("seed_type"),
        "seed_query": doc.get("source_query"),
        "seed_topics": doc.get("candidate_topics") or [],
        "descendant_edge_count": None,
        "platform": doc.get("platform"),
        "doc_id": doc.get("doc_id"),
        "post_url": doc.get("post_url"),
        "server_domain": doc.get("server_domain"),
        "is_seed_post": doc.get("is_seed_post"),
        "is_reply": doc.get("is_reply"),
        "collection_method": doc.get("collection_method"),
        "collection_methods": doc.get("collection_methods") or [],
        "matched_keywords": doc.get("matched_keywords") or [],
        "inferred_region": doc.get("inferred_region"),
        "inferred_region_confidence": str(region_confidence) if region_confidence is not None else None,
        "nearest_event_id": doc.get("nearest_event_id"),
        "nearest_event_name": doc.get("nearest_event_name"),
        "days_from_event": doc.get("days_from_event"),
        "event_period": doc.get("event_period"),
    }


def load_post_nodes(path: Path) -> Dict[str, Dict]:
    nodes_by_post_id = {}

    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid JSON on line {line_number}: {error}") from error

            node = node_from_ndjson_doc(doc)
            post_id = node.get("post_id")
            if not post_id:
                continue
            nodes_by_post_id[post_id] = node

    return nodes_by_post_id




def parse_ndjson_threads(path: Path) -> List[Dict]:
    nodes_by_post_id = load_post_nodes(path)
    return build_threads_from_post_nodes(nodes_by_post_id)




def load_edge_docs(path: Path) -> List[Dict]:
    edges = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                edge = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid edge JSON on line {line_number}: {error}") from error
            if edge.get("seed_post_id") and edge.get("descendant_post_id"):
                edges.append(edge)
    return edges


def select_primary_edges(edges: List[Dict], seed_post_ids: set) -> List[Dict]:
    """
    Keep one primary seed relationship per descendant post for post-level output.
    Seed posts are kept as thread roots rather than duplicated as descendants.
    """
    primary_by_descendant = {}
    for edge in sorted(
        edges,
        key=lambda item: (
            str(item.get("descendant_post_id") or ""),
            int(item.get("depth_from_seed") or 999999),
            str(item.get("seed_created_at") or ""),
            str(item.get("seed_post_id") or ""),
        ),
    ):
        descendant_post_id = edge.get("descendant_post_id")
        if descendant_post_id in seed_post_ids:
            continue
        primary_by_descendant.setdefault(descendant_post_id, edge)
    return list(primary_by_descendant.values())


def clone_node_for_edge_thread(node: Dict, edge: Optional[Dict] = None) -> Dict:
    cloned = deepcopy(node)
    cloned["children"] = []
    if edge:
        cloned["edge_depth_from_seed"] = edge.get("depth_from_seed")
        cloned["inherited_candidate_topics"] = edge.get("inherited_candidate_topics") or []
        cloned["seed_query"] = edge.get("seed_query") or cloned.get("seed_query")
        cloned["seed_topics"] = edge.get("seed_candidate_topics") or cloned.get("seed_topics")
    return cloned


def parse_ndjson_threads_with_edges(posts_path: Path, edges_path: Path) -> List[Dict]:
    posts_by_id = load_post_nodes(posts_path)
    return build_threads_from_posts_and_edges(posts_by_id, load_edge_docs(edges_path))










def load_threads(input_path: Path, edges_path: Optional[Path] = None) -> List[Dict]:
    if input_path.suffix.lower() == ".json":
        with input_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    if input_path.suffix.lower() in {".ndjson", ".jsonl"}:
        if edges_path:
            return parse_ndjson_threads_with_edges(input_path, edges_path)
        return parse_ndjson_threads(input_path)

    if input_path.suffix.lower() == ".md":
        return parse_md_reply_threads(input_path)

    raise ValueError("Input file must be .json, .jsonl, .ndjson, or .md")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        default="output/reply_threads_hierarchy.md",
        help="Input .md/.json thread hierarchy file or raw posts .ndjson/.jsonl file.",
    )

    parser.add_argument(
        "--edges",
        help="Optional raw edges .ndjson/.jsonl file. When set with posts NDJSON input, seed-thread reconstruction uses edge records.",
    )

    parser.add_argument(
        "--output-json",
        default="output/thread_sentiment_annotated.json",
    )

    parser.add_argument(
        "--output-jsonl",
        default="output/thread_sentiment_flat.jsonl",
    )

    parser.add_argument(
        "--model",
        default="cardiffnlp/twitter-roberta-base-sentiment-latest",
    )

    parser.add_argument(
        "--no-transformers",
        action="store_true",
        help="Use VADER only.",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_json_path = Path(args.output_json)
    output_jsonl_path = Path(args.output_jsonl)

    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    edges_path = Path(args.edges) if args.edges else None
    threads = load_threads(input_path, edges_path=edges_path)

    print(f"Loaded threads: {len(threads)}")

    model = SentimentModel(
        model_name=args.model,
        use_transformers=not args.no_transformers,
    )

    annotated_threads = []
    all_rows = []

    for i, thread in enumerate(threads, start=1):
        annotated, rows = annotate_thread(thread, model)

        for row in rows:
            row["thread_index"] = i
            row["thread_seed_post_id"] = thread.get("post_id")
            row["thread_seed_query"] = thread.get("seed_query")
            row["thread_seed_type"] = thread.get("seed_type")

        annotated_threads.append(annotated)
        all_rows.extend(rows)

    with output_json_path.open("w", encoding="utf-8") as f:
        json.dump(annotated_threads, f, ensure_ascii=False, indent=2)

    with output_jsonl_path.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Annotated tree written to: {output_json_path}")
    print(f"Flat JSONL written to: {output_jsonl_path}")
    print(f"Total threads: {len(annotated_threads)}")
    print(f"Total posts: {len(all_rows)}")


if __name__ == "__main__":
    main()
