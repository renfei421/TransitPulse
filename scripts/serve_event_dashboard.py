"""Loopback-only observation page backed by the cloud Fission read API.

The browser receives no Kubernetes/Elasticsearch/provider credentials. All proxy
requests are pinned to the frozen experiment; arbitrary URLs and writes are not
supported. Run separately from the existing synthetic demo on port 9090.
"""
import argparse
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_file
import requests

from scripts.cloud_elasticsearch import Cluster, CONTEXT
from scripts.cloud_fission import router

ROOT=Path(__file__).resolve().parents[1]
RESOURCES={'experiments/status','social/posts','social/target-sentiment','quality','oil/prices'}
TOPICS={'fuel_price','public_transport','ev','oil_vehicle'}


class ReconnectingRouter:
    """Rebuild only the local read tunnel, with one attempt per 30 seconds.

    A generation check prevents requests from an old connection from tearing
    down a newly recovered tunnel. Cloud HTTP errors do not trigger reconnects.
    No model calls or Kubernetes workload changes are made here.
    """

    def __init__(self, cluster, factory=router, clock=time.monotonic):
        self.cluster, self.factory, self.clock = cluster, factory, clock
        self.lock = threading.Lock()
        self.context = self.session = self.upstream = None
        self.broken = True
        self.next_attempt = 0
        self.generation = 0

    def get(self, url, **kwargs):
        with self.lock:
            if self.broken:
                if self.clock() < self.next_attempt:
                    raise requests.ConnectionError('Local tunnel reconnect is waiting for backoff')
                self.next_attempt = self.clock() + 30
                if self.context is not None:
                    self.context.__exit__(None, None, None)
                    self.context = None
                candidate = self.factory(self.cluster, startup_timeout=15)
                try:
                    self.session, self.upstream = candidate.__enter__()
                except (OSError, RuntimeError, requests.RequestException) as exc:
                    logging.getLogger(__name__).warning('Cloud read tunnel unavailable (%s)', type(exc).__name__)
                    raise requests.ConnectionError('Local cloud tunnel could not reconnect') from None
                self.context = candidate
                self.broken = False
                self.generation += 1
                logging.getLogger(__name__).info('Cloud read tunnel connected; generation=%s', self.generation)
            session, upstream, generation = self.session, self.upstream, self.generation
        try:
            return session.get(upstream + urlsplit(url).path, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            with self.lock:
                if generation == self.generation:
                    self.broken = True
            raise

    def close(self):
        with self.lock:
            if self.context is not None:
                self.context.__exit__(None, None, None)
                self.context = None
            self.broken = True


def configuration(directory):
    directory=Path(directory)
    read=lambda name:json.loads((directory/name).read_text(encoding='utf-8'))
    plan,cohort,budget=read('plan.json'),read('cohort.json'),read('execution-budget.json')
    with sqlite3.connect(f'{(directory/"collection.sqlite").resolve().as_uri()}?mode=ro',uri=True) as db:
        daily=[{'platform':p,'date':day,'count':count} for p,day,count in db.execute(
            'SELECT platform,substr(created_at,1,10),count(*) FROM docs GROUP BY 1,2 ORDER BY 2,1')]
    return {'experiment_id':plan['experiment_id'],'from':plan['start'],'to':plan['end'],
        'event_date':plan['event_date'],'model':plan['model'],'timezone':'UTC',
        'raw_records':cohort['raw_records'],'eligible_records':cohort['eligible_records'],
        'eligible_by_platform':cohort['eligible_by_platform'],'daily_raw':daily,
        'cost_cap_usd':budget['maximum_inference_cost_usd'],
        'minimum_daily_scores':plan['analysis']['minimum_target_scores_per_day'],
        'collection_gaps':cohort['collection_gaps']}


def create_app(upstream, config, get=None):
    parts=urlsplit(upstream)
    if parts.scheme!='http' or parts.hostname!='127.0.0.1' or parts.path not in ('','/') or parts.query or parts.fragment or parts.username:
        raise ValueError('Dashboard upstream must be a local authenticated kubectl tunnel')
    app=Flask(__name__,static_folder=None)
    session=requests.Session()
    session.trust_env=False
    fetch=get or session.get

    @app.after_request
    def response_headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        return response

    @app.get('/')
    def home():
        return send_file(ROOT/'frontend/event_dashboard.html')

    @app.get('/dashboard-config')
    def settings():
        return jsonify(config)

    @app.get('/api/v1/<path:resource>')
    def proxy(resource):
        if resource not in RESOURCES:
            return jsonify(error='Resource not available in this observation page'),404
        if resource=='experiments/status':
            params={'experiment_id':config['experiment_id']}
        else:
            params={'from':config['from'],'to':config['to'],'dataset_kind':'live'}
            if resource=='oil/prices':
                params['source_dataset']='eia_brent_daily'
            else:
                params.update(timezone='UTC',model=config['model'],experiment_id=config['experiment_id'])
                platform=request.args.get('platform')
                if platform:
                    if platform not in ('bluesky','mastodon'):
                        return jsonify(error='Invalid platform'),400
                    params['platform']=platform
                topic=request.args.get('topic')
                if topic:
                    if topic not in TOPICS:
                        return jsonify(error='Invalid target'),400
                    params['topic']=topic
                if resource=='social/posts':
                    params['limit']='8'
                    cursor=request.args.get('cursor')
                    if cursor:
                        if len(cursor)>1024:
                            return jsonify(error='Invalid cursor'),400
                        params['cursor']=cursor
        try:
            result=fetch(upstream.rstrip('/')+'/api/v1/'+resource,params=params,timeout=(3,25),allow_redirects=False)
            if result.status_code not in (200,400,404,503):
                return jsonify(error='Cloud API is temporarily unavailable'),502
            # Do not forward cookies, redirect locations, or browser-controlled headers.
            return Response(json.dumps(result.json(),ensure_ascii=False),status=result.status_code,mimetype='application/json')
        except (requests.RequestException,ValueError):
            return jsonify(error='Cloud connection interrupted; last successful snapshot remains on screen'),502
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--kubeconfig')
    source.add_argument('--local-api',help='Loopback API over a restored private archive; no cloud credentials')
    parser.add_argument('--directory',type=Path,default=Path('data/experiments/iran-20260228'))
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    config=configuration(args.directory)
    if args.local_api:
        config['mode']='replay'
        create_app(args.local_api,config).run(host='127.0.0.1',port=args.port,
            debug=False,use_reloader=False,threaded=True)
        return
    cluster=Cluster(args.kubeconfig,CONTEXT)
    tunnel = ReconnectingRouter(cluster)
    try:
        print(f'Observation page: http://127.0.0.1:{args.port}',flush=True)
        create_app('http://127.0.0.1',config,get=tunnel.get).run(
            host='127.0.0.1',port=args.port,debug=False,use_reloader=False,threaded=True)
    finally:
        tunnel.close()


if __name__=='__main__':
    main()
