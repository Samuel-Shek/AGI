"""Evidence-bound feedback destinations, never authority to edit a new target."""
import re
from .store import digest


def route(quotes, target, revision):
    names=set()
    for row in quotes:
        for name in ('claude','codex','hermes'):
            if re.search(r'(?<![a-z])'+name+r'(?![a-z])',row.get('quote',''),re.I):
                names.add(name)
    project=target.get('project_id','') if target.get('status')=='identified' else ''
    result={'schema_version':1,'source_revision':revision,'criticized_object':'unknown',
            'editable_object':'none','handler':'investigation','verification':'current_source_required'}
    if names:
        result.update(criticized_object=next(iter(names)) if len(names)==1 else 'multiple_assistants',
                      handler='skill_candidate' if names=={'hermes'} else 'behavior_observation',
                      verification='independent_natural_tasks')
    elif project:
        result.update(criticized_object=project,editable_object=project,
                      handler='project_prepare' if target.get('prepare_only') else 'project_repair',
                      verification='reproduction_installation_and_natural_samples')
    result['binding_sha256']=digest(result)
    return result


def current_route(store,incident,target,revision):
    from .sources import reference_is_current
    rows=store.rows('SELECT m.* FROM messages m JOIN quotes q ON q.message_id=m.id WHERE q.incident_id=?',(incident['id'],))
    current=[r for r in rows if reference_is_current(r)]
    # Missing/stale evidence cannot become a project modification grant.
    if not current or len(current)!=len(rows):
        return route([],{},revision)
    return route(current,target,revision)
