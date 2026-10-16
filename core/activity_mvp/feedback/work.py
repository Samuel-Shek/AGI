"""Durable, version-bound controller work; human answers remain separate."""
from __future__ import annotations
import json
import time
from .store import digest, packed

POLICY = "owner-approved-autonomy-20260921"
STATES = {"queued", "investigating", "waiting_evidence", "ready", "running", "verifying", "observing", "verified", "resolved", "human_required", "blocked"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, reference TEXT NOT NULL,
 source_revision TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'queued', summary TEXT NOT NULL DEFAULT '',
 evidence TEXT NOT NULL DEFAULT '[]', result TEXT NOT NULL DEFAULT '{}',
 routing TEXT NOT NULL DEFAULT '{}',
 processed_revision TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
 next_check REAL NOT NULL DEFAULT 0, updated REAL NOT NULL, policy TEXT NOT NULL,
 UNIQUE(kind,reference));
CREATE TABLE IF NOT EXISTS work_events(
 id INTEGER PRIMARY KEY, work_id TEXT NOT NULL, event TEXT NOT NULL,
 source_revision TEXT NOT NULL, details TEXT NOT NULL, created REAL NOT NULL);
"""

class WorkQueue:
    def __init__(self, store):
        self.store = store
        if not store.readonly:
            store.conn.executescript(SCHEMA)
            if "routing" not in {r[1] for r in store.conn.execute("PRAGMA table_info(work_items)")}:
                store.conn.execute("ALTER TABLE work_items ADD COLUMN routing TEXT NOT NULL DEFAULT '{}'")
    def enqueue(self, kind, reference, revision, project_id=""):
        key = digest([kind, reference])[:24]
        row = self.get(key)
        if row is None:
            self.store.conn.execute("INSERT INTO work_items(id,kind,reference,source_revision,project_id,updated,policy) VALUES(?,?,?,?,?,?,?)", (key,kind,reference,revision,project_id,time.time(),POLICY))
            self.event(key, "queued", {})
        elif row["source_revision"] != revision:
            state=row['state'] if row['state'] in {'running','verifying'} else 'queued'
            self.store.conn.execute("UPDATE work_items SET source_revision=?,state=?,next_check=0,updated=?,project_id=? WHERE id=?", (revision,state,time.time(),project_id,key))
            self.event(key, "source_changed", {"previous_revision":row["source_revision"]})
        return self.get(key)
    def get(self,key):
        rows=self.store.rows("SELECT * FROM work_items WHERE id=?", (key,))
        return self.decode(rows[0]) if rows else None
    @staticmethod
    def decode(row):
        row=dict(row)
        for field in ("evidence","result"): row[field]=json.loads(row[field])
        row["routing"]=json.loads(row.get("routing","{}"))
        return row
    def update(self,key,state,**fields):
        if state not in STATES: raise ValueError("invalid_work_state")
        allowed={"summary","evidence","result","processed_revision","attempts","next_check","project_id","routing"}
        if set(fields)-allowed: raise ValueError("unsupported_work_fields")
        values={"state":state, "updated":time.time(), **fields}
        for field in ("evidence","result","routing"):
            if field in values: values[field]=packed(values[field])
        self.store.conn.execute("UPDATE work_items SET "+",".join(k+"=?" for k in values)+" WHERE id=?",(*values.values(),key))
        self.event(key,state,{k:v for k,v in fields.items() if k in {"summary","processed_revision"}})
        return self.get(key)
    def event(self,key,event,details):
        row=self.store.conn.execute("SELECT source_revision FROM work_items WHERE id=?",(key,)).fetchone()
        self.store.conn.execute("INSERT INTO work_events(work_id,event,source_revision,details,created) VALUES(?,?,?,?,?)",(key,event,row[0],packed(details),time.time()))
    def items(self,kind=None):
        sql="SELECT * FROM work_items"; args=()
        if kind: sql+=" WHERE kind=?";args=(kind,)
        return [self.decode(r) for r in self.store.rows(sql+" ORDER BY updated DESC",args)]
    def snapshot(self):
        try: rows=self.items()
        except __import__("sqlite3").OperationalError: return {"enabled":False,"items":[],"counts":{}}
        from collections import Counter
        return {"enabled":self.store.get("autonomy",{}).get("policy")==POLICY,"policy":POLICY,"items":rows,"counts":dict(Counter(r["state"] for r in rows))}
