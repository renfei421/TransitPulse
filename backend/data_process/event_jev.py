"""Bounded concurrent Jev processing with durable per-request cost reservations.

Elasticsearch stores reservations before HTTP calls and native responses before
processed records. Replays hydrate the exact cache and avoid paying twice for
responses already received. Uncertain failed requests retain their upper bound.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from elasticsearch import helpers
import requests

from backend.common.es import get_client, bulk_documents
from backend.common.settings import index_name
from backend.data_process.jev_sentiment import (JevSentimentModel, JevError, processed_document,
    request_payload, validate_response, MODEL, CONTRACT_SHA256, INPUT_USD_PER_M)
from backend.ingestion.import_ndjson import atomic_json, fingerprint

MAX_PAID_ATTEMPTS_PER_DOCUMENT = 3
RETRY_POLICY = 'transient-3-total-v1'


def transient_failure(exc):
    """Only transport failures and explicit transient HTTP statuses may retry.

    Parsing, credentials, TLS verification, ES writes and budget errors fail
    closed. A saved native response must be reviewed/replayed, never repurchased.
    """
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return isinstance(exc, JevError) and bool(re.fullmatch(
        r'Jev returned HTTP (408|500|502|503|504|520|521|522|523|524|529); no automatic retry', str(exc)))


def now():
    return datetime.now(timezone.utc).isoformat()


def reservation(text):
    return (len(json.dumps(request_payload(text),ensure_ascii=False).encode())+2048)*INPUT_USD_PER_M/1_000_000


class Budget:
    def __init__(self, maximum, spent=0):
        self.maximum, self.accounted = maximum, spent
        self.lock=threading.Lock()

    def reserve(self, amount):
        with self.lock:
            if self.accounted+amount > self.maximum:
                raise JevError('Experiment inference budget exhausted')
            self.accounted+=amount

    def settle(self, reserved, actual):
        with self.lock:
            if actual > reserved+1e-12:
                raise JevError('Inference exceeded conservative cost reservation')
            self.accounted+=actual-reserved


def run(plan, input_path, cohort, output, *, workers=4, requests_per_second=8, budget_usd=None, es=None):
    if not 1 <= workers <= 4 or not 0 < requests_per_second <= 10:
        raise ValueError('Use 1..4 workers and at most ten requests per second')
    if fingerprint(input_path)!=cohort['input_sha256']:
        raise ValueError('Frozen cohort input checksum mismatch')
    if plan['model']!=MODEL or plan['rubric_version']!='transport-jev-v2':
        raise ValueError('Experiment and inference model contract disagree')
    maximum=plan['maximum_inference_cost_usd'] if budget_usd is None else budget_usd
    if isinstance(maximum,bool) or not math.isfinite(maximum) or maximum<=0:
        raise ValueError('Inference budget must be a positive finite amount')
    es=es or get_client()
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    cache=output/'jev-cache'; cache.mkdir(exist_ok=True)
    experiment=plan['experiment_id']
    query={'query':{'term':{'experiment_id':experiment}}}
    old={hit['_id']:hit['_source'] for hit in helpers.scan(es,index=index_name('social_posts_jev'),query=query,
           _source=['doc_id','jev.input_sha256','jev.contract_sha256'])}
    budget=Budget(maximum)
    paid_attempts=Counter()
    # Stream durable receipts: retaining every native response in memory makes
    # recovery increasingly expensive as the historical cohort grows.
    for hit in helpers.scan(es,index=index_name('jev_inference_attempts'),query=query,size=100):
        row=hit['_source']
        # Include failed/uncertain reservations so restarting cannot reset the
        # three-call ceiling. Cached responses remain free to replay at the cap.
        paid_attempts[row['doc_id']]+=1
        saved=row.get('response_cache')
        if row['status']=='failed' and saved:
            # Recheck a retained native HTTP 200 response with the current
            # protocol validator before allowing another paid request.
            if saved.get('input_sha256')!=row['input_sha256'] or saved.get('contract_sha256')!=CONTRACT_SHA256:
                raise JevError('Failed receipt identity mismatch; offline review required')
            validated=validate_response(saved['response'])
            actual=validated['usage']['input_tokens']*INPUT_USD_PER_M/1_000_000
            if actual>row['reserved_usd']+1e-12:
                raise JevError('Retained response exceeds its reservation; offline review required')
            row.update(status='succeeded',accounted_usd=actual,settled_at=now(),
                input_tokens=validated['usage']['input_tokens'],latency_seconds=saved['latency_seconds'])
            # Keep original error_type/error_reason for the recovery audit.
            es.index(index=index_name('jev_inference_attempts'),id=row['attempt_id'],document=row)
        budget.accounted+=row['accounted_usd']
        old_meta=old.get(row['doc_id'],{}).get('jev',{})
        if old_meta.get('contract_sha256')==CONTRACT_SHA256 and old_meta.get('input_sha256')==row['input_sha256']:
            continue
        if row.get('status')=='succeeded' and row.get('contract_sha256')==CONTRACT_SHA256:
            if saved:
                key=hashlib.sha256((MODEL+CONTRACT_SHA256+saved['input_sha256']).encode()).hexdigest()
                atomic_json(cache/(key+'.json'),saved)
    with Path(input_path).open(encoding='utf-8') as stream:
        docs=[json.loads(line) for line in stream if line.strip()]
    pending=[]
    for doc in docs:
        old_meta=old.get(doc['doc_id'],{}).get('jev',{})
        if old_meta.get('contract_sha256')==CONTRACT_SHA256 and old_meta.get('input_sha256')==hashlib.sha256(doc['raw_text'].encode()).hexdigest():
            continue
        pending.append(doc)
    stop=threading.Event()
    pace_lock=threading.Lock()
    next_request=[time.monotonic()]
    started=time.monotonic()
    report={'experiment_id':experiment,'model':MODEL,'contract_sha256':CONTRACT_SHA256,
        'input_sha256':cohort['input_sha256'],'total_records':len(docs),'already_processed':len(docs)-len(pending),
        'succeeded_this_run':0,'failed_this_run':0,'new_api_calls':0,'cache_hits':0,'errors':[],
        'cost_cap_usd':budget.maximum,'original_plan_cost_cap_usd':plan['maximum_inference_cost_usd'],
        'accounted_cost_usd':budget.accounted,'workers':workers,
        'requests_per_second_limit':requests_per_second,'started_at':now(),'status':'running',
        'retry_policy':RETRY_POLICY,'transient_request_failures_this_run':0}

    def save_report():
        report.update(accounted_cost_usd=budget.accounted,elapsed_seconds=round(time.monotonic()-started,2),updated_at=now())
        es.index(index=index_name('experiment_runs'),id=experiment,document={
            'experiment_id':experiment,'status':report['status'],'updated_at':now(),'model_name':MODEL,'report':report})
        atomic_json(output/'run.json',report)

    def process_once(doc):
        if stop.is_set():
            return {'skipped':True}
        text=doc['raw_text']
        content_hash=hashlib.sha256(text.encode()).hexdigest()
        key=hashlib.sha256((MODEL+CONTRACT_SHA256+content_hash).encode()).hexdigest()
        cached=(cache/(key+'.json')).is_file()
        cost=reservation(text) if not cached else 0
        attempt=None
        try:
            if not cached:
                if paid_attempts[doc['doc_id']] >= MAX_PAID_ATTEMPTS_PER_DOCUMENT:
                    raise JevError('Durable per-document request limit reached; external review required')
                budget.reserve(cost)
                attempt={'attempt_id':uuid4().hex,'doc_id':doc['doc_id'],'experiment_id':experiment,
                    'contract_sha256':CONTRACT_SHA256,'input_sha256':content_hash,'status':'reserved',
                    'started_at':now(),'reserved_usd':cost,'accounted_usd':cost}
                # A process crash after this write is charged conservatively until reconciled.
                es.index(index=index_name('jev_inference_attempts'),id=attempt['attempt_id'],document=attempt)
                paid_attempts[doc['doc_id']]+=1
                with pace_lock:
                    delay=max(0,next_request[0]-time.monotonic())
                    if delay:
                        time.sleep(delay)
                    next_request[0]=time.monotonic()+1/requests_per_second
            client=JevSentimentModel(budget_usd=2,confidence_threshold=plan['confidence_threshold'],cache_directory=cache)
            result=client.classify(text)
            if attempt:
                actual=client.estimated_cost
                saved=json.loads((cache/(key+'.json')).read_text(encoding='utf-8'))
                attempt.update(status='succeeded',settled_at=now(),accounted_usd=actual,
                    input_tokens=result['usage']['input_tokens'],latency_seconds=result['latency_seconds'],response_cache=saved)
                es.index(index=index_name('jev_inference_attempts'),id=attempt['attempt_id'],document=attempt)
                budget.settle(cost,actual)
            row=processed_document(doc,result)
            # The generic adapter intentionally clears old events. Restore this explicit study's event.
            for field in ('nearest_event_id','nearest_event_name','days_from_event','event_period'):
                row[field]=doc.get(field)
            row.update(processed_at=now(),processing_run_id=experiment+'-'+CONTRACT_SHA256[:12])
            bulk_documents('social_posts_jev',[row],client=es)
            return {'succeeded':True,'cache_hit':result['cache_hit'],'api_call':bool(attempt)}
        except Exception as exc:
            reason=str(exc) if isinstance(exc,JevError) else type(exc).__name__
            if attempt and attempt.get('status')!='succeeded':
                attempt.update(status='failed',settled_at=now(),error_type=type(exc).__name__,error_reason=reason)
                # Persist any native 200 response for offline protocol review, even when rejected.
                attempts=sorted((cache/'attempts').glob(key+'-*.json'),key=lambda p:p.stat().st_mtime) if (cache/'attempts').exists() else []
                if attempts:
                    attempt['response_cache']=json.loads(attempts[-1].read_text(encoding='utf-8'))
                es.index(index=index_name('jev_inference_attempts'),id=attempt['attempt_id'],document=attempt)
            retryable=bool(attempt and attempt.get('status')=='failed' and not attempt.get('response_cache') and transient_failure(exc))
            return {'succeeded':False,'doc_id':doc['doc_id'],'error_type':type(exc).__name__,
                'reason':reason,'api_call':bool(attempt),'retryable':retryable}

    def process(doc):
        calls=0
        transient_errors=0
        previous=None
        while True:
            outcome=process_once(doc)
            calls+=int(outcome.get('api_call',False))
            if outcome.get('skipped'):
                return previous or outcome
            if outcome.get('retryable'):
                transient_errors+=1
            outcome.update(api_call=calls,transient_request_failures=transient_errors)
            previous=outcome
            if outcome['succeeded']:
                return outcome
            if not outcome.get('retryable') or paid_attempts[doc['doc_id']]>=MAX_PAID_ATTEMPTS_PER_DOCUMENT:
                stop.set()
                return outcome
            # Cooperative bounded backoff: another worker's terminal failure
            # cancels waiting retries. Each retry gets its own durable reserve.
            delay=5 * 2 ** (paid_attempts[doc['doc_id']]-1)
            if stop.wait(delay):
                return outcome

    save_report()
    iterator=iter(pending)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active={pool.submit(process,doc) for doc in [next(iterator,None) for _ in range(workers)] if doc is not None}
        while active:
            finished,active=wait(active,return_when=FIRST_COMPLETED)
            for future in finished:
                outcome=future.result()
                if outcome.get('skipped'):
                    continue
                report['new_api_calls']+=int(outcome.get('api_call',False))
                report['transient_request_failures_this_run']+=outcome.get('transient_request_failures',0)
                if outcome['succeeded']:
                    report['succeeded_this_run']+=1
                    report['cache_hits']+=int(outcome['cache_hit'])
                else:
                    report['failed_this_run']+=1
                    report['errors'].append(outcome)
                if not stop.is_set():
                    doc=next(iterator,None)
                    if doc is not None:
                        active.add(pool.submit(process,doc))
            attempted=report['succeeded_this_run']+report['failed_this_run']
            if attempted%100==0 or not active:
                save_report()
                print(json.dumps({k:v for k,v in report.items() if k!='errors'}),flush=True)
    report['unattempted']=len(pending)-report['succeeded_this_run']-report['failed_this_run']
    report['status']='complete' if not report['failed_this_run'] and not report['unattempted'] else 'incomplete'
    save_report()
    print(json.dumps(report),flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--input',type=Path,required=True)
    p.add_argument('--cohort',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workers',type=int,default=4)
    p.add_argument('--requests-per-second',type=float,default=8)
    p.add_argument('--budget-usd',type=float,help='Explicit operational cost ceiling across all resumptions; preserves the frozen research plan')
    a=p.parse_args()
    result=run(json.loads(a.plan.read_text()),a.input,json.loads(a.cohort.read_text()),a.output,
               workers=a.workers,requests_per_second=a.requests_per_second,budget_usd=a.budget_usd)
    if result['status']!='complete':
        raise SystemExit(1)


if __name__=='__main__':
    main()
