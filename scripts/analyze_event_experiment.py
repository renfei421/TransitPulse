"""Read back a completed experiment and produce aggregate, non-causal evidence.

No headline accuracy claims: model gates and provenance are verified separately
from semantic validity. Dates without accepted attitudes remain missing, not zero.
"""
import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import statistics

from elasticsearch import Elasticsearch, helpers
import numpy as np

from backend.common.time import timestamp, utc_now
from backend.data_process.jev_sentiment import (
    CONTRACT_SHA256, MODEL, INPUT_USD_PER_M, RUBRIC_VERSION, SCORING_VERSION,
    LABELS, TARGETS, choice_probability_gap,
)
from backend.ingestion.import_ndjson import atomic_json
from scripts.cloud_elasticsearch import Cluster, CONTEXT, connection


def days(start, end):
    current, last = date.fromisoformat(start), date.fromisoformat(end)
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


def corr(x, y):
    if len(x)<3 or np.std(x)<1e-12 or np.std(y)<1e-12:
        return None
    return float(np.corrcoef(x,y)[0,1])


def association(pairs, seed=20260228):
    """Five-observation moving-block bootstrap; an exploratory sensitivity CI."""
    x=np.array([p[1] for p in pairs],dtype=float)
    y=np.array([p[2] for p in pairs],dtype=float)
    result={'paired_observations':len(pairs),'pearson_r':corr(x,y),
            'first_date':pairs[0][0] if pairs else None,'last_date':pairs[-1][0] if pairs else None,
            'block_bootstrap_95pct_ci':None,'bootstrap_block_observations':5}
    if len(pairs)<20 or result['pearson_r'] is None:
        result['interpretation']='Insufficient observations/variation for an interval; descriptive only'
        return result
    rng=np.random.default_rng(seed)
    estimates=[]
    block=5
    for _ in range(1000):
        starts=rng.integers(0,len(pairs),size=math.ceil(len(pairs)/block))
        indices=np.concatenate([(np.arange(start,start+block)%len(pairs)) for start in starts])[:len(pairs)]
        value=corr(x[indices],y[indices])
        if value is not None:
            estimates.append(value)
    if estimates:
        result['block_bootstrap_95pct_ci']=[float(v) for v in np.quantile(estimates,[.025,.975])]
    result['interpretation']='Observed association, not a causal estimate; exploratory lags are not independent confirmatory tests'
    return result


def verify_scoring(row, threshold):
    """Independently reconstruct published acceptance and polarity from stored evidence."""
    j = row['jev']
    if (j['rubric_version'], j['scoring_version'], j['confidence_threshold']) != (
            RUBRIC_VERSION, SCORING_VERSION, threshold):
        raise ValueError('Unexpected scoring contract')
    if set(j['targets']) != set(TARGETS) or len(row['target_sentiments']) != len(TARGETS):
        raise ValueError('Missing or duplicate targets')
    published = {v['target']: v for v in row['target_sentiments']}
    if set(published) != set(TARGETS):
        raise ValueError('Unexpected published targets')
    relevance = j['decision_answers']['relevance']
    blocked = (relevance['choice'] != 'related' or relevance['confidence'] < threshold
               or choice_probability_gap(relevance) > 1e-5)
    if j['relevance'] != relevance['choice'] or j['content_type'] != j['decision_answers']['content_type']['choice']:
        raise ValueError('Decision labels differ from native answers')
    for name, value in [('overall', j['overall']), *j['targets'].items()]:
        probs = value['probabilities']
        if set(probs) != set(LABELS) or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probs.values()):
            raise ValueError('Invalid score probabilities')
        polarity = probs['positive'] - probs['negative']
        if not math.isclose(value['unfiltered_polarity'], polarity, abs_tol=1e-9):
            raise ValueError('Probability polarity mismatch')
        reasons = []
        if j['input_truncated']:
            reasons.append('input_truncated')
        if value['state'] != ('single' if name == 'overall' else 'expressed'):
            reasons.append(value['state'])
        if value['state_confidence'] < threshold:
            reasons.append('low_state_confidence')
        # The strict v2 validator predates this diagnostic and rejected all gaps.
        if value.get('state_choice_probability_gap', 0) > 1e-5:
            reasons.append('choice_probability_disagreement')
        if value['confidence'] < threshold:
            reasons.append('low_score_confidence')
        label = max(LABELS, key=probs.get)
        if not reasons and label != 'neutral' and polarity == 0:
            reasons.append('opposed_probability_mass')
        expected_label = label if not reasons else 'mixed' if value['state'] == 'mixed' else 'unclassified'
        if name != 'overall' and blocked:
            reasons.append('topic_not_clearly_related')
            expected_label = 'unclassified'
        if (value['review_reasons'] != reasons or value['accepted'] != (not reasons)
                or value['label'] != expected_label
                or value['polarity'] != (None if reasons else polarity)):
            raise ValueError('Acceptance or score does not follow stored evidence')
        if name == 'overall':
            if (row['contextual_sentiment_label'], row['contextual_sentiment_polarity']) != (expected_label, value['polarity']):
                raise ValueError('Published whole-post tone mismatch')
        else:
            target = published[name]
            for outer, inner in [('score','polarity'), ('accepted','accepted'), ('label','label'),
                                 ('confidence','confidence'), ('state','state'), ('review_reasons','review_reasons')]:
                if target[outer] != value[inner]:
                    raise ValueError('Published target differs from scoring evidence')


def accepted_review_candidates(rows, existing_ids):
    """Supplement content strata with deterministic accepted platform/target/label examples.

    Purposive diagnostic selection, not a random or prevalence-weighted accuracy set.
    """
    strata = {}
    for row in sorted(rows, key=lambda r: r['doc_id']):
        for target in row['target_sentiments']:
            if target['accepted']:
                strata.setdefault((row['platform'], target['target'], target['label']), row)
    chosen, seen = [], set(existing_ids)
    for key, row in strata.items():
        if row['doc_id'] not in seen:
            chosen.append((key, row))
            seen.add(row['doc_id'])
    return chosen


def aggregate(plan, raw_rows, processed_rows, prices, gaps):
    calendar=list(days(plan['start'],plan['end']))
    raw_counts=Counter((r['platform'],timestamp(r['created_at']).date().isoformat()) for r in raw_rows)
    processed_counts=Counter()
    target_values=defaultdict(list)
    label_counts=defaultdict(Counter)
    tone=Counter(); relevance=Counter(); content=Counter(); abstentions=Counter()
    for row in processed_rows:
        day=timestamp(row['created_at']).date().isoformat()
        platform=row['platform']
        processed_counts[platform,day]+=1
        tone[row['contextual_sentiment_label']]+=1
        relevance[row['jev']['relevance']]+=1
        content[row['jev']['content_type']]+=1
        for target in row['target_sentiments']:
            if target['accepted']:
                target_values[platform,day,target['target']].append(target['score'])
                label_counts[platform,day,target['target']][target['label']]+=1
            else:
                abstentions.update(target['review_reasons'])
    capped_days={gap['day'] for gap in gaps if gap['platform']=='bluesky' and gap.get('day')}
    daily=[]
    for platform in ('bluesky','mastodon'):
        for day in calendar:
            for target in ('fuel_price','public_transport','ev','oil_vehicle'):
                values=target_values[platform,day,target]
                daily.append({'platform':platform,'date':day,'target':target,
                    'raw_posts':raw_counts[platform,day],'processed_posts':processed_counts[platform,day],
                    'accepted_targets':len(values),'mean_target_polarity':statistics.mean(values) if values else None,
                    'labels':dict(label_counts[platform,day,target]),'bluesky_search_capped':platform=='bluesky' and day in capped_days})
    price_by_day={p['date']:p for p in prices}
    minimum=plan['analysis']['minimum_target_scores_per_day']
    fuel={row['date']:row for row in daily if row['platform']=='bluesky' and row['target']=='fuel_price'
          and row['accepted_targets']>=minimum and not row['bluesky_search_capped']}
    correlations=[]
    for lag in plan['analysis']['exploratory_lags_days']:
        pairs=[]
        for day,row in sorted(fuel.items()):
            oil_day=(date.fromisoformat(day)-timedelta(days=lag)).isoformat()
            oil=price_by_day.get(oil_day,{})
            if oil.get('daily_return_pct') is not None:
                pairs.append((day,oil['daily_return_pct'],row['mean_target_polarity']))
        correlations.append({'lag_calendar_days':lag,'definition':'Positive lag: oil date precedes sentiment date',
                             'primary':lag==0,**association(pairs)})
    phases=[]
    for name in ('pre_period','post_month_1','post_month_2'):
        start,end=plan['analysis'][name]
        calendar_n=len(list(days(start,end)))
        for platform in ('bluesky','mastodon'):
            for target in ('fuel_price','public_transport','ev','oil_vehicle'):
                rows=[r for r in daily if r['platform']==platform and r['target']==target and start<=r['date']<=end]
                valid=[r for r in rows if r['accepted_targets']>=minimum and not r['bluesky_search_capped']]
                phases.append({'phase':name,'platform':platform,'target':target,'calendar_days':calendar_n,
                    'raw_posts':sum(r['raw_posts'] for r in rows),'raw_posts_per_calendar_day':sum(r['raw_posts'] for r in rows)/calendar_n,
                    'processed_posts':sum(r['processed_posts'] for r in rows),'accepted_targets':sum(r['accepted_targets'] for r in rows),
                    'eligible_score_days':len(valid),'equal_day_mean_target_polarity':statistics.mean(r['mean_target_polarity'] for r in valid) if valid else None})
    return {'daily':daily,'phase_summaries':phases,'oil_sentiment_associations':correlations,
        'whole_post_labels':dict(tone),'relevance':dict(relevance),'content_types':dict(content),
        'target_abstention_reasons':dict(abstentions),'bluesky_capped_days':sorted(capped_days),
        'minimum_target_scores_per_day':minimum,'price_observations':len(prices),
        'missing_price_policy':'No interpolation or zero filling; align only available daily returns',
        'limits':['Search results are platform-selected, not an exhaustive population',
                  'Capped/repeated-cursor Bluesky days excluded from primary association and phase sentiment means',
                  'Mastodon is supplemental; platform volumes must not be pooled as a population estimate',
                  'Confidence threshold is provisional; no human-gold accuracy/F1 claim',
                  'Whole-post emotional tone is separate from the poster attitude toward each target',
                  'No causal interpretation: topic demand, platform exposure and author composition can change']}


def render_figures(result, prices, destination):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    destination.mkdir(parents=True,exist_ok=True)
    fig,axes=plt.subplots(3,1,figsize=(13,10),sharex=True,constrained_layout=True)
    color={'fuel_price':'#cc5a43','public_transport':'#3373ac','ev':'#288570','oil_vehicle':'#8b6daf'}
    base=[r for r in result['daily'] if r['platform']=='bluesky' and r['target']=='fuel_price']
    x=[date.fromisoformat(r['date']) for r in base]
    axes[0].plot(x,[r['raw_posts'] for r in base],color='#3373ac',label='Unique observed posts')
    axes[0].plot(x,[r['processed_posts'] for r in base],color='#288570',label='Eligible unique-text cohort')
    axes[0].set_ylabel('Bluesky posts / day'); axes[0].legend(loc='upper left')
    for target in ('fuel_price','public_transport','ev'):
        rows=[r for r in result['daily'] if r['platform']=='bluesky' and r['target']==target]
        y=[r['mean_target_polarity'] if r['accepted_targets']>=result['minimum_target_scores_per_day'] and not r['bluesky_search_capped'] else np.nan for r in rows]
        axes[1].plot(x,y,label=target,color=color[target],linewidth=1.4)
    axes[1].set_ylim(-1.05,1.05); axes[1].axhline(0,color='#999999',linewidth=.6)
    axes[1].set_ylabel('Mean accepted target polarity'); axes[1].legend(loc='upper left',ncol=3)
    axes[2].plot([date.fromisoformat(p['date']) for p in prices],[p['price'] for p in prices],color='#66553c')
    axes[2].set_ylabel('Brent spot price (USD/barrel)')
    for ax in axes:
        ax.axvline(date(2026,2,28),color='#333333',linestyle='--',linewidth=1)
        ax.grid(alpha=.15)
        for day in result['bluesky_capped_days']:
            start=date.fromisoformat(day)
            ax.axvspan(start,start+timedelta(days=1),color='#dd9955',alpha=.13)
    axes[0].set_title('Observed transport discussion around 28 February 2026')
    axes[2].set_xlabel('UTC date; shaded dates have capped search coverage. Association is not causation.')
    path=destination/'event-iran-20260228.png'
    fig.savefig(path,dpi=170)
    plt.close(fig)
    return path


def run(cluster, directory):
    directory=Path(directory)
    plan=json.loads((directory/'plan.json').read_text())
    cohort=json.loads((directory/'cohort.json').read_text())
    app=cluster.secret('transport-secrets','default')
    with connection(cluster) as (_,url,ca), Elasticsearch(url,basic_auth=(app['ES_USER'],app['ES_PASSWORD']),ca_certs=str(ca),request_timeout=60) as es:
        status=es.get(index='v2_experiment_runs',id=plan['experiment_id'])['_source']
        if status['status']!='complete':
            print(json.dumps({'status':status['status'],'report':status['report']}))
            return {'status':status['status']}
        query={'query':{'term':{'experiment_id':plan['experiment_id']}}}
        rows=[]
        fields=['doc_id','created_at','platform','experiment_id','experiment_phase','model_name','model_revision',
            'contextual_sentiment_label','contextual_sentiment_polarity','target_sentiments','jev','input_truncated']
        with (directory/'processed.ndjson').open('w',encoding='utf-8',newline='\n') as stream:
            for hit in helpers.scan(es,index='v2_social_posts_jev',query=query,_source=fields,size=250):
                row=hit['_source']; rows.append(row)
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        expected={r['doc_id']:hashlib.sha256(r['raw_text'].encode()).hexdigest() for r in
                  (json.loads(line) for line in (directory/'input.ndjson').open(encoding='utf-8'))}
        if len(rows)!=cohort['eligible_records'] or {r['doc_id'] for r in rows}!=set(expected):
            raise ValueError('Cloud processed set differs from frozen eligible cohort')
        for row in rows:
            assert row['model_name']==row['model_revision']==MODEL
            assert row['jev']['input_sha256']==expected[row['doc_id']]
            assert row['jev']['contract_sha256']==CONTRACT_SHA256
            assert plan['start']<=timestamp(row['created_at']).date().isoformat()<=plan['end']
            verify_scoring(row, plan['confidence_threshold'])
            for target in row['target_sentiments']:
                assert (target['score'] is not None)==target['accepted']
                if target['accepted']:
                    assert -1<=target['score']<=1 and target['state']=='expressed' and not target['review_reasons']
                    assert row['jev']['relevance']=='related'
        attempts=[hit['_source'] for hit in helpers.scan(es,index='v2_jev_inference_attempts',query=query,
            _source=['status','input_tokens','accounted_usd','reserved_usd','latency_seconds','error_type','error_reason'])]
        audit={}
        for row in sorted(rows,key=lambda r:r['doc_id']):
            key=(row['platform'],row['experiment_phase'],row['jev']['content_type'])
            if key not in audit:
                audit[key]=row
        chosen=list(audit.values())[:36]
        originals=es.mget(index='v2_social_discussion_posts_raw',ids=[r['doc_id'] for r in chosen],_source=['raw_text'])['docs']
        sample=[{'sample_id':f'E{i+1:02}','doc_id':r['doc_id'],'platform':r['platform'],'phase':r['experiment_phase'],
                 'text':original.get('_source',{}).get('raw_text',''),'model_content_type':r['jev']['content_type'],
                 'model_relevance':r['jev']['relevance'],'targets':r['target_sentiments']}
                 for i,(r,original) in enumerate(zip(chosen,originals))]
        extra = accepted_review_candidates(rows, {r['doc_id'] for r in chosen})
        extra_originals = es.mget(index='v2_social_discussion_posts_raw', ids=[r['doc_id'] for _,r in extra],
                                 _source=['raw_text'])['docs'] if extra else []
        extra_sample = [{'sample_id':f'A{i+1:02}', 'doc_id':r['doc_id'], 'platform':r['platform'],
                         'phase':r['experiment_phase'], 'selection_stratum':list(key),
                         'text':original.get('_source',{}).get('raw_text',''),
                         'model_content_type':r['jev']['content_type'], 'model_relevance':r['jev']['relevance'],
                         'targets':r['target_sentiments']}
                        for i,((key,r),original) in enumerate(zip(extra,extra_originals))]
    raw=[{'platform':r['platform'],'created_at':r['created_at']} for r in
         (json.loads(line) for line in (directory/'raw.ndjson').open(encoding='utf-8'))]
    prices=json.loads((directory/'oil-prices.json').read_text())
    gaps=cohort.get('collection_gaps',[])
    result=aggregate(plan,raw,rows,prices,gaps)
    latencies=[r['latency_seconds'] for r in attempts if r.get('latency_seconds') is not None]
    result.update(experiment_id=plan['experiment_id'],verified_at=utc_now(),window={'start':plan['start'],'end':plan['end'],'timezone':'UTC'},
        raw_records=len(raw),processed_records=len(rows),provenance_checks_passed=len(rows),
        model=MODEL,contract_sha256=CONTRACT_SHA256,cohort=cohort,collection_gaps=gaps,
        validation_versions=dict(Counter(r['jev']['validation_version'] for r in rows)),
        inference={'attempts':len(attempts),'statuses':dict(Counter(r['status'] for r in attempts)),
            'reconciled_native_responses':sum(r['status']=='succeeded' and bool(r.get('error_reason')) for r in attempts),
            'accounted_cost_usd':sum(r['accounted_usd'] for r in attempts),
            'estimated_usage_cost_usd':sum(r.get('input_tokens',0) for r in attempts)*INPUT_USD_PER_M/1e6,
            'input_tokens':sum(r.get('input_tokens',0) for r in attempts),
            'https_latency_p50_seconds':float(np.median(latencies)) if latencies else None,
            'https_latency_p95_seconds':float(np.quantile(latencies,.95)) if latencies else None,
            'cost_note':'Token-rate estimate and conservative unknown-request reservations; not a billing invoice'},
        assistant_review={'status':'pending','sample_size':len(sample)+len(extra_sample),
                          'content_strata_sample_size':len(sample),'accepted_supplement_size':len(extra_sample),
                          'not_human_gold':True})
    atomic_json(directory/'analysis.json',result)
    atomic_json(directory/'review-sample.json',sample)
    atomic_json(directory/'review-accepted-sample.json',extra_sample)
    figure=render_figures(result,prices,Path('docs/figures'))
    public={k:v for k,v in result.items() if k!='daily'}
    atomic_json(Path('docs/evidence/event-experiment-2026-10-01.json'),public)
    print(json.dumps({'status':'verified','raw':len(raw),'processed':len(rows),'analysis':str(directory/'analysis.json'),
                      'figure':str(figure),'inference':result['inference'],'primary_correlation':next(r for r in result['oil_sentiment_associations'] if r['primary'])}),flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kubeconfig',required=True)
    p.add_argument('--directory',type=Path,required=True)
    a=p.parse_args()
    run(Cluster(a.kubeconfig,CONTEXT),a.directory)
