import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.ingestion.event_harvest import open_ledger, commit_page, normalize, bounds
from backend.ingestion.authenticated_bluesky import BlueskyReader, SourceError
from backend.data_process.event_jev import Budget


def plan():
    return json.loads(Path('experiments/iran-20260228.json').read_text())


def post(stamp='2026-02-28T23:00:00Z', uri='at://did:plc:example/app.bsky.feed.post/one'):
    return {'uri':uri,'record':{'text':'Public transport is affordable and works well for me.',
            'langs':['en'],'createdAt':stamp},'author':{'did':'did:plc:example'}}


def test_calendar_window_and_timezone_boundaries():
    p=plan()
    first,last=bounds(p)
    assert (last-first).days == 91
    assert normalize(post('2026-01-27T23:59:59Z'),'bluesky',p,'q') is None
    assert normalize(post('2026-04-29T00:00:00Z'),'bluesky',p,'q') is None
    assert normalize(post('2026-04-28T23:59:59Z'),'bluesky',p,'q')['experiment_phase']=='post_month_2'
    assert normalize(post('2026-02-28T00:30:00+02:00'),'bluesky',p,'q')['experiment_phase']=='pre'


def test_page_commits_documents_and_cursor_together_and_deduplicates(tmp_path):
    p=plan()
    db=open_ledger(tmp_path,p)
    task=db.execute("SELECT * FROM tasks WHERE platform='bluesky' ORDER BY id LIMIT 1").fetchone()
    commit_page(db,task,[post(),post(),post('2026-05-01T00:00:00Z')],'next',p)
    updated=db.execute('SELECT * FROM tasks WHERE id=?',(task[0],)).fetchone()
    assert updated[5:8]==('next',1,'pending')
    assert db.execute('SELECT COUNT(*) FROM docs').fetchone()[0]==1
    assert db.execute('SELECT returned,outside,accepted FROM pages').fetchone()==(3,1,2)
    db.close()
    db=open_ledger(tmp_path,p)
    updated=db.execute('SELECT * FROM tasks WHERE id=?',(task[0],)).fetchone()
    assert commit_page(db,updated,[post()],'next',p)=='repeated_cursor'
    assert db.execute('SELECT COUNT(*) FROM docs').fetchone()[0]==1
    db.close()


def test_resume_rejects_changed_collection_contract(tmp_path):
    p=plan()
    open_ledger(tmp_path,p).close()
    p['end']='2026-04-29'
    with pytest.raises(ValueError,match='different frozen plan'):
        open_ledger(tmp_path,p)


def test_authenticated_search_does_not_forward_credentials_to_public_hosts(monkeypatch):
    monkeypatch.setenv('BLUESKY_HANDLE','example.bsky.social')
    monkeypatch.setenv('BLUESKY_APP_PASSWORD','test-password')
    session=Mock()
    session.post.return_value=Mock(status_code=200,json=lambda:{'accessJwt':'test-token'})
    session.get.return_value=Mock(status_code=200,json=lambda:{'posts':[]})
    client=BlueskyReader(session=session,sleep=lambda _:None)
    assert client.search({'q':'fuel prices'})=={'posts':[]}
    assert session.get.call_args.args[0].startswith('https://bsky.social/')
    assert session.get.call_args.kwargs['allow_redirects'] is False
    session.get.return_value=Mock(status_code=403)
    with pytest.raises(SourceError,match='HTTP 403'):
        client.search({'q':'fuel prices'})


def test_budget_retains_uncertain_requests_and_releases_only_known_cost():
    budget=Budget(1.0)
    budget.reserve(.8)
    with pytest.raises(Exception,match='budget exhausted'):
        budget.reserve(.3)
    budget.settle(.8,.2)
    budget.reserve(.7)
    assert budget.accounted==pytest.approx(.9)
    with pytest.raises(Exception,match='conservative'):
        budget.settle(.7,.8)


def test_only_explicit_transient_provider_failures_are_retryable():
    import requests
    from backend.data_process.event_jev import transient_failure
    from backend.data_process.jev_sentiment import JevError
    for exc in (requests.ReadTimeout(),requests.ConnectionError(),JevError('Jev returned HTTP 520; no automatic retry'),
                JevError('Jev returned HTTP 529; no automatic retry')):
        assert transient_failure(exc)
    for exc in (requests.exceptions.SSLError(),JevError('Jev returned HTTP 401; no automatic retry'),
                JevError('Jev returned HTTP 429; no automatic retry'),JevError('Choice disagrees with its probability distribution'),
                JevError('Experiment inference budget exhausted'),RuntimeError('ES unavailable')):
        assert not transient_failure(exc)


@pytest.fixture
def single_event_input(tmp_path,monkeypatch):
    import hashlib
    monkeypatch.setenv('TYPESAFE_API_KEY','test-key')
    p=plan()
    doc=normalize(post(),'bluesky',p,'public transport')
    path=tmp_path/'input.ndjson'
    path.write_text(json.dumps(doc)+'\n',encoding='utf-8')
    return p,doc,path,{'input_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.integration
def test_transient_retries_preserve_each_cost_reserve_then_resume_without_rebilling(es_service,single_event_input,monkeypatch,tmp_path):
    import requests
    from backend.data_process import event_jev
    from backend.common.settings import index_name
    from test.test_jev_sentiment import fixture_response
    p,doc,path,cohort=single_event_input
    http=Mock(side_effect=[requests.ReadTimeout(),Mock(status_code=520),Mock(status_code=200,json=fixture_response)])
    monkeypatch.setattr('backend.data_process.jev_sentiment.requests.post',http)
    result=event_jev.run(p,path,cohort,tmp_path/'first',workers=1,es=es_service)
    assert result['status']=='complete' and result['new_api_calls']==3 and result['failed_this_run']==0
    assert result['transient_request_failures_this_run']==2 and http.call_count==3
    actual=fixture_response()['usage']['input_tokens']*event_jev.INPUT_USD_PER_M/1_000_000
    assert result['accounted_cost_usd']==pytest.approx(2*event_jev.reservation(doc['raw_text'])+actual)
    es_service.indices.refresh(index=[index_name('jev_inference_attempts'),index_name('social_posts_jev')])
    resumed=event_jev.run(p,path,cohort,tmp_path/'resumed',workers=1,es=es_service)
    assert resumed['already_processed']==1 and resumed['new_api_calls']==0 and http.call_count==3
    assert resumed['accounted_cost_usd']==pytest.approx(result['accounted_cost_usd'])


@pytest.mark.integration
def test_three_failed_calls_stop_and_restart_cannot_reset_the_ceiling(es_service,single_event_input,monkeypatch,tmp_path):
    import requests
    from backend.data_process import event_jev
    from backend.common.settings import index_name
    p,doc,path,cohort=single_event_input
    http=Mock(side_effect=requests.ReadTimeout())
    monkeypatch.setattr('backend.data_process.jev_sentiment.requests.post',http)
    first=event_jev.run(p,path,cohort,tmp_path/'first',workers=1,es=es_service)
    assert first['status']=='incomplete' and first['new_api_calls']==3 and first['failed_this_run']==1
    assert first['transient_request_failures_this_run']==3 and http.call_count==3
    assert first['accounted_cost_usd']==pytest.approx(3*event_jev.reservation(doc['raw_text']))
    es_service.indices.refresh(index=index_name('jev_inference_attempts'))
    resumed=event_jev.run(p,path,cohort,tmp_path/'resumed',workers=1,es=es_service)
    assert resumed['status']=='incomplete' and resumed['new_api_calls']==0 and http.call_count==3
    assert 'request limit reached' in resumed['errors'][0]['reason']
    assert resumed['accounted_cost_usd']==pytest.approx(first['accounted_cost_usd'])


@pytest.mark.integration
def test_retry_cannot_exceed_remaining_experiment_budget(es_service,single_event_input,monkeypatch,tmp_path):
    import requests
    from backend.data_process import event_jev
    p,doc,path,cohort=single_event_input
    http=Mock(side_effect=requests.ReadTimeout())
    monkeypatch.setattr('backend.data_process.jev_sentiment.requests.post',http)
    amount=event_jev.reservation(doc['raw_text'])
    result=event_jev.run(p,path,cohort,tmp_path/'run',workers=1,budget_usd=amount*1.5,es=es_service)
    assert result['status']=='incomplete' and result['new_api_calls']==1 and http.call_count==1
    assert 'budget exhausted' in result['errors'][0]['reason']
    assert result['accounted_cost_usd']==pytest.approx(amount)


def test_operational_budget_is_finite_and_passed_to_cloud_worker():
    from scripts.cloud_event_experiment import manifest
    spec=manifest('example','a'*64,'iran-20260228-v1',15.0)
    args=spec['spec']['template']['spec']['containers'][0]['args']
    assert args[args.index('--budget-usd')+1]=='15.0'
    for invalid in (float('inf'),float('nan'),-1,0,True):
        with pytest.raises(ValueError,match='budget'):
            manifest('example','a'*64,'iran-20260228-v1',invalid)


@pytest.mark.integration
def test_native_response_survives_processed_write_failure_without_rebilling(monkeypatch,tmp_path):
    import hashlib
    import os
    from uuid import uuid4
    from elasticsearch import Elasticsearch
    from database.migrate import migrate
    from backend.data_process import event_jev
    from test.test_jev_sentiment import fixture_response
    if not os.environ.get('TEST_ES_URL'):
        pytest.skip('TEST_ES_URL required')
    prefix='test_event_'+uuid4().hex[:10]+'_'
    monkeypatch.setenv('ES_INDEX_PREFIX',prefix)
    monkeypatch.setenv('TYPESAFE_API_KEY','test-key')
    es=Elasticsearch(os.environ['TEST_ES_URL'])
    migrate(es)
    p=plan()
    doc=normalize(post(),'bluesky',p,'public transport')
    input_path=tmp_path/'input.ndjson'
    input_path.write_text(json.dumps(doc)+'\n',encoding='utf-8')
    cohort={'input_sha256':hashlib.sha256(input_path.read_bytes()).hexdigest()}
    response=Mock(status_code=200,json=fixture_response)
    http=Mock(return_value=response)
    monkeypatch.setattr('backend.data_process.jev_sentiment.requests.post',http)
    original=event_jev.bulk_documents
    failed=Mock(side_effect=RuntimeError('simulated ES write interruption'))
    monkeypatch.setattr(event_jev,'bulk_documents',failed)
    try:
        first=event_jev.run(p,input_path,cohort,tmp_path/'first',workers=1,es=es)
        assert first['status']=='incomplete'
        assert http.call_count==1
        es.indices.refresh(index=prefix+'*')
        monkeypatch.setattr(event_jev,'bulk_documents',original)
        second=event_jev.run(p,input_path,cohort,tmp_path/'resumed',workers=1,es=es)
        assert second['status']=='complete' and second['cache_hits']==1
        assert second['new_api_calls']==0 and http.call_count==1
        assert second['accounted_cost_usd']==pytest.approx(first['accounted_cost_usd'])
        third=event_jev.run(p,input_path,cohort,tmp_path/'higher-ceiling',workers=1,budget_usd=15,es=es)
        assert third['new_api_calls']==0 and third['cost_cap_usd']==15
        assert third['accounted_cost_usd']==pytest.approx(second['accounted_cost_usd'])
        es.indices.refresh(index=prefix+'*')
        assert es.count(index=prefix+'social_posts_jev')['count']==1
        stored=es.get(index=prefix+'social_posts_jev',id=doc['doc_id'])['_source']
        assert stored['experiment_id']==p['experiment_id'] and stored['days_from_event']==0
        from backend.api.app import create_app
        from backend.api import app as api_module
        monkeypatch.setattr(api_module,'get_client',lambda:es)
        client=create_app().test_client()
        params={'from':'2026-01-28','to':'2026-04-28','timezone':'UTC',
                'model':'jev-1.13.0','experiment_id':p['experiment_id']}
        posts_response=client.get('/api/v1/social/posts',query_string=params)
        assert posts_response.status_code==200
        assert len(posts_response.json['data']['items'])==1
        assert posts_response.json['data']['items'][0]['target_sentiments']
        scores=client.get('/api/v1/social/target-sentiment',query_string={**params,'topic':'fuel_price'})
        assert scores.status_code==200
        assert all(row['topic']=='fuel_price' for row in scores.json['data'])
        status=client.get('/api/v1/experiments/status',query_string={'experiment_id':p['experiment_id']})
        assert status.status_code==200 and status.json['data']['status']=='complete'

        # A parser rejection still has a paid native HTTP 200 response. A
        # validated protocol repair must reuse it, not send the text again.
        from backend.data_process import jev_sentiment
        valid=jev_sentiment.validate_response
        p['experiment_id']='event-native-response-recovery'
        doc=normalize(post(uri='at://did:plc:example/app.bsky.feed.post/two'),'bluesky',p,'public transport')
        input_path.write_text(json.dumps(doc)+'\n',encoding='utf-8')
        cohort={'input_sha256':hashlib.sha256(input_path.read_bytes()).hexdigest()}
        before=http.call_count
        monkeypatch.setattr(jev_sentiment,'validate_response',Mock(side_effect=jev_sentiment.JevError('simulated parser rejection')))
        rejected=event_jev.run(p,input_path,cohort,tmp_path/'rejected',workers=1,es=es)
        assert rejected['status']=='incomplete' and http.call_count==before+1
        es.indices.refresh(index=prefix+'*')
        monkeypatch.setattr(jev_sentiment,'validate_response',valid)
        recovered=event_jev.run(p,input_path,cohort,tmp_path/'recovered',workers=1,es=es)
        assert recovered['status']=='complete' and recovered['cache_hits']==1
        assert recovered['new_api_calls']==0 and http.call_count==before+1
        assert recovered['accounted_cost_usd']<rejected['accounted_cost_usd']
    finally:
        names=list(es.indices.get(index=prefix+'*'))
        assert all(name.startswith(prefix) for name in names)
        if names:
            es.indices.delete(index=names)
        es.close()


def test_model_selection_keeps_baseline_and_jev_separate(monkeypatch):
    from backend.api.queries import social_index
    monkeypatch.delenv('SOCIAL_ACTIVE_MODEL',raising=False)
    assert social_index()=='social_posts_processed'
    assert social_index({'model':'jev-1.13.0'})=='social_posts_jev'
    monkeypatch.setenv('SOCIAL_ACTIVE_MODEL','jev-1.13.0')
    assert social_index()=='social_posts_jev'
    assert social_index({'model':'cardiffnlp/twitter-roberta-base-sentiment-latest'})=='social_posts_processed'


def test_api_default_model_filters_exact_revision_and_preserves_explicit_baseline(monkeypatch):
    from backend.api import app as api
    monkeypatch.setenv('SOCIAL_ACTIVE_MODEL', 'jev-1.13.0')
    es = Mock()
    es.search.return_value = {'hits': {'hits': []}}
    monkeypatch.setattr(api, 'get_client', lambda: es)
    client = api.create_app().test_client()
    for query, model, index in [('', 'jev-1.13.0', 'social_posts_jev'),
            ('&model=cardiffnlp/twitter-roberta-base-sentiment-latest', 'cardiffnlp/twitter-roberta-base-sentiment-latest', 'social_posts_processed')]:
        r = client.get('/api/v1/social/posts?from=2026-01-28&to=2026-04-28'+query)
        assert r.status_code == 200 and r.json['meta']['model'] == model
        call = es.search.call_args.kwargs
        assert call['index'].endswith(index)
        assert {'term': {'model_name': model}} in call['query']['bool']['filter']
    assert client.get('/api/v1/meta').json['data']['default_social_model'] == 'jev-1.13.0'
    annotations = client.get('/api/v1/social/annotations?from=2026-01-28&to=2026-04-28')
    assert annotations.status_code == 200 and annotations.json['meta']['model'] is None


def test_offline_scoring_audit_rejects_tampered_acceptance_and_wrong_polarity(monkeypatch, tmp_path):
    from copy import deepcopy
    from test.test_jev_sentiment import fixture_response, http, set_choice
    from backend.data_process.jev_sentiment import JevSentimentModel, processed_document
    from scripts.analyze_event_experiment import verify_scoring
    body = fixture_response()
    set_choice(body, 'fuel_price_state', 'factual_only')
    http(monkeypatch, body)
    result = JevSentimentModel(api_key='fixture-only', cache_directory=tmp_path).classify('Fixture text')
    row = processed_document({}, result)
    verify_scoring(row, .6)
    wrong = deepcopy(row)
    wrong['target_sentiments'][0]['score'] = .9
    with pytest.raises(ValueError, match='Published target'):
        verify_scoring(wrong, .6)
    wrong = deepcopy(row)
    wrong['jev']['targets']['fuel_price'].update(accepted=True, polarity=.65, label='positive', review_reasons=[])
    with pytest.raises(ValueError, match='Acceptance'):
        verify_scoring(wrong, .6)


def test_brent_uses_native_date_filtered_route_and_rejects_outside_rows(monkeypatch):
    from datetime import date
    from backend.brent_ingest import fetch_brent
    monkeypatch.setenv('EIA_API_KEY','test-key')
    get=Mock(return_value=Mock(json=lambda:{'response':{'total':'1','data':[{'period':'2026-02-02','value':'70'}]}}))
    monkeypatch.setattr(fetch_brent,'retry_get',get)
    assert len(fetch_brent.fetch_eia(date(2026,1,28),date(2026,4,28)))==1
    assert get.call_args.args[0].endswith('/petroleum/pri/spt/data/')
    assert get.call_args.kwargs['params']['frequency']=='daily'
    assert get.call_args.kwargs['params']['facets[series][]']=='RBRTE'
    get.return_value=Mock(json=lambda:{'response':{'total':'1','data':[{'period':'2026-09-29','value':'100'}]}})
    with pytest.raises(ValueError,match='outside'):
        fetch_brent.fetch_eia(date(2026,1,28),date(2026,4,28))


def test_analysis_keeps_missing_scores_missing_and_excludes_capped_dates():
    from scripts.analyze_event_experiment import aggregate
    p=plan()
    p['analysis']['minimum_target_scores_per_day']=1
    p['analysis']['exploratory_lags_days']=[0,1]
    def record(day,score):
        return {'platform':'bluesky','created_at':day+'T12:00:00Z',
            'contextual_sentiment_label':'neutral','jev':{'relevance':'related','content_type':'opinion'},
            'target_sentiments':[{'target':'fuel_price','accepted':True,'score':score,'label':'negative'}]}
    rows=[record('2026-02-28',-.7),record('2026-03-01',-.5),record('2026-03-09',-1)]
    prices=[{'date':'2026-02-27','daily_return_pct':1}, {'date':'2026-02-28','daily_return_pct':2},
            {'date':'2026-03-09','daily_return_pct':3}]
    result=aggregate(p,rows,rows,prices,[{'platform':'bluesky','day':'2026-03-09'}])
    empty=next(r for r in result['daily'] if r['date']=='2026-01-28')
    assert empty['accepted_targets']==0 and empty['mean_target_polarity'] is None
    assert [r['paired_observations'] for r in result['oil_sentiment_associations']]==[1,2]
    assert result['oil_sentiment_associations'][1]['last_date']=='2026-03-01'
    month=next(r for r in result['phase_summaries'] if r['phase']=='post_month_1' and r['platform']=='bluesky' and r['target']=='fuel_price')
    assert month['eligible_score_days']==2
    assert month['equal_day_mean_target_polarity']==pytest.approx(-.6)
    assert month['raw_posts_per_calendar_day']==pytest.approx(3/28)


def test_association_constant_or_tiny_samples_do_not_manufacture_correlation():
    from scripts.analyze_event_experiment import association
    for pairs in ([], [('2026-01-28',1,2)], [(str(i),1,float(i)) for i in range(30)]):
        result=association(pairs)
        assert result['pearson_r'] is None
        assert result['block_bootstrap_95pct_ci'] is None
