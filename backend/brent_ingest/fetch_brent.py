"""Idempotent EIA Brent ingestion with previous-observation (not calendar-day) returns."""
from datetime import date, timedelta
import argparse
import json
import math
import os
from backend.common.es import get_client, bulk_documents
from backend.common.http import get as retry_get
from backend.common.settings import secret, index_name
from backend.common.time import utc_now
from backend.common.logging import get_logger

SERIES_ID = "PET.RBRTE.D"
DATA_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
log = get_logger("brent")


def fetch_eia(start, end):
    key = secret("EIA_API_KEY")
    if not key:
        raise ValueError("EIA_API_KEY is required")
    # Two weeks includes ordinary weekends and holidays before the first price.
    first = start-timedelta(days=14)
    # The legacy series-ID translation currently ignores start/end. The native
    # v2 data route applies explicit daily frequency, facet and date filters.
    response = retry_get(DATA_URL,
        params={"api_key": key, "frequency": "daily", "data[0]": "value",
                "facets[series][]": "RBRTE", "start": first.isoformat(),
                "end": end.isoformat(), "length": 5000,
                "sort[0][column]": "period", "sort[0][direction]": "asc"}, timeout=30)
    payload = response.json().get("response", {})
    rows = payload.get("data", [])
    if int(payload.get("total", len(rows))) > len(rows):
        raise ValueError("EIA response truncated; partition the requested date range")
    if any(not first <= date.fromisoformat(row["period"][:10]) <= end for row in rows):
        raise ValueError("EIA returned observations outside the requested date range")
    return rows


def build_docs(rows, start, previous=None):
    prices = {}
    for row in rows:
        if row.get("value") in (None, ""):
            continue
        price = float(row["value"])
        if not math.isfinite(price) or price <= 0:
            continue
        prices[date.fromisoformat(row["period"][:10])] = price
    docs = []
    for day, price in sorted(prices.items()):
        pct = (price/previous-1)*100 if previous else None
        previous = price
        if day < start:
            continue
        doc = {"id": f"BRENT_{day.isoformat()}", "date": day.isoformat(), "ticker": "BRENT",
               "price": price, "source": "EIA", "source_url": "https://www.eia.gov/dnav/pet/hist/RBRTED.htm",
               "return_definition": "percentage change since previous available price",
               "fetched_at": utc_now(), "dataset_kind": "live"}
        # Omit an unknown return so a sparse replay cannot erase a known one.
        if pct is not None:
            doc["daily_return_pct"] = round(pct, 6)
        docs.append(doc)
    return docs


def run(start=None, end=None, es=None):
    es = es or get_client()
    end = date.fromisoformat(end) if end else date.today()
    start = date.fromisoformat(start) if start else end-timedelta(days=14)
    if start > end or (end-start).days > 3660:
        raise ValueError("Use an ordered window of at most ten years")
    rows = fetch_eia(start, end)
    if not rows:
        raise RuntimeError("EIA returned no observations")
    first = min(row["period"][:10] for row in rows)
    stored = es.search(index=index_name("oil_prices_raw"), size=1, sort=[{"date": "desc"}],
        query={"bool": {"filter": [{"term": {"ticker": "BRENT"}}, {"range": {"date": {"lt": first}}}]}})
    hits = stored["hits"]["hits"]
    previous = hits[0]["_source"]["price"] if hits else None
    counts = bulk_documents("oil_prices_raw", build_docs(rows, start, previous), "id", client=es)
    result = {"status": "success", "start": start.isoformat(), "end": end.isoformat(), "writes": counts.as_dict()}
    log.info("brent_ingested", extra={"fields": result})
    return result


def main():
    from flask import request
    try:
        return run(request.args.get("from"), request.args.get("to")), 200
    except ValueError:
        return {"error": "Invalid configuration or date range"}, 400
    except Exception:
        log.exception("brent_failed")
        return {"error": "Upstream data service unavailable"}, 503


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=os.getenv("START_DATE"))
    parser.add_argument("--end", default=os.getenv("END_DATE"))
    args = parser.parse_args()
    print(json.dumps(run(args.start, args.end)))
