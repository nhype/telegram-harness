"""Real normalization/transition/route/transport seam; no live webhook or agent writes."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import herdr_event_bridge as bridge
import herdr_workflow_context as route

TG_CONFIG = {'profile': 'exampleapp', 'project': 'ExampleApp', 'cwd_prefixes': ['/srv/example-app'],
             'sibling_prefix': False, 'route': 'herdr-agent-events'}
route.CONFIG = TG_CONFIG
# Integration with an installed Hermes host: profiles and its venv python come from the
# environment; on a machine without Hermes these parametrized cases simply do not exist.
PROFILES_DIR = Path(os.environ.get('HERMES_PROFILES_DIR', os.path.expanduser('~/.hermes/profiles')))
DEPLOYED = sorted(PROFILES_DIR.glob('*/herdr-pipeline.json'))
HERMES_PYTHON = os.environ.get('HERMES_PYTHON', '')


def hermes_runtime():
    """(argv prefix, code prelude) that runs Python inside the installed Hermes, or (None, '').

    Current Hermes names its runtime with `hermes --print-runtime-command`; older installs had a
    virtualenv. HERMES_PYTHON overrides both.
    """
    if HERMES_PYTHON:
        return [HERMES_PYTHON], ''
    hermes = shutil.which('hermes')
    if hermes:
        try:
            cmd = json.loads(subprocess.run([hermes, '--print-runtime-command'], capture_output=True,
                                            text=True, timeout=60).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            cmd = None
        agent = re.search(r"sys\.path\.insert\(0, '([^']+)'\)", ' '.join(cmd)) if cmd else None
        if agent:
            prelude = ('import os, sys\n'
                       f'sys.path.insert(0, {agent.group(1)!r})\n'
                       "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or "
                       "str(__import__('hermes_constants').get_default_hermes_root())\n"
                       'import hermes_bootstrap\n')
            return [cmd[0], '-I'], prelude
    venv = Path(os.path.expanduser('~/.hermes/hermes-agent/venv/bin/python'))
    return ([str(venv)], '') if venv.is_file() else (None, '')


def registry_fixture():
    author = dict(id='author', enabled=True, project='ExampleApp',
                  cwd='/srv/example-app', change='change', session='default',
                  workspace_id='w5', pane_id='w5:pX', agent_session='author-session',
                  reviewer_pane_id='w5:pY', phase='bounded-correction', phase_generation=7,
                  policy={'allow_continue': True, 'allow_deploy': True,
                          'allow_push': True, 'allow_sync_archive': False},
                  brief='Deploy approved; G1a <= USD 5 total; other gates CLOSED',
                  review_gate='Review needed before deploy',
                  controller_findings_current=['R2-L1'],
                  current_review_handoff={'id': 'r3', 'target_pane': 'w5:pY',
                                          'status': 'executing',
                                          'receipt': {'status': 'working'}},
                  last_controller_checkpoint={'event_id': 'author:5:done'},
                  last_controller_event='reviewer:5:done')
    reviewer = dict(author, id='reviewer', pane_id='w5:pY',
                    agent_session='reviewer-session', parent_task_id='author',
                    parent_pane_id='w5:pX',
                    policy={'allow_continue': True, 'allow_deploy': False},
                    expected_review_artifact='/home/dev/.hermes/profiles/exampleapp/cache/scratch/r3.md')
    return {'version': 1, 'tasks': [author, reviewer]}


def prepare(tmp_path):
    home = tmp_path / 'profile'
    (home / 'state').mkdir(parents=True)
    (home / 'herdr-pipeline.json').write_text(json.dumps(TG_CONFIG))
    path = home / 'state/herdr_tasks.json'
    registry = registry_fixture()
    path.write_text(json.dumps(registry))
    tasks, _ = bridge.load_tasks(path)
    state = {'version': 1, 'panes': {}, 'pending': []}
    bridge.transition(state, tasks['reviewer'], 'working')
    event = bridge.transition(state, tasks['reviewer'], 'done')
    return home, path, registry, tasks, state, event


def test_wire_event_supplies_authorization_and_dedup_receipts(tmp_path):
    _, _, registry, tasks, _, event = prepare(tmp_path)
    result = route.context(event, registry, tasks)
    workflow = result['workflow']
    author = workflow['author']
    # v2 projection: identity/phase/policy/linking + compact summary fields.
    for key in ('brief', 'review_gate', 'phase_generation', 'agent_session', 'pane_id', 'policy'):
        assert author.get(key) == registry['tasks'][0][key], key
    # Large historical prose is no longer forwarded (read via herdr_task get).
    for key in ('controller_findings_current', 'current_review_handoff',
                'last_controller_checkpoint', 'last_controller_event'):
        assert key not in author, key
    assert workflow['reviewer']['expected_review_artifact'].endswith('/r3.md')
    assert author['policy']['allow_deploy'] is True
    assert workflow['reviewer']['policy']['allow_deploy'] is False
    assert not author['policy']['allow_sync_archive']
    assert workflow['requires_live_identity_check'] is True
    assert workflow['registry_access']['requires_revalidation_before_mutation'] is True
    assert workflow['registry_access']['tool'] == 'herdr_task'
    assert workflow['registry_access']['actions'] == ['get', 'update', 'note', 'decision']
    assert workflow['registry_access']['task_ids'] == ['author', 'reviewer']
    assert workflow['registry_access']['snapshot_only'] is True


def test_unlisted_registry_fields_are_not_forwarded(tmp_path):
    _, _, registry, tasks, _, event = prepare(tmp_path)
    registry['tasks'][0]['credential'] = 'DO-NOT-FORWARD'
    registry['tasks'].append({'id': 'unrelated', 'credential': 'OTHER-SECRET'})
    assert 'DO-NOT-FORWARD' not in json.dumps(route.context(event, registry, tasks))
    assert 'OTHER-SECRET' not in json.dumps(route.context(event, registry, tasks))


def delivery_cli(tmp_path, home):
    """Local contract harness for the Hermes CLI; runs the REAL route main()."""
    script = tmp_path / 'hermes-fixture'
    script.write_text(
        '#!/usr/bin/env python3\n'
        'import contextlib, io, json, pathlib, sys\n'
        f'sys.path.insert(0, {str(SCRIPTS)!r})\n'
        'import herdr_workflow_context as route\n'
        f'route.HOME = pathlib.Path({str(home)!r})\n'
        "assert sys.argv[1:4] == ['webhook', 'test', 'fixture-only']\n"
        "sys.stdin = io.StringIO(sys.argv[sys.argv.index('--payload') + 1])\n"
        'out = io.StringIO()\n'
        'with contextlib.redirect_stdout(out): route.main()\n'
        'text = out.getvalue()\n'
        f'pathlib.Path({str(tmp_path / "routed.json")!r}).write_text(text)\n'
        "print('Response (200): ' + json.dumps({'status': 'accepted' if text else 'ignored'}))\n"
    )
    script.chmod(0o700)
    return str(script)


def test_load_transition_route_delivery_and_duplicate_suppression(tmp_path):
    home, _, _, tasks, state, event = prepare(tmp_path)
    cli = delivery_cli(tmp_path, home)
    assert bridge.enqueue(state, event)
    assert not bridge.enqueue(state, event)
    assert bridge.flush_pending(state, tmp_path / 'bridge-state.json', cli, 'fixture-only', 5,
                                tasks=tasks, live_pane_ids={'w5:pX', 'w5:pY'})
    routed = json.loads((tmp_path / 'routed.json').read_text())
    assert routed['workflow']['source_role'] == 'reviewer'
    assert routed['workflow']['reviewer']['expected_review_artifact'].endswith('/r3.md')
    assert routed['workflow']['author']['brief'].endswith('other gates CLOSED')
    assert routed['workflow']['registry_access']['tool'] == 'herdr_task'
    for key in ('reason', 'hint', 'wait', 'coalesced', 'status_age_seconds'):
        assert routed[key] == event[key], key
    assert not state['pending']
    assert bridge.transition(state, tasks['reviewer'], 'done') is None
    assert not bridge.load_state(tmp_path / 'bridge-state.json')['pending']


def test_ignored_route_is_final_instead_of_retrying_forever(tmp_path):
    """v2: an ``ignored`` route reply is a final drop (the old bridge retried
    forever and stalled socket reading); the event is never claimed delivered."""
    home, path, registry, tasks, state, event = prepare(tmp_path)
    # Route observes a disabled source after the bridge queued the event.
    registry['tasks'][1]['enabled'] = False
    path.write_text(json.dumps(registry))
    bridge.enqueue(state, event)
    assert bridge.flush_pending(state, tmp_path / 'state.json', delivery_cli(tmp_path, home),
                                'fixture-only', 5, tasks=tasks,
                                live_pane_ids={'w5:pX', 'w5:pY'})
    assert state['pending'] == []
    assert (tmp_path / 'routed.json').read_text() == ''
    # With the refreshed inventory a queued event for the disabled task is
    # dropped without running the transport at all.
    bridge.enqueue(state, event)
    fresh, _ = bridge.load_tasks(path)
    assert bridge.flush_pending(state, tmp_path / 'state.json', '/must-not-run',
                                'fixture-only', 5, tasks=fresh, live_pane_ids={'w5:pX', 'w5:pY'})
    assert state['pending'] == []


def test_delivery_requires_success_http_as_well_as_accepted_body(tmp_path):
    script = tmp_path / 'failed-hermes-fixture'
    script.write_text('#!/usr/bin/env python3\nprint(\'Response (500): {"status":"accepted"}\')\n')
    script.chmod(0o700)
    ok, detail = bridge.deliver_one(str(script), 'fixture-only', {}, 5)
    assert not ok, detail


@pytest.mark.parametrize('response,expected', [
    ('Response (200): {"status":"ignored"}', False),
    ('Response (200): {"status":"rejected"}', False),
    ('Response (202): {"status":"accepted"}', True),
    ('Response (200): {"status":"delivered"}', True),
    ('Response (200): {}', False),
    ('Response (200): {broken}', False),
    ('not a webhook acknowledgement', False),
])
def test_transport_response_contract(tmp_path, response, expected):
    script = tmp_path / 'response-fixture'
    script.write_text('#!/usr/bin/env python3\nprint(' + repr(response) + ')\n')
    script.chmod(0o700)
    assert bridge.deliver_one(str(script), 'fixture-only', {}, 5)[0] is expected


@pytest.mark.parametrize('change', ['disabled', 'completed', 'foreign', 'reassigned', 'phase'])
def test_real_pipeline_rejects_invalid_source(tmp_path, change):
    home, path, registry, _, _, event = prepare(tmp_path)
    source = registry['tasks'][1]
    if change == 'disabled':
        source['enabled'] = False
    elif change == 'completed':
        source['completed'] = True
    elif change == 'foreign':
        source['project'] = 'Other'
    elif change == 'reassigned':
        source['parent_pane_id'] = 'w5:wrong'
    else:
        source['phase'] = 'superseded'
    path.write_text(json.dumps(registry))
    assert not bridge.deliver_one(delivery_cli(tmp_path, home), 'fixture-only', event, 5)[0]
    assert (tmp_path / 'routed.json').read_text() == ''


@pytest.mark.parametrize('pipeline', DEPLOYED, ids=lambda p: p.parent.name)
def test_native_registry_recovery_and_webhook_toolset_resolution(tmp_path, pipeline):
    """Exercise installed Hermes, not a reimplementation of its tool resolver.

    Project-scoped lean-ctx denial is independently verified against the live
    MCP tool; this test checks the native recovery path with the same outside-cwd
    registry shape, and checks the deployed profile's explicit toolsets.
    """
    config = bridge.load_pipeline_config(pipeline)
    assert config is not None, f'invalid {pipeline}'
    runtime, prelude = hermes_runtime()
    if runtime is None or not Path(config['cwd_prefixes'][0]).is_dir():
        pytest.skip('needs the Hermes host: an installed Hermes and the profile repo')
    home, path, registry, _, _, _ = prepare(tmp_path)
    probe = r'''
import json, sys, yaml
from hermes_cli.tools_config import _get_platform_tools
from toolsets import resolve_toolset
from tools.file_tools import read_file_tool
old = _get_platform_tools({}, 'webhook')
assert not {'file', 'terminal'} & old, old
explicit = {'platform_toolsets': {'webhook': ['file', 'herdr', 'skills', 'terminal']}}
configured = _get_platform_tools(explicit, 'webhook')
assert {'file', 'terminal', 'herdr', 'skills'} <= configured, configured
names = set().union(*(set(resolve_toolset(t)) for t in configured))
assert {'read_file', 'write_file', 'terminal'} <= names, names
# Read-only deployed-config assertion: do not leak mcp environment/credentials.
with open(sys.argv[2]) as f:
    actual = yaml.safe_load(f)
actual_tools = _get_platform_tools(actual, 'webhook')
assert {'file', 'terminal', 'herdr', 'skills'} <= actual_tools, actual_tools
result = json.loads(read_file_tool(sys.argv[1], task_id='exampleapp-native-recovery-test'))
assert 'error' not in result, result
# Native read_file line-number format -> exact JSON fixture.
text = '\n'.join(line.split('|', 1)[1] for line in result['content'].splitlines())
parsed = json.loads(text)
assert parsed['tasks'][0]['brief'].endswith('other gates CLOSED')
assert parsed['tasks'][0]['policy']['allow_sync_archive'] is False
print(json.dumps({'native_registry_read': True, 'toolsets': sorted(actual_tools)}))
'''
    proc = subprocess.run([*runtime, '-c', prelude + probe, str(path), str(pipeline.parent / 'config.yaml')],
                          cwd=json.loads(pipeline.read_text())['cwd_prefixes'][0], text=True,
                          # other suites point HERMES_HOME at a scratch home; the probe needs the real one
                          env={k: v for k, v in os.environ.items() if k != 'HERMES_HOME'},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)  # a cold runtime start can be slow
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)['native_registry_read'] is True


