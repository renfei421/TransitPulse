from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
import requests

from scripts.serve_event_dashboard import ReconnectingRouter, create_app


def config():
    return {'experiment_id':'iran-20260228-v1','from':'2026-01-28','to':'2026-04-28',
            'model':'jev-1.13.0','raw_records':124491,'eligible_records':90576}


def test_dashboard_proxy_pins_cohort_and_never_forwards_arbitrary_routes_or_writes():
    get=Mock(return_value=Mock(status_code=200,json=lambda:{'data':{'items':[]}}))
    client=create_app('http://127.0.0.1:12345',config(),get).test_client()
    response=client.get('/api/v1/social/posts?platform=bluesky&model=wrong&experiment_id=other&from=2000-01-01&limit=999')
    assert response.status_code==200
    kwargs=get.call_args.kwargs
    assert kwargs['params']=={'from':'2026-01-28','to':'2026-04-28','dataset_kind':'live','timezone':'UTC',
        'model':'jev-1.13.0','experiment_id':'iran-20260228-v1','platform':'bluesky','limit':'8'}
    assert kwargs['allow_redirects'] is False
    calls=get.call_count
    assert client.post('/api/v1/social/posts').status_code==405
    assert client.get('/api/v1/_cat/indices').status_code==404
    assert client.get('/api/v1/social/posts?platform=unknown').status_code==400
    assert client.get('/api/v1/social/target-sentiment?topic=wrong').status_code==400
    assert get.call_count==calls


def test_upstream_is_loopback_only_and_errors_do_not_leak_provider_bodies():
    for url in ('https://example.com','http://127.0.0.1:12345/redirect','http://name:secret@127.0.0.1:12345'):
        with pytest.raises(ValueError):
            create_app(url,config())
    get=Mock(return_value=Mock(status_code=302,json=lambda:{'sensitive':'must not forward'}))
    client=create_app('http://127.0.0.1:12345',config(),get).test_client()
    response=client.get('/api/v1/experiments/status?experiment_id=other')
    assert response.status_code==502 and b'sensitive' not in response.data
    assert get.call_args.kwargs['params']=={'experiment_id':'iran-20260228-v1'}
    assert client.get('/').status_code==200
    assert client.get('/dashboard-config').json['eligible_records']==90576


def test_dead_tunnel_recovers_once_for_concurrent_refreshes_without_replaying_request():
    first, second = Mock(), Mock()
    first.get.side_effect = requests.ConnectionError('dead local tunnel')
    second.get.return_value = Mock(status_code=200)
    contexts = [Mock(), Mock()]
    for context, session, port in zip(contexts, (first, second), (12345, 12346)):
        context.__enter__ = Mock(return_value=(session, f'http://127.0.0.1:{port}'))
        context.__exit__ = Mock()
    factory = Mock(side_effect=contexts)
    now = [0]
    tunnel = ReconnectingRouter(None, factory, lambda: now[0])
    with pytest.raises(requests.ConnectionError):
        tunnel.get('http://127.0.0.1/api/v1/experiments/status')
    with pytest.raises(requests.ConnectionError):
        tunnel.get('http://127.0.0.1/api/v1/experiments/status')
    assert factory.call_count == 1 and first.get.call_count == 1
    now[0] = 31
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: tunnel.get('http://127.0.0.1/api/v1/experiments/status'), range(4)))
    assert all(result.status_code == 200 for result in results)
    assert factory.call_count == 2
    contexts[0].__exit__.assert_called_once()
    assert second.get.call_args.args[0] == 'http://127.0.0.1:12346/api/v1/experiments/status'
    tunnel.close()
    contexts[1].__exit__.assert_called_once()


def test_tunnel_startup_failure_backs_off_and_http_errors_do_not_reconnect():
    context = Mock()
    context.__enter__ = Mock(side_effect=TimeoutError('startup'))
    factory = Mock(return_value=context)
    now = [0]
    tunnel = ReconnectingRouter(None, factory, lambda: now[0])
    for _ in range(4):
        with pytest.raises(requests.ConnectionError):
            tunnel.get('http://127.0.0.1/api/v1/quality')
    assert factory.call_count == 1
    session = Mock()
    session.get.return_value = Mock(status_code=503)
    context.__enter__.side_effect = None
    context.__enter__.return_value = (session, 'http://127.0.0.1:12345')
    context.__exit__ = Mock()
    now[0] = 31
    assert tunnel.get('http://127.0.0.1/api/v1/quality').status_code == 503
    assert tunnel.get('http://127.0.0.1/api/v1/quality').status_code == 503
    assert factory.call_count == 2
    tunnel.close()


def test_late_failure_from_old_tunnel_does_not_invalidate_new_connection():
    entered, release = threading.Event(), threading.Event()
    first, second = Mock(), Mock()
    def delayed_failure(url, **kwargs):
        if url.endswith('/quality'):
            entered.set()
            assert release.wait(timeout=5)
        raise requests.ConnectionError('old generation')
    first.get.side_effect = delayed_failure
    second.get.return_value = Mock(status_code=200)
    contexts = [Mock(), Mock()]
    for context, session in zip(contexts, (first, second)):
        context.__enter__ = Mock(return_value=(session, 'http://127.0.0.1:12345'))
        context.__exit__ = Mock()
    factory = Mock(side_effect=contexts)
    now = [0]
    tunnel = ReconnectingRouter(None, factory, lambda: now[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(tunnel.get, 'http://127.0.0.1/api/v1/quality')
        assert entered.wait(timeout=5)
        with pytest.raises(requests.ConnectionError):
            tunnel.get('http://127.0.0.1/api/v1/experiments/status')
        now[0] = 31
        assert tunnel.get('http://127.0.0.1/api/v1/experiments/status').status_code == 200
        release.set()
        with pytest.raises(requests.ConnectionError):
            old.result(timeout=5)
    assert tunnel.get('http://127.0.0.1/api/v1/experiments/status').status_code == 200
    assert factory.call_count == 2
    tunnel.close()
