"""Content-free health evidence; successful polling is not successful work."""
from pathlib import Path
import sqlite3
import time

FAILURES = {'blocked', 'unknown', 'unavailable', 'error', 'partial', 'failed'}
NORMAL_WAITS = {'not_active', 'model_busy', 'daily_budget_exhausted', 'analysis_backoff'}
REASONS = NORMAL_WAITS | {
    'prior_input_unverifiable', 'prior_input_unresolved', 'personal_source_unavailable',
    'local_check_timeout', 'no_complete_context', 'source_changed',
    'source_read_timeout', 'source_read_failed', 'unsafe_source_path', 'unbounded_source',
    'private_operator_registry_required', 'governed_source_receipt_mismatch',
    'autonomy_policy_not_enabled', 'automatic_actor_mismatch', 'bindings_required',
    'memory_source_changed', 'account_rate_limited', 'model_result_unavailable',
    'analysis_inputs_changed', 'reflection_input_unverifiable', 'reflection_input_duplicate',
    'authorization_changed', 'scope_unavailable', 'work_item_not_visible',
    'OSError', 'ValueError', 'PermissionError', 'RuntimeError', 'TimeoutError',
    'IntegrityError', 'OperationalError', 'stage_failed', 'health_metadata_unavailable',
}


def incomplete(result):
    if not isinstance(result, dict): return False
    return (result.get('status') in FAILURES or
            result.get('reason') == 'personal_source_unavailable' or
            (result.get('status') == 'deferred' and result.get('reason') not in NORMAL_WAITS))


def needs_attention(result):
    # Ordinary exception deferrals already have a durable backoff notice.
    # Local read deadlines are caught outside that path and need this episode.
    return incomplete(result) and (result.get('status') != 'deferred' or result.get('reason') == 'local_check_timeout')


def summary(result):
    if not isinstance(result, dict): return {}
    value = {}
    state = result.get('status')
    if state == 'owner_retired':
        # An owner's stop decision is not an execution failure. Its fixed
        # state is sufficient; do not relabel or expose a free-text reason.
        return {'status': 'owner_retired'}
    if state == 'waiting_evidence' and result.get('reason') == 'current_work_required':
        # Preserve this fixed wait without changing failure or recovery policy.
        # Other reasons still pass through the existing content-free filter.
        return {'status': 'waiting_evidence', 'reason': 'current_work_required'}
    if state in FAILURES | {'ok', 'idle', 'paused', 'deferred', 'running', 'processed'}:
        value['status'] = state
    if 'reason' in result:
        value['reason'] = result['reason'] if result['reason'] in REASONS else 'stage_failed'
    if type(result.get('processed')) is int and result['processed'] >= 0:
        value['processed'] = result['processed']
    return value


def source_health(store, results):
    # Only the fixed source-scan envelope and visible registered source states.
    # A rotated scan that did not visit the failed source is not recovery.
    unavailable = {r['id'] for r in store.rows("SELECT id FROM sources WHERE status='unavailable'")}
    unavailable.update(r['source'] for r in results if isinstance(r,dict) and r.get('error') and 'source' in r)
    return {'status':'partial' if unavailable else 'ok','unavailable':len(unavailable)}


def unresolved_analysis(store):
    """Retain the last failure of each family; idle or another family cannot clear it."""
    value = store.get('analysis_health', None)
    if value is None:
        previous = store.get('analysis_last_result', {})
        family = store.get('analysis_last_family', 'controller')
        if previous.get('reason') == 'local_check_timeout':
            phase=previous.get('phase')
            if phase in {'feedback_prepare','reflection_prepare'}: family=phase.split('_')[0]
            elif phase not in {'submission_check','model','review'}: family='controller'
        failures={family: summary(previous)} if family in {'feedback', 'reflection', 'controller'} and incomplete(previous) else {}
        # The predecessor kept only one family result. Its other family's
        # unresolved-input flag remains authoritative even after a success.
        root=store.root.parent/'reflection'
        if (root/'reflection.sqlite').is_file():
            from ..reflection.store import Store as ReflectionStore
            try:
                with ReflectionStore(root,readonly=True) as reader:
                    if reader.enabled() and reader.get('replay_gap',''):
                        failures.setdefault('reflection',{'status':'blocked','reason':'prior_input_unresolved'})
            except (OSError,ValueError,sqlite3.Error):
                failures.setdefault('reflection',{'status':'unavailable','reason':'health_metadata_unavailable'})
        return failures
    failures = {k: {**summary(v), **{f:v[f] for f in ('failures','episode') if f in v}}
                for k, v in value.items()
                if k in {'feedback', 'reflection', 'controller'} and incomplete(v)}
    return failures


def record_analysis(store, family, result):
    failures = unresolved_analysis(store)
    family = family if family in {'feedback', 'reflection'} else 'controller'
    if incomplete(result):
        current = summary(result)
        previous = failures.get(family, {})
        current.update(failures=previous.get('failures',0)+1,
                       episode=previous.get('episode',time.time()))
        failures[family] = current
        # Exceptions already use the existing backoff notice. Returned blocked
        # results need the same durable outbox, independent of the other family.
        if current['failures'] >= 3 and needs_attention(current):
            store.notify('analysis-health-'+family+'-'+str(current['episode']),'analysis_blocked')
    elif type(result.get('processed')) is int and result['processed'] > 0:
        failures.pop(family, None)
    # Reaching a family proves readiness completed, but says nothing about the
    # other family's unresolved inputs or external execution receipts.
    if family != 'controller': failures.pop('controller', None)
    store.set('analysis_health', failures)
    return failures


def exception_evidence(error):
    """No exception text, locals, source lines, user paths, or source bodies."""
    root = Path(__file__).parent.parent
    frames = []
    tb = error.__traceback__
    while tb is not None:
        path = Path(tb.tb_frame.f_code.co_filename)
        if path.is_absolute() and path.is_relative_to(root):
            frames.append({'module': str(path.relative_to(root)), 'line': tb.tb_lineno})
        tb = tb.tb_next
    reason = str(error)
    return {'type': type(error).__name__,
            'reason': reason if reason in REASONS else 'stage_failed', 'frames': frames[-8:]}
