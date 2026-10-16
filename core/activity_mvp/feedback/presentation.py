"""Read-only, content-free progress for the native home page."""
import time


def progress(store, now=None):
    now = time.time() if now is None else now
    counts = {r["state"]: r["n"] for r in store.rows("SELECT state,count(*) n FROM incidents GROUP BY state")}
    messages = {r["state"]: r["n"] for r in store.rows("SELECT state,count(*) n FROM messages GROUP BY state")}
    latest = store.conn.execute("SELECT max(created) FROM audit WHERE event='classification_completed'").fetchone()[0]
    installed = store.conn.execute("SELECT count(*) FROM incidents WHERE installed_at IS NOT NULL").fetchone()[0]
    verified = store.conn.execute("SELECT count(*) FROM incidents WHERE state='verified' AND verified_at IS NOT NULL").fetchone()[0]
    last_result = store.get('analysis_last_result', {})
    last_tick = store.get('last_tick')
    started = last_result.get('started', 0) if last_result.get('status') == 'running' else 0
    running = bool(started and 0 <= now-started <= 960)
    retry = store.get('analysis_retry_at', 0)
    if not store.active(): state = 'paused'
    elif running: state = 'running'
    elif retry > now: state = 'retrying'
    elif started or not last_tick or now-last_tick > 1800: state = 'stale'
    elif store.get('last_error', ''): state = 'error'
    else: state = 'current'
    return {'state': state, 'observed_at': now, 'incident_count': sum(counts.values()),
            'analyzed_message_count': messages.get('classified', 0)+messages.get('excluded', 0),
            'pending_message_count': messages.get('pending', 0),
            'decision_count': counts.get('decision', 0)+counts.get('human_required', 0)+(0 if store.get('autonomy',{}).get('policy') else counts.get('recurred', 0)),
            'installed_count': installed, 'verified_count': verified,
            'last_analysis_success_at': latest, 'last_tick': last_tick,
            'next_retry_at': retry, 'raw_access': False}
