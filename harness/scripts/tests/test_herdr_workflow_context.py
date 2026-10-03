import copy
import importlib.util
import io
import json
from pathlib import Path
s = importlib.util.spec_from_file_location('workflow', Path(__file__).parents[1] / 'herdr_workflow_context.py')
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
TG = {'profile': 'exampleapp', 'project': 'ExampleApp', 'cwd_prefixes': ['/srv/example-app'],
      'sibling_prefix': False, 'route': 'herdr-agent-events'}
MP = {'profile': 'demo', 'project': 'DemoApi', 'cwd_prefixes': ['/srv/demo-api'],
      'sibling_prefix': True, 'route': 'herdr-agent-events'}
m.CONFIG = TG

def fixture():
    a = dict(id='author', enabled=True, project='ExampleApp', cwd='/srv/example-app',
             change='change', session='default', pane_id='p1', reviewer_pane_id='p2',
             policy={'allow_continue': True, 'allow_deploy': False})
    r = dict(a, id='reviewer', pane_id='p2', parent_task_id='author', parent_pane_id='p1')
    return {'tasks': [a, r]}

def event(t):
    return {'task':copy.deepcopy(t),'task_fingerprint':m.fingerprint(t)}

def test_actual_bridge_normalization(tmp_path):
    import sys
    import json
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from herdr_event_bridge import load_tasks, transition
    d=fixture(); path=tmp_path/'tasks.json'; path.write_text(json.dumps(dict(d,version=1)))
    tasks,_=load_tasks(path)
    e=transition({'panes':{}},tasks['author'],'done')
    assert m.context(e,d) is None  # raw and wire records intentionally differ
    assert m.context(e,d,tasks)['workflow']['author']['pane_id']=='p1'


def test_reviewer_routes_back_to_author():
    d=fixture(); v=m.context(event(d['tasks'][1]),d)['workflow']
    assert v['author']['pane_id']=='p1' and v['source_role']=='reviewer' and v['allow_handoff']

def test_author_can_reactivate_only_linked_completed_reviewer():
    d=fixture();d['tasks'][1].update(enabled=False,completed=True)
    v=m.context(event(d['tasks'][0]),d)['workflow']
    assert v['reviewer']['pane_id']=='p2' and v['reviewer']['completed']
    assert not v['author']['policy']['allow_deploy']

def test_stale_event_dropped():
    d=fixture();e=event(d['tasks'][0]);d['tasks'][0]['phase']='changed'
    assert m.context(e,d) is None

def test_disabled_source_dropped():
    d=fixture();d['tasks'][1]['enabled']=False
    assert m.context(event(d['tasks'][1]),d) is None

def test_other_project_not_linked():
    d=fixture();d['tasks'][1]['project']='other'
    assert m.context(event(d['tasks'][1]),d) is None
    assert m.context(event(d['tasks'][0]),d)['workflow']['reviewer'] is None

def test_reassigned_reviewer_not_linked():
    d=fixture();d['tasks'][1]['change']='different'
    assert m.context(event(d['tasks'][1]),d) is None

def test_policy_preserved():
    d=fixture();d['tasks'][0]['policy']['allow_continue']=False
    assert not m.context(event(d['tasks'][1]),d)['workflow']['allow_handoff']

def test_payload_cannot_inject_target():
    d=fixture();e=event(d['tasks'][0]);e['workflow']={'author':{'pane_id':'evil'}}
    assert m.context(e,d)['workflow']['author']['pane_id']=='p1'

def test_ambiguous_reviewer_not_linked():
    d=fixture();d['tasks'].append(dict(d['tasks'][1], id='duplicate'))
    assert m.context(event(d['tasks'][0]),d)['workflow']['reviewer'] is None

def test_bridge_v2_keys_pass_through_and_default_when_missing():
    d=fixture();e=event(d['tasks'][0])
    old=m.context(e,d)
    assert old['reason']=='status' and old['coalesced']==0 and old['status_age_seconds']==0
    assert old['hint']=={'kind':'none','label':''} and old['wait']=={'reason':'none','until':''}
    e.update(reason='stall', coalesced=3, status_age_seconds=901,
             hint={'kind':'menu_open','label':'Esc to cancel'},
             wait={'reason':'external','until':'2026-09-24T10:00:00+00:00'})
    new=m.context(e,d)
    for key in ('reason','coalesced','status_age_seconds','hint','wait'):
        assert new[key]==e[key], key

def test_projection_is_compact_and_tolerates_missing_fields():
    d=fixture();a=d['tasks'][0]
    a.update(brief='b'*5000, notes=[{'text':str(i)} for i in range(10)],
             decisions=[{'text':str(i)} for i in range(10)], wait={'reason':'user_decision','until':'','note':'n'},
             legacy_path='state/herdr_tasks_legacy/author.json', controller_disposition='x'*2000,
             decision_ledger=[{'x':1}], latest_user_decision='y', event5_disposition='z',
             current_review_handoff={'id':'r'})
    author=m.context(event(a),d)['workflow']['author']
    assert len(author['brief'])==m.BRIEF_MAX+1
    assert [n['text'] for n in author['notes']]==['7','8','9']
    assert [n['text'] for n in author['decisions']]==['5','6','7','8','9']
    assert author['wait']['reason']=='user_decision' and author['legacy_path'].endswith('author.json')
    for key in ('controller_disposition','decision_ledger','latest_user_decision',
                'event5_disposition','current_review_handoff'):
        assert key not in author, key
    reviewer=m.context(event(a),d)['workflow']['reviewer']
    assert 'notes' not in reviewer and 'brief' not in reviewer
    assert m.context(event(a),d)['workflow']['registry_access']['tool']=='herdr_task'

def mp_fixture(cwd='/srv/demo-api-worktrees/pricing-cost-audit'):
    d = fixture()
    for t in d['tasks']:
        t.update(project='DemoApi', cwd=cwd)
    return d

def test_demoapi_worktree_routes_with_its_config():
    d = mp_fixture()
    assert m.context(event(d['tasks'][0]), d, config=MP)['workflow']['author']['pane_id'] == 'p1'

def test_demoapi_foreign_cwd_rejected():
    d = mp_fixture(cwd='/srv/example-app')
    assert m.context(event(d['tasks'][0]), d, config=MP) is None

def test_other_profile_project_rejected():
    d = fixture()
    assert m.context(event(d['tasks'][0]), d, config=MP) is None

def test_unconfigured_route_is_silent(monkeypatch):
    monkeypatch.setattr(m, 'CONFIG', None)
    d = fixture()
    assert m.context(event(d['tasks'][0]), d) is None

def test_main_without_pipeline_config_prints_nothing(tmp_path, monkeypatch, capsys):
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state' / 'herdr_tasks.json').write_text(json.dumps({'version': 1, 'tasks': []}))
    monkeypatch.setattr(m, 'HOME', tmp_path)
    monkeypatch.setattr('sys.stdin', io.StringIO('{"task": {"id": "x"}}'))
    m.main()
    assert capsys.readouterr().out == ''
