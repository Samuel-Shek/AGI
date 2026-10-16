"""Append-only dispatch evidence. Legacy attempts are never reset or replayed."""
import sqlite3,time

SCHEMA='''
CREATE TABLE IF NOT EXISTS jev_dispatch(event_id TEXT PRIMARY KEY,phase TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jev_dispatch_events(id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,phase TEXT NOT NULL,created REAL NOT NULL);
'''

def execute(path,event,request,allowed):
    if not allowed():return None
    # Each worker owns its connection; a durable claim excludes concurrent sends.
    with sqlite3.connect(path,timeout=2) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT phase FROM jev_dispatch WHERE event_id=?',(event,)).fetchone()
        if c.execute('SELECT 1 FROM jev_rank_attempts WHERE event_id=?',(event,)).fetchone():return None
        if row and row[0]!='not_dispatched':return None
        if not allowed():return None
        now=time.time()
        c.execute("INSERT INTO jev_dispatch VALUES (?,'dispatch_started',?,?) ON CONFLICT(event_id) DO UPDATE SET phase='dispatch_started',updated=excluded.updated",(event,now,now))
        c.execute("INSERT INTO jev_dispatch_events(event_id,phase,created) VALUES (?,'dispatch_started',?)",(event,now))
    result=None;phase='unknown'
    try:
        if not allowed():phase='not_dispatched'
        else:
            result=request()
            # Only transport/server evidence establishes a safely unsent call.
            if isinstance(result,dict) and result.get('dispatch_phase') in {'not_dispatched','completed'}:
                phase=result['dispatch_phase']
    finally:
        with sqlite3.connect(path,timeout=2) as c:
            c.execute('UPDATE jev_dispatch SET phase=?,updated=? WHERE event_id=?',(phase,time.time(),event))
            c.execute('INSERT INTO jev_dispatch_events(event_id,phase,created) VALUES (?,?,?)',(event,phase,time.time()))
    return result
