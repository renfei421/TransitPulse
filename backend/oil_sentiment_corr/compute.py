"""Scheduled exploratory correlation on calendar-aligned, signed sentiment.

Positive lag means price at date d is paired with sentiment at d + lag days.
Missing dates are not compressed or imputed. P-values are exploratory: daily
series may be autocorrelated. Bonferroni adjustment addresses lag searching only,
not that temporal-dependence assumption. No causal or forecasting claim is made.
"""
from datetime import date, timedelta
import argparse
import json
import math
import os

import numpy as np
from scipy import stats
from elasticsearch.helpers import scan

from backend.common.es import get_client, bulk_documents
from backend.common.settings import index_name
from backend.common.time import day_bounds, utc_now
from backend.common.logging import get_logger

log=get_logger("correlation")


def cohort_filter(kind):
    if kind == "observed":
        return {"bool":{"must_not":[{"terms":{"dataset_kind":["synthetic","fixture"]}}]}}
    return {"term":{"dataset_kind":kind}}


def fetch_oil(from_date,to_date,es=None,dataset_kind="observed"):
    es=es or get_client()
    query={"query":{"bool":{"filter":[{"term":{"ticker":"BRENT"}},
          {"range":{"date":{"gte":from_date,"lte":to_date}}},
          {"exists":{"field":"daily_return_pct"}},cohort_filter(dataset_kind)]}}}
    return {hit["_source"]["date"][:10]:float(hit["_source"]["daily_return_pct"])
            for hit in scan(es,index=index_name("oil_prices_raw"),query=query,size=500)}


def fetch_sentiment(from_date,to_date,es=None,dataset_kind="observed",model_name=None):
    es=es or get_client()
    start,end=day_bounds(from_date,to_date)
    response=es.search(index=index_name("social_posts_processed"),body={
        "size":0,"query":{"bool":{"filter":[
            {"range":{"created_at":{"gte":start,"lt":end}}},
            {"term":{"candidate_topics":"fuel_price"}},
            {"term":{"schema_version":2}},
            cohort_filter(dataset_kind),
            {"term":{"model_name":model_name}} if model_name else {"match_all":{}},
        ]}},
        "aggs":{"days":{"date_histogram":{"field":"created_at","calendar_interval":"day",
                "time_zone":"Australia/Sydney","min_doc_count":1},
                "aggs":{"labels":{"terms":{"field":"contextual_sentiment_label","size":3}}}}}
    })
    result={}
    for bucket in response["aggregations"]["days"]["buckets"]:
        counts={b["key"]:b["doc_count"] for b in bucket["labels"]["buckets"]}
        total=sum(counts.get(x,0) for x in ("positive","neutral","negative"))
        if total:
            result[bucket["key_as_string"][:10]]=(counts.get("positive",0)-counts.get("negative",0))/total
    return result


def aligned_pairs(oil_map,sentiment_map,lag):
    pairs=[]
    for key,value in sorted(oil_map.items()):
        shifted=(date.fromisoformat(key[:10])+timedelta(days=lag)).isoformat()
        other=sentiment_map.get(shifted)
        if other is not None and math.isfinite(float(value)) and math.isfinite(float(other)):
            pairs.append((key,shifted,float(value),float(other)))
    return pairs


def correlation(pairs, minimum_pairs):
    if len(pairs)<minimum_pairs:
        return None,None
    x=np.asarray([p[2] for p in pairs])
    y=np.asarray([p[3] for p in pairs])
    if np.ptp(x)<1e-12 or np.ptp(y)<1e-12:
        return None,None
    r,p=stats.pearsonr(x,y)
    return (round(float(r),6),round(float(p),8)) if np.isfinite(r) and np.isfinite(p) else (None,None)


def compute_stats(oil_map,sentiment_map,minimum_pairs=14,max_lag=7):
    if minimum_pairs<3 or max_lag<0:
        raise ValueError("minimum_pairs >= 3 and max_lag >= 0 are required")
    zero=aligned_pairs(oil_map,sentiment_map,0)
    if len(zero)<minimum_pairs:
        return None
    r,p=correlation(zero,minimum_pairs)
    windows=[]
    for lag in range(-max_lag,max_lag+1):
        pairs=aligned_pairs(oil_map,sentiment_map,lag)
        corr,pvalue=correlation(pairs,minimum_pairs)
        windows.append({"lag":lag,"corr":corr,"p_value":pvalue,"n_pairs":len(pairs)})
    tests=sum(item["p_value"] is not None for item in windows)
    for item in windows:
        item["p_value_bonferroni"]=min(1.0,item["p_value"]*tests) if item["p_value"] is not None else None
    valid=[item for item in windows if item["corr"] is not None]
    best=max(valid,key=lambda item:abs(item["corr"])) if valid else None
    return {
        "correlation_pearson":r,"p_value":p,"n_days_used":len(zero),
        "best_lag_days":best["lag"] if best else None,
        "p_value_bonferroni":best["p_value_bonferroni"] if best else None,
        "ccf_window":windows,"minimum_pairs":minimum_pairs,"lag_tests":tests,
        "lag_unit":"calendar_days","metric":"net_contextual_sentiment",
        "polarity_definition":"(positive-negative)/(positive+neutral+negative)",
        "interpretation":"Exploratory association only; positive lag means price leads. Serial dependence is not corrected.",
        "pipeline_version":"correlation-v2",
        "scatter_data":[{"date":a,"brent_return_pct":x,"oil_sentiment":y} for a,_,x,y in zero],
        "best_lag_scatter_data":[{"price_date":a,"sentiment_date":b,"brent_return_pct":x,"oil_sentiment":y}
            for a,b,x,y in aligned_pairs(oil_map,sentiment_map,best["lag"])] if best else [],
    }


def run(from_date,to_date,es=None,dataset_kind="observed",model_name="cardiffnlp/twitter-roberta-base-sentiment-latest"):
    es=es or get_client()
    result=compute_stats(fetch_oil(from_date,to_date,es,dataset_kind),fetch_sentiment(from_date,to_date,es,dataset_kind,model_name))
    if result is None:
        log.warning("insufficient_overlapping_dates")
        return {"status":"insufficient_data","minimum_pairs":14}
    doc={**result,"id":f"{from_date}_{to_date}_{dataset_kind}_{model_name}","computed_at":utc_now(),
         "from_date":from_date,"to_date":to_date,"dataset_kind":dataset_kind,"model_name":model_name}
    bulk_documents("oil_sentiment_corr_results",[doc],"id",client=es)
    log.info("correlation_complete",extra={"fields":{"n_days":result["n_days_used"],"pearson":result["correlation_pearson"]}})
    return doc


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date",default=os.getenv("FROM_DATE",(date.today()-timedelta(days=90)).isoformat()))
    parser.add_argument("--to-date",default=date.today().isoformat())
    parser.add_argument("--dataset-kind",default="observed",choices=["observed","live","imported","synthetic"])
    parser.add_argument("--model-name",default="cardiffnlp/twitter-roberta-base-sentiment-latest")
    args=parser.parse_args()
    print(json.dumps(run(args.from_date,args.to_date,dataset_kind=args.dataset_kind,model_name=args.model_name),allow_nan=False))


if __name__=="__main__":
    main()
