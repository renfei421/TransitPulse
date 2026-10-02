"""GDELT daily attention with zero preservation and intraday aggregation."""
import argparse
from collections import defaultdict
from datetime import date, timedelta
import json
import math
from backend.common.http import get
from backend.common.time import utc_now
from backend.common.logging import get_logger
from backend.news_harvester import config
from backend.news_harvester.es_client import bulk_upsert, ensure_indexes, stable_hash

log=get_logger("gdelt_volume")


def normalise_gdelt_date(value):
    digits="".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits)<8:
        return None
    try:
        return date(int(digits[:4]),int(digits[4:6]),int(digits[6:8])).isoformat()
    except ValueError:
        return None


def date_to_gdelt_datetime(value,end_of_day=False):
    return date.fromisoformat(value).strftime("%Y%m%d")+("235959" if end_of_day else "000000")


def first_present(obj,keys):
    return next((obj[key] for key in keys if obj.get(key) is not None),None)


def extract_volume_rows(payload,keyword):
    buckets={}
    def walk(value):
        if isinstance(value,dict):
            stamp=first_present(value,("date","datetime","time","timestamp"))
            count=first_present(value,("value","count","articles","volume","Volume"))
            day=normalise_gdelt_date(stamp)
            if day and count is not None:
                try:
                    count=float(count)
                    total=first_present(value,("norm","total","Total","monitored"))
                    total=float(total) if total is not None else None
                    if not math.isfinite(count) or count<0 or (total is not None and (not math.isfinite(total) or total<0)):
                        raise ValueError("Negative volume")
                    # Same raw timestamp repeated in a payload is one bucket.
                    buckets[str(stamp)]=(day,count,total)
                except (TypeError,ValueError):
                    log.warning("invalid_volume_bucket")
            for item in value.values():
                walk(item)
        elif isinstance(value,list):
            for item in value:
                walk(item)
    walk(payload)
    daily=defaultdict(lambda:{"volume":0.0,"total":0.0,"has_total":True,"buckets":0})
    for day,count,total in buckets.values():
        item=daily[day]
        item["volume"]+=count
        item["buckets"]+=1
        if total is None:
            item["has_total"]=False
        else:
            item["total"]+=total
    return [{
        "index_name":config.VOLUME_INDEX,"date":day,"keyword":keyword,"volume":item["volume"],
        "total_monitored":item["total"] if item["has_total"] else None,
        "share_percent":100*item["volume"]/item["total"] if item["has_total"] and item["total"]>0 else None,
        "bucket_count":item["buckets"],"fetched_at":utc_now(),"dataset_kind":"live",
    } for day,item in sorted(daily.items())]


def collect_volume_payload(query,start_date,end_date):
    return get(config.GDELT_DOC_API,params={
        "query":query,"mode":"timelinevolraw","format":"json",
        "startdatetime":date_to_gdelt_datetime(start_date),
        "enddatetime":date_to_gdelt_datetime(end_date,True),"timelinesmooth":"0",
    },timeout=config.REQUEST_TIMEOUT).json()


def volume_doc_id(record):
    return stable_hash(f"gdelt_volume|{record.get('keyword')}|{record.get('date')}")


def run_collector(start,end,keyword,query):
    if date.fromisoformat(start)>date.fromisoformat(end):
        raise ValueError("start must not follow end")
    ensure_indexes()
    rows=extract_volume_rows(collect_volume_payload(query,start,end),keyword)
    writes=bulk_upsert(config.VOLUME_INDEX,rows,volume_doc_id)
    result={"status":"success","records_extracted":len(rows),"writes":writes}
    log.info("volume_complete",extra={"fields":result})
    return result


def main(context=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start",default=(date.today()-timedelta(days=30)).isoformat())
    parser.add_argument("--end",default=date.today().isoformat())
    parser.add_argument("--keyword",default=config.GDELT_KEYWORD)
    parser.add_argument("--query",default=config.GDELT_QUERY)
    args=parser.parse_args()
    return json.dumps(run_collector(args.start,args.end,args.keyword,args.query),allow_nan=False)


if __name__=="__main__":
    print(main())
