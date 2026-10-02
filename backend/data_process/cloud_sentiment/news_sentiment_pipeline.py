"""Batched news inference with provenance and explicit input truncation metadata."""
import argparse
import json
import time
import uuid
from elasticsearch.helpers import scan
from backend.common.es import get_client, bulk_documents, WriteCounts
from backend.common.settings import index_name
from backend.common.logging import get_logger, run_id
from backend.common.time import range_query, resolve_range, utc_now
from backend.data_process.sentiment import MODEL_NAME, PIPELINE_VERSION, SentimentModel
from backend.data_process.cloud_sentiment.cloud_sentiment_pipeline import batched

log=get_logger("news_processing")


def article_text(source):
    # Headlines/lead are retained. Full-document sentiment is not claimed for
    # articles longer than the classifier's 512-token input capacity.
    return (str(source.get("title") or "")+"\n\n"+str(source.get("text") or "")).strip()


def parse_args(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-index",default="gdelt_news_raw")
    parser.add_argument("--processed-index",default="news_processed")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--window-hours",type=float)
    parser.add_argument("--time-field",choices=["time","fetched_at"],default="fetched_at")
    parser.add_argument("--scan-size",type=int,default=100)
    parser.add_argument("--bulk-size",type=int,default=64)
    parser.add_argument("--model",default=MODEL_NAME)
    parser.add_argument("--no-transformers",action="store_true")
    parser.add_argument("--dry-run",action="store_true")
    return parser.parse_args(argv)


def _run_pipeline(args, *, es=None, model=None):
    es=es or get_client()
    model=model or SentimentModel(args.model,use_transformers=not args.no_transformers)
    if model.pipe is not None:
        model.pipe.tokenizer.truncation_side="right"
    start,end=resolve_range(args.start_date,args.end_date,args.window_hours)
    identifier=run_id.get()
    began=time.monotonic()
    counts=WriteCounts()
    total=0
    hits=scan(es,index=index_name(args.raw_index),
              query={"query":range_query(args.time_field,start,end)},size=args.scan_size,scroll="30m")
    for group in batched(hits,args.bulk_size):
        texts=[article_text(h["_source"]) for h in group]
        results=model.classify_many(texts)
        docs=[]
        for hit,text,result in zip(group,texts,results):
            source=hit["_source"]
            docs.append({
                "id":source.get("id") or hit["_id"],"time":source.get("time"),
                "fetched_at":source.get("fetched_at"),"processed_at":utc_now(),
                "url":source.get("url"),"source_domain":source.get("source_domain"),
                "sentiment":result["label"],"sentiment_score":result["confidence"],
                "sentiment_polarity":result["polarity"],"sentiment_probabilities":result["probabilities"],
                "model_name":result["model"],"model_revision":result["revision"],"score_type":result["score_type"],
                "pipeline_version":PIPELINE_VERSION,"schema_version":2,"processing_run_id":identifier,
                "input_scope":"headline_and_lead_512_tokens" if model.pipe is not None else "full_text_lexicon",
                "input_characters":len(text),"dataset_kind":source.get("dataset_kind","live"),
                "source_dataset":source.get("source_dataset"),
            })
        if not args.dry_run:
            counts.add(bulk_documents(args.processed_index,docs,"id",client=es))
        total+=len(docs)
    summary={"run_id":identifier,"component":"news_processing","status":"success",
             "processed_rows":total,"writes":counts.as_dict(),"completed_at":utc_now(),
             "duration_seconds":round(time.monotonic()-began,3)}
    if not args.dry_run:
        bulk_documents("pipeline_runs",[summary],"run_id",client=es)
    log.info("processing_complete",extra={"fields":summary})
    return summary


def run_pipeline(args, *, es=None, model=None):
    es = es or get_client()
    identifier = "news_" + uuid.uuid4().hex
    token = run_id.set(identifier)
    try:
        return _run_pipeline(args, es=es, model=model)
    except Exception:
        log.exception("news_processing_failed")
        if not args.dry_run:
            try:
                bulk_documents("pipeline_runs", [{"run_id": identifier, "component": "news_processing",
                    "status": "failed", "completed_at": utc_now()}], "run_id", client=es)
            except Exception:
                log.exception("audit_write_failed")
        raise
    finally:
        run_id.reset(token)


def main():
    print(json.dumps(run_pipeline(parse_args()),allow_nan=False))


if __name__=="__main__":
    main()
