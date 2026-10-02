"""Social storage adapter; mappings are provisioned by database.migrate."""
from backend.common.es import bulk_documents, get_client
from backend.common.settings import index_name

# Preserve seed metadata when a seed is revisited as a reply; union collection
# methods AFTER updating the document, otherwise putAll discards the union.
UPSERT_SCRIPT = """
def methods = new ArrayList();
if (ctx._source.collection_methods != null) { methods.addAll(ctx._source.collection_methods); }
if (params.doc.collection_methods != null) {
  for (def m : params.doc.collection_methods) { if (!methods.contains(m)) { methods.add(m); } }
}
if (ctx._source.is_seed_post == true && params.doc.is_seed_post == false) {
  params.doc.is_seed_post = true;
  for (def k : ['seed_type', 'candidate_topics', 'direct_candidate_topics', 'matched_keywords', 'topic_count', 'reply_has_keyword']) {
    if (ctx._source.containsKey(k)) { params.doc[k] = ctx._source[k]; }
  }
}
ctx._source.putAll(params.doc);
ctx._source.collection_methods = methods;
"""


def bulk_index(index, docs, id_field):
    return bulk_documents(index, docs, id_field)


def bulk_upsert_posts(index, docs):
    return bulk_documents(index, docs, script=UPSERT_SCRIPT)


def get_index_fields(index):
    name = index_name(index)
    response = get_client().indices.get_mapping(index=name)
    return set(response[name]["mappings"]["properties"])
