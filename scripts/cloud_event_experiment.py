"""Provision event schemas, ingest verified inputs, and submit a bounded Jev Job.

Uses the existing cluster and volumes. API credentials have their own Secrets;
only the Jev Secret is injected into inference, alongside the ES application role.
"""
import argparse
from copy import deepcopy
from datetime import date
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from uuid import uuid4

from elasticsearch import Elasticsearch, NotFoundError

from backend.brent_ingest.fetch_brent import fetch_eia, build_docs
from backend.common.es import create_documents, bulk_documents, CreateCounts, WriteCounts
from backend.ingestion.event_harvest import load_private
from backend.ingestion.import_ndjson import atomic_json, fingerprint
from scripts.cloud_elasticsearch import Cluster, CONTEXT, connection
from scripts.cloud_model_job import IMAGE

ROOT=Path(__file__).resolve().parents[1]


def prepare(cluster, plan, directory):
    load_private(ROOT/'data/private/additional-apis.env')
    jev=(ROOT/'data/private/typesafe-api-key.txt').read_text().strip()
    cluster.put_secret('transport-jev-secrets',{'TYPESAFE_API_KEY':jev})
    sources={key:os.environ[key] for key in ('EIA_API_KEY','BLUESKY_HANDLE','BLUESKY_APP_PASSWORD','YOUTUBE_API_KEY') if os.environ.get(key)}
    cluster.put_secret('transport-source-secrets',sources)
    migrated=[]
    app=cluster.secret('transport-secrets','default')
    schema=cluster.secret('transport-migration-secrets','default')
    with connection(cluster) as (_,url,ca):
        with Elasticsearch(url,basic_auth=(schema['ES_USER'],schema['ES_PASSWORD']),ca_certs=str(ca)) as es:
            for name in ('social_discussion_posts_raw','social_processing_decisions','social_posts_jev','jev_inference_attempts','experiment_runs'):
                definition=json.loads((ROOT/'database/mappings'/f'{name}.json').read_text())
                definition['settings']={'number_of_shards':1,'number_of_replicas':0}
                if es.indices.exists(index='v2_'+name):
                    es.indices.put_mapping(index='v2_'+name,body=definition['mappings'])
                    action='updated_mapping'
                else:
                    es.indices.create(index='v2_'+name,body=definition)
                    action='created'
                migrated.append({'index':'v2_'+name,'action':action})
        os.environ['ES_INDEX_PREFIX']='v2_'
        rows=fetch_eia(date.fromisoformat(plan['start']),date.fromisoformat(plan['end']))
        docs=[{**d,'source_dataset':'eia_brent_daily'} for d in build_docs(rows,date.fromisoformat(plan['start'])) if d['date']<=plan['end']]
        if not docs:
            raise RuntimeError('EIA returned no event-window prices')
        with Elasticsearch(url,basic_auth=(app['ES_USER'],app['ES_PASSWORD']),ca_certs=str(ca)) as es:
            counts=bulk_documents('oil_prices_raw',docs,'id',client=es)
    atomic_json(directory/'oil-prices.json',docs)
    report={'schema_changes':migrated,'cloud_secrets':['transport-jev-secrets','transport-source-secrets'],
        'oil_price_records':len(docs),'oil_first_date':docs[0]['date'],'oil_last_date':docs[-1]['date'],
        'oil_writes':counts.as_dict()}
    atomic_json(directory/'cloud-preparation.json',report)
    print(json.dumps(report),flush=True)
    return report


def ingest(cluster, directory):
    cohort=json.loads((directory/'cohort.json').read_text())
    if fingerprint(directory/'raw.ndjson')!=cohort['raw_sha256'] or fingerprint(directory/'decisions.ndjson')!=cohort['decisions_sha256']:
        raise ValueError('Frozen ingestion input checksum mismatch')
    app=cluster.secret('transport-secrets','default')
    os.environ['ES_INDEX_PREFIX']='v2_'
    results={}
    with connection(cluster) as (_,url,ca), Elasticsearch(url,basic_auth=(app['ES_USER'],app['ES_PASSWORD']),ca_certs=str(ca),request_timeout=60) as es:
        for filename,logical,create in (('raw.ndjson','social_discussion_posts_raw',True),('decisions.ndjson','social_processing_decisions',False)):
            counts=CreateCounts() if create else WriteCounts()
            batch=[]
            with (directory/filename).open(encoding='utf-8') as stream:
                for line in stream:
                    batch.append(json.loads(line))
                    if len(batch)==250:
                        counts.add(create_documents(logical,batch,client=es) if create else bulk_documents(logical,batch,client=es))
                        batch.clear()
            if batch:
                counts.add(create_documents(logical,batch,client=es) if create else bulk_documents(logical,batch,client=es))
            results[logical]=counts.as_dict()
            print(json.dumps({logical:counts.as_dict()}),flush=True)
    atomic_json(directory/'ingestion.json',results)
    return results


def package(directory):
    entries={}
    for folder in ('backend','database'):
        for path in (ROOT/folder).rglob('*'):
            if path.is_file() and path.suffix in ('.py','.json') and '__pycache__' not in path.parts and path.name!='config_private.py':
                entries['release/'+path.relative_to(ROOT).as_posix()]=path.read_bytes().replace(b'\r\n',b'\n')
    entries['release/requirements-fission.txt']=(ROOT/'requirements-fission.txt').read_bytes()
    for filename in ('input.ndjson','cohort.json','plan.json'):
        entries['release/experiment/'+filename]=(directory/filename).read_bytes()
    output=io.BytesIO()
    with gzip.GzipFile(fileobj=output,mode='wb',mtime=0) as gz, tarfile.open(fileobj=gz,mode='w') as archive:
        for name,data in sorted(entries.items()):
            item=tarfile.TarInfo(name); item.size=len(data); item.mode=0o644; item.mtime=0
            archive.addfile(item,io.BytesIO(data))
    sha=hashlib.sha256(output.getvalue()).hexdigest()
    path=ROOT/'artifacts'/('event-release-'+sha[:16]+'.tar.gz')
    path.write_bytes(output.getvalue())
    return path,sha


def manifest(name, checksum, experiment, budget_usd=None):
    env=[{'name':k,'value':v} for k,v in {'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1',
        'PYTHONPATH':'/work/vendor:/work/release','RELEASE_SHA256':checksum,'HOME':'/work','TMPDIR':'/tmp',
        'PIP_DISABLE_PIP_VERSION_CHECK':'1','ES_INDEX_PREFIX':'v2_'}.items()]
    mounts=[{'name':'work','mountPath':'/work'},{'name':'tmp','mountPath':'/tmp'}]
    security={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}
    bootstrap=('until test -f /work/release.ready; do sleep 2; done\n'
        'printf "%s  /work/release.tar.gz\\n" "$RELEASE_SHA256" | sha256sum -c -\n'
        'tar -xzf /work/release.tar.gz -C /work\n'
        'python -m pip install --no-cache-dir --require-hashes --no-compile --target /work/vendor -r /work/release/requirements-fission.txt')
    resources={'requests':{'cpu':'500m','memory':'512Mi','ephemeral-storage':'1Gi'},
               'limits':{'cpu':'2','memory':'2Gi','ephemeral-storage':'5Gi'}}
    arguments=['--plan','/work/release/experiment/plan.json','--input','/work/release/experiment/input.ndjson',
        '--cohort','/work/release/experiment/cohort.json','--output','/work/results','--workers','4','--requests-per-second','8']
    if budget_usd is not None:
        if isinstance(budget_usd,bool) or not math.isfinite(budget_usd) or budget_usd<=0:
            raise ValueError('Invalid operational budget')
        arguments.extend(['--budget-usd',str(budget_usd)])
    return {'apiVersion':'batch/v1','kind':'Job','metadata':{'name':name,'namespace':'default',
        'labels':{'app.kubernetes.io/part-of':'transport-analytics','experiment':experiment}},
        'spec':{'backoffLimit':0,'activeDeadlineSeconds':21600,'ttlSecondsAfterFinished':172800,
        'template':{'metadata':{'labels':{'app':'transport-event-jev','experiment':experiment}},'spec':{
        'restartPolicy':'Never','automountServiceAccountToken':False,
        'securityContext':{'runAsNonRoot':True,'runAsUser':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},
        'initContainers':[{'name':'prepare','image':IMAGE,'command':['sh','-ec',bootstrap],'env':env,
                          'securityContext':security,'volumeMounts':mounts,'resources':resources}],
        'containers':[{'name':'processor','image':IMAGE,'command':['python','-m','backend.data_process.event_jev'],
            'args':arguments,
            'env':env,'envFrom':[{'configMapRef':{'name':'transport-config'}},{'secretRef':{'name':'transport-secrets'}},
                               {'secretRef':{'name':'transport-jev-secrets'}}],
            'securityContext':security,'resources':resources,'volumeMounts':mounts+[{'name':'es-ca','mountPath':'/etc/es-ca','readOnly':True}]}],
        'volumes':[{'name':'work','emptyDir':{'sizeLimit':'4Gi'}},{'name':'tmp','emptyDir':{'sizeLimit':'512Mi'}},
                   {'name':'es-ca','secret':{'secretName':'transport-es-ca'}}]}}}}


def submit(cluster,directory):
    plan=json.loads((directory/'plan.json').read_text())
    jobs=json.loads(cluster.run('get','jobs','-n','default','-l','experiment='+plan['experiment_id'],'-o','json'))['items']
    unfinished=[job for job in jobs if not any(c['type'] in ('Complete','Failed') and c['status']=='True'
        for c in job.get('status',{}).get('conditions',[]))]
    if unfinished:
        raise RuntimeError('An unfinished Job already exists for this experiment; inspect it instead of duplicating inference')
    latest=directory/'cloud-job.json'
    if latest.exists():
        previous=json.loads(latest.read_text())
        if not re.fullmatch(r'transport-jev-event-[a-f0-9]{8}',previous['job']):
            raise ValueError('Unexpected saved Job name')
        atomic_json(directory/'job-history'/(previous['job']+'.json'),previous)
    archive,sha=package(directory)
    name='transport-jev-event-'+uuid4().hex[:8]
    budget=plan['maximum_inference_cost_usd']
    policy_path=directory/'execution-budget.json'
    if policy_path.exists():
        policy=json.loads(policy_path.read_text())
        if policy['experiment_id']!=plan['experiment_id'] or policy['input_sha256']!=fingerprint(directory/'input.ndjson'):
            raise ValueError('Operational budget does not match this frozen cohort')
        budget=policy['maximum_inference_cost_usd']
    spec=manifest(name,sha,plan['experiment_id'],budget)
    report={'job':name,'release_sha256':sha,'archive':str(archive.relative_to(ROOT)),
            'manifest':spec,'state':'created','experiment_id':plan['experiment_id'],'cost_cap_usd':budget}
    atomic_json(directory/'cloud-job.json',report)
    atomic_json(directory/'job-history'/(name+'.json'),report)
    cluster.run('create','-f','-',data=json.dumps(spec))
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        pods=json.loads(cluster.run('get','pods','-n','default','-l','job-name='+name,'-o','json'))['items']
        pod=next((p for p in pods if any('running' in c.get('state',{}) for c in p.get('status',{}).get('initContainerStatuses',[]))),None)
        if pod:
            break
        time.sleep(3)
    else:
        raise TimeoutError('Experiment Job init container did not start')
    pod_name=pod['metadata']['name']
    subprocess.run(cluster.command+['cp',str(archive.relative_to(ROOT)),f'default/{pod_name}:/work/release.tar.gz','-c','prepare'],cwd=ROOT,check=True,timeout=180)
    cluster.run('exec','-n','default',pod_name,'-c','prepare','--','touch','/work/release.ready')
    report.update(pod=pod_name,state='release_uploaded')
    atomic_json(directory/'cloud-job.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='manifest'}),flush=True)
    return report


def status(cluster, directory):
    """Small read-only snapshot; never emits credentials or individual post text."""
    plan=json.loads((directory/'plan.json').read_text())
    cohort=json.loads((directory/'cohort.json').read_text())
    app=cluster.secret('transport-secrets','default')
    with connection(cluster) as (_,url,ca), Elasticsearch(url,basic_auth=(app['ES_USER'],app['ES_PASSWORD']),ca_certs=str(ca),request_timeout=60) as es:
        query={'term':{'experiment_id':plan['experiment_id']}}
        counts={name:es.count(index='v2_'+name,query=query)['count'] for name in
            ('social_discussion_posts_raw','social_processing_decisions','social_posts_jev')}
        try:
            run=es.get(index='v2_experiment_runs',id=plan['experiment_id'])['_source']
        except NotFoundError:
            run={'status':'not_started'}
    jobs=json.loads(cluster.run('get','jobs','-n','default','-l','experiment='+plan['experiment_id'],'-o','json'))['items']
    result={'experiment_id':plan['experiment_id'],'expected_raw':cohort['raw_records'],
        'expected_processed':cohort['eligible_records'],'cloud_counts':counts,'run':run,
        'jobs':[{'name':j['metadata']['name'],'status':j.get('status',{})} for j in jobs]}
    atomic_json(directory/'latest-status.json',result)
    print(json.dumps(result),flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','ingest','submit','status'))
    p.add_argument('--kubeconfig',required=True)
    p.add_argument('--directory',type=Path,required=True)
    a=p.parse_args()
    cluster=Cluster(a.kubeconfig,CONTEXT)
    if a.action=='prepare':
        prepare(cluster,json.loads((a.directory/'plan.json').read_text()),a.directory)
    elif a.action=='ingest':
        ingest(cluster,a.directory)
    elif a.action=='submit':
        submit(cluster,a.directory)
    else:
        status(cluster,a.directory)
