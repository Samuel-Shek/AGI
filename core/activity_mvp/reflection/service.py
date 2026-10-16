"""Runs inside the existing feedback controller; adds no resident service."""
import json
import subprocess
import time
from ..feedback.control import gate
from .store import Store
from . import sources,analysis

def root_for(feedback):return feedback.root.parent/'reflection'

def scan(feedback,now=None):
    root=root_for(feedback)
    if not (root/'reflection.sqlite').is_file():return {'status':'not_enabled'}
    with Store(root) as store:return sources.scan(store,feedback,now)

def ready(feedback):
    root=root_for(feedback)
    if not (root/'reflection.sqlite').is_file():return False
    with Store(root,readonly=True) as store:return analysis.ready(store)

def analyze(feedback):
    with Store(root_for(feedback)) as store:return analysis.analyze(store,feedback)

def notify(feedback):
    root=root_for(feedback)
    if not (root/'reflection.sqlite').is_file():return {'requested':0}
    with Store(root) as original_store:
        store=filtered(original_store,"reflection")
        if not store.enabled() or not feedback.active() or feedback.get('autonomy',{}).get('paused',False):return {'requested':0}
        epoch=store.get('authorization_epoch',0)
        sources.bind_read_check(store)
        notices=store.rows("SELECT n.id,n.insight_id,n.revision,i.evidence FROM notices n JOIN insights i ON i.id=n.insight_id WHERE n.delivered=0 AND i.state='active' AND i.revision=n.revision ORDER BY n.created LIMIT 5")
        eligible=[n for n in notices if all(sources.evidence_current(store,e,require_context=True) for e in json.loads(n['evidence']))]
        from ..presentation_scope import DisplayScope,read_view
        display_scope=DisplayScope(root.parent)
        visible=read_view(original_store.conn,display_scope,('insights','turns','retired_turns','contexts','retired_contexts','sources'))
        eligible=[n for n in eligible if visible.execute('SELECT 1 FROM insights WHERE id=?',(n['insight_id'],)).fetchone() and display_scope.unchanged()]
        if not eligible:return {'requested':0}
        # Raw conversations never appear on the lock screen. The app contains
        # the actual hypothesis, evidence and neutral distinguishing question.
        active=lambda:store.enabled() and feedback.active() and not feedback.get('autonomy',{}).get('paused',False) and store.get('authorization_epoch',0)==epoch and display_scope.unchanged() and not display_scope.invalid
        with gate(feedback.root,active):
            # Recheck after serialization: another notifier may have claimed
            # these same rows, or the conversation may now contain new progress.
            eligible=[n for n in eligible if visible.execute('SELECT 1 FROM insights WHERE id=?',(n['insight_id'],)).fetchone() and store.conn.execute("SELECT 1 FROM notices n JOIN insights i ON i.id=n.insight_id WHERE n.id=? AND n.delivered=0 AND i.state='active' AND i.revision=n.revision AND i.revision=?",(n['id'],n['revision'])).fetchone() and all(sources.evidence_current(store,e,require_context=True) for e in json.loads(n['evidence']))]
            if not eligible:return {'requested':0}
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                for notice in eligible:store.conn.execute('UPDATE notices SET delivered=3 WHERE id=? AND delivered=0',(notice['id'],))
                store.set('last_notification_attempt',time.time())
                store.conn.execute('COMMIT')
            except BaseException:store.conn.execute('ROLLBACK');raise
            try:
                process=subprocess.Popen(['/usr/bin/osascript','-e','display notification "有对话依据提示一处可能的思路停滞。打开 AGI 的思考与反思查看依据和其他解释。" with title "AGI 思考与反思"'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            except OSError:
                store.set('last_notification_error','通知提交结果不明，保留尝试，不自动重发。')
                return {'requested':0,'delivery_observed':False,'unknown':True}
        try:process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill();process.communicate()
            store.set('last_notification_error','通知提交结果不明，保留尝试，不自动重发。')
            return {'requested':0,'delivery_observed':False,'unknown':True}
        if process.returncode==0:
            for notice in eligible:store.conn.execute('UPDATE notices SET delivered=1 WHERE id=?',(notice['id'],))
            store.set('last_notification_request',time.time())
        else:store.set('last_notification_error','通知请求未确认，保留尝试，不自动重发。')
        return {'requested':len(eligible) if process.returncode==0 else 0,'delivery_observed':False}

import sys
from pathlib import Path
from ..conversation_scope import filtered
