"""Read-only service-owned coverage; unavailable is never interpreted as zero."""
import json,sqlite3,time
from pathlib import Path

import sys
from activity_mvp.source_governance import personal_sources as personal_scope

def ranking_mode(path):
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2) as c:
            row=c.execute("SELECT mode FROM scenario_releases WHERE caller='agi' AND task='analysis_priority'").fetchone()
        return row[0] if row and row[0] in {'off','shadow','assist','gate'} else 'unavailable'
    except (OSError,sqlite3.Error):return 'unavailable'

def status():
    path=Path.home()/'AI/LocalData/Jev-Judgment.nosync/coverage.sqlite'
    try:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2) as c:
            c.row_factory=sqlite3.Row
            c.execute("BEGIN")
            sources=[]
            for name in ('mem0','business','agi'):
                if name=='business' and personal_scope.company_stopped():
                    sources.append({'source':name,'owner_status':'owner_retired','status':'本人停用','excluded':True,'checked':None,'gaps':None})
                    continue
                row=c.execute('SELECT * FROM coverage_sources WHERE source=?',(name,)).fetchone()
                if not row:return {'available':False,'reason':'initial_scan_pending'}
                counts=dict(c.execute('SELECT state,count(*) FROM coverage_objects WHERE source=? AND present=1 GROUP BY state',(name,)))
                sources.append({'source':name,'inventory_complete':bool(row['complete']),
                    'known':sum(counts.values()),'states':counts,'observed_at':row['observed'],
                    'fresh':time.time()-row['observed']<180,'error':row['error']})
            details={name:{'available':False,'reason':'initial_scan_pending'} for name in ('governance','permits')}
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='coverage_details'").fetchone():
                for row in c.execute('SELECT name,value,observed FROM coverage_details'):
                    value=json.loads(row['value']);value['fresh']=time.time()-row['observed']<180;details[row['name']]=value
        return {'available':True,'sources':sources,'meaning':'inventory_not_factual_approval',
                'ranking_mode':ranking_mode(path.with_name('ledger.sqlite')),**details}
    except (OSError,sqlite3.Error,ValueError,TypeError):return {'available':False,'reason':'coverage_unavailable'}
