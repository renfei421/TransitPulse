"""Budgeted, deterministic GDELT URL discovery by adaptive time slicing.

A full 250-result window is subdivided; this improves coverage without claiming
complete access to GDELT's archive. The API's retention and result limits still
apply. Saturated leaf windows and request-budget truncation are reported.
"""
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import time
from urllib.parse import urlsplit

from backend.common.http import get
from backend.common.logging import get_logger
from backend.news_harvester import config

log=get_logger("gdelt_discovery")


def discover(query,day,*,request_budget=24,min_window_minutes=15,fetch=None,sleep=time.sleep):
    if request_budget<1 or min_window_minutes<1:
        raise ValueError("Positive request/time-window budgets are required")
    fetch=fetch or (lambda params:get(config.GDELT_DOC_API,params=params,timeout=config.REQUEST_TIMEOUT).json())
    start=datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    queue=deque([(start,start+timedelta(days=1))])
    urls={}
    requests_used=0
    saturated=0
    while queue and requests_used<request_budget:
        begin,end=queue.popleft()
        params={"query":query,"mode":"artlist","format":"json","maxrecords":250,"sort":"datedesc",
                "startdatetime":begin.strftime("%Y%m%d%H%M%S"),
                "enddatetime":(end-timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")}
        payload=fetch(params)
        requests_used+=1
        articles=payload.get("articles") or []
        for article in articles:
            url=article.get("url")
            if url and urlsplit(url).scheme in {"http","https"}:
                urls[url]={"url":url,"title":article.get("title") or "",
                           "seendate":article.get("seendate") or article.get("seenDate") or ""}
        if len(articles)>=250:
            if (end-begin).total_seconds()>min_window_minutes*60:
                middle=begin+(end-begin)/2
                queue.extend([(begin,middle),(middle,end)])
            else:
                saturated+=1
        if queue and requests_used<request_budget:
            sleep(config.GDELT_SLEEP_SECONDS)
    # Deterministic hash ordering avoids always taking the newest candidate in
    # the accessible URL pool. It does not remove inaccessible-publisher bias.
    candidates=sorted(urls.values(),key=lambda a:hashlib.sha256(a["url"].encode()).hexdigest())
    metadata={"request_windows":requests_used,"unique_candidates":len(candidates),
              "unvisited_windows":len(queue),"saturated_leaf_windows":saturated,
              "window_search_exhausted":not queue and not saturated}
    log.info("discovery_complete",extra={"fields":{"date":day,**metadata}})
    return candidates,metadata
