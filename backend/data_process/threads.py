"""Reconstruct discussion forests without duplicating or silently losing nodes."""
from copy import deepcopy


def assign_depths_and_sort(roots):
    roots.sort(key=lambda n:str(n.get("created_at") or ""))
    stack = [(root,0) for root in roots]
    visited = set()
    while stack:
        node,depth = stack.pop()
        key = node.get("post_id") or id(node)
        if key in visited:
            raise ValueError("Cycle or duplicate node in discussion graph")
        visited.add(key)
        node["md_depth"] = depth
        node.setdefault("children", []).sort(key=lambda n:str(n.get("created_at") or ""))
        stack.extend((child,depth+1) for child in node["children"])
    return roots


def build_threads_from_post_nodes(nodes):
    copied = {key:{**deepcopy(value), "children":[]} for key,value in nodes.items()}
    roots = []
    # Validate all parent chains, including isolated cycles with no root.
    complete = set()
    for identifier in copied:
        cursor = identifier
        path = set()
        while cursor in copied and cursor not in complete:
            if cursor in path:
                raise ValueError("Cycle in raw parent-post relationships")
            path.add(cursor)
            cursor = copied[cursor].get("parent_post_id")
        complete.update(path)
    for node in copied.values():
        parent = copied.get(node.get("parent_post_id"))
        node["context_missing_parent"] = bool(node.get("parent_post_id") and parent is None)
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return assign_depths_and_sort(roots)


def build_threads_from_posts_and_edges(nodes, edges):
    copied = {key:deepcopy(value) for key,value in nodes.items()}
    # Edges add provenance/topic candidates; the platform's direct parent relation
    # remains authoritative when present.
    for edge in sorted(edges, key=lambda e:(int(e.get("depth_from_seed") or 0),str(e.get("seed_post_id")))):
        node = copied.get(edge.get("descendant_post_id"))
        if node is None:
            continue
        inherited = set(node.get("inherited_candidate_topics") or [])
        inherited.update(edge.get("inherited_candidate_topics") or edge.get("seed_candidate_topics") or [])
        node["inherited_candidate_topics"] = sorted(inherited)
        node.setdefault("seed_query", edge.get("seed_query"))
        node["seed_topics"] = sorted(set(node.get("seed_topics") or []) | inherited)
        if not node.get("parent_post_id"):
            parent = edge.get("parent_post_id") or edge.get("seed_post_id")
            if parent != node.get("post_id"):
                node["parent_post_id"] = parent
    return build_threads_from_post_nodes(copied)
