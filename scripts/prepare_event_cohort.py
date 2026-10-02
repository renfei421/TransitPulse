"""Freeze the observed cohort only after collection finishes; retain all exclusions.

Language assessment may run alongside collection. Final text deduplication uses
chronological order, independently of sentiment. Raw counts retain duplicates of
text that were posted as distinct records; the inference cohort does not.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from backend.common.time import utc_now
from backend.data_process.eligibility import LanguageDetector, assess
from backend.ingestion.import_ndjson import atomic_json, fingerprint


def prepare(directory, *, follow=False):
    directory=Path(directory)
    db=sqlite3.connect(directory/'collection.sqlite',timeout=60)
    db.execute('CREATE TABLE IF NOT EXISTS eligibility(id TEXT PRIMARY KEY, body TEXT)')
    db.commit()
    detector=LanguageDetector()
    started=time.monotonic()
    while True:
        rows=db.execute('SELECT d.id,d.body FROM docs d LEFT JOIN eligibility e ON d.id=e.id WHERE e.id IS NULL LIMIT 250').fetchall()
        evaluated=[]
        for identity,body in rows:
            doc=json.loads(body)
            result=assess(doc,detector)
            if not doc['candidate_topics']:
                result.update(decision='skip',reason='no_topic_keyword')
            evaluated.append((identity,json.dumps(result)))
        with db:
            db.executemany('INSERT OR IGNORE INTO eligibility VALUES(?,?)',evaluated)
        if rows:
            print(json.dumps({'language_assessed':db.execute('SELECT COUNT(*) FROM eligibility').fetchone()[0]}),flush=True)
            continue
        pending=db.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
        if pending and follow:
            if time.monotonic()-started > 7200:
                raise RuntimeError('Collection still incomplete after bounded two-hour preparation wait')
            time.sleep(5)
            continue
        if pending:
            raise RuntimeError('Collection has pending tasks; do not freeze an incomplete cohort')
        break
    seen, decisions, eligible_count, platforms = {},Counter(),0,Counter()
    manifest=hashlib.sha256()
    raw_path,decision_path,input_path=(directory/name for name in ('raw.ndjson','decisions.ndjson','input.ndjson'))
    with raw_path.open('w',encoding='utf-8',newline='\n') as raw_file, decision_path.open('w',encoding='utf-8',newline='\n') as decision_file, input_path.open('w',encoding='utf-8',newline='\n') as inputs:
        for body,assessment in db.execute('SELECT d.body,e.body FROM docs d JOIN eligibility e ON d.id=e.id ORDER BY d.created_at,d.id'):
            doc,result=json.loads(body),json.loads(assessment)
            raw_file.write(json.dumps(doc,ensure_ascii=False)+'\n')
            signature=result['text_fingerprint']
            if result['decision']=='process':
                if signature in seen:
                    result.update(decision='skip',reason='duplicate_model_text',duplicate_of=seen[signature])
                else:
                    seen[signature]=doc['doc_id']
            result.update(doc_id=doc['doc_id'],platform=doc['platform'],created_at=doc['created_at'],
                source_dataset=doc['source_dataset'],dataset_kind='live',schema_version=2,
                experiment_id=doc['experiment_id'],experiment_phase=doc['experiment_phase'],decided_at=utc_now())
            decisions[result['reason']]+=1
            decision_file.write(json.dumps(result,ensure_ascii=False)+'\n')
            if result['decision']=='process':
                entry={**doc,**result,'context_missing_parent':bool(doc.get('parent_post_id'))}
                line=json.dumps(entry,ensure_ascii=False)+'\n'
                inputs.write(line)
                manifest.update(line.encode())
                eligible_count+=1
                platforms[doc['platform']]+=1
    report={'prepared_at':utc_now(),'raw_records':sum(decisions.values()),'eligible_records':eligible_count,
            'eligibility_reasons':dict(decisions),'eligible_by_platform':dict(platforms),
            'input_sha256':fingerprint(input_path),'raw_sha256':fingerprint(raw_path),
            'decisions_sha256':fingerprint(decision_path),'deduplication':'Earliest post per normalized model-text fingerprint',
            'collection_statuses':[{'platform':p,'status':s,'tasks':n} for p,s,n in db.execute('SELECT platform,status,COUNT(*) FROM tasks GROUP BY platform,status')],
            'collection_gaps':[{'platform':p,'query':q,'day':d,'host':h,'pages':n,'status':s}
                for p,q,d,h,n,s in db.execute("SELECT platform,query,day,host,pages,status FROM tasks WHERE status IN ('capped','repeated_cursor') ORDER BY id")],
            'limits':['Provider language plus langid routing is not human language annotation',
                      'Text deduplication may remove later reposts; raw daily volume is reported separately',
                      'Parent context not inferred: direct-text Jev rubric may abstain on replies']}
    atomic_json(directory/'cohort.json',report)
    db.close()
    print(json.dumps(report),flush=True)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--follow',action='store_true')
    prepare(**vars(p.parse_args()))
