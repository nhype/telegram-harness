"""Attach trusted same-task handoff targets to Herdr wake events.
No Herdr mutations, credentials or LLM calls. Stale/unknown events are dropped.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from herdr_event_bridge import PIPELINE_CONFIG, cwd_matches, load_pipeline_config, load_tasks  # noqa: E402

HOME: Path | None = None    # tests may pin; default: $HERMES_HOME
CONFIG: dict | None = None  # tests may pin; main() passes the profile's config explicitly
# Compact projection: identity, phase, policy and author/reviewer linking plus
# the controller-facing summary fields.  Large historical prose (dispositions,
# decision ledgers, handoff dicts, eventN_* checkpoints) is deliberately NOT
# forwarded; controllers read/update it through the ``herdr_task`` tool.
FIELDS = ('id', 'session', 'workspace_id', 'tab_id', 'pane_id', 'agent', 'agent_session',
          'previous_agent_session', 'fresh_session_verified',
          'cwd', 'project', 'change', 'branch', 'enabled', 'completed', 'phase',
          'phase_generation', 'execution_profile', 'policy', 'review_gate', 'review_verdict',
          'final_artifact', 'candidate_sha', 'deploy_revision',
          # Author/reviewer linking; also used by the controller guard admission check.
          'parent_task_id', 'parent_pane_id', 'reviewer_pane_id', 'reviewer_task_id',
          'expected_review_artifact',
          'wait', 'brief', 'brief_path', 'legacy_path', 'notes', 'decisions', 'updated_at')
BRIEF_MAX = 1500
TAIL = {'notes': 3, 'decisions': 5}
# Keys the bridge adds; defaulted so the prompt renderer never shows "{key}".
PAYLOAD_DEFAULTS = {
    'reason': 'status',
    'status_age_seconds': 0,
    'coalesced': 0,
    'hint': {'kind': 'none', 'label': ''},
    'wait': {'reason': 'none', 'until': ''},
}


def fingerprint(task):
    return hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def project_task(task):
    if not task:
        return None
    out = {}
    for key in FIELDS:
        if key not in task:
            continue
        value = task[key]
        if key == 'brief' and isinstance(value, str) and len(value) > BRIEF_MAX:
            value = value[:BRIEF_MAX] + '…'
        elif key in TAIL:
            if not isinstance(value, list):
                continue
            value = value[-TAIL[key]:]
        out[key] = value
    return out


def context(payload, registry, normalized=None, config=None):
    cfg = config if config is not None else CONFIG
    if not cfg:
        return None
    tasks = registry['tasks']
    supplied = payload.get('task', {})
    found = [t for t in tasks if t.get('id') == supplied.get('id')]
    if len(found) != 1:
        return None
    source = found[0]
    if (not source.get('enabled') or source.get('completed')
        or source.get('project') != cfg['project']
        or not cwd_matches(source.get('cwd', ''), cfg['cwd_prefixes'], cfg.get('sibling_prefix', False))
        or payload.get('task_fingerprint') != fingerprint(
            normalized.get(source['id'], {}) if normalized is not None else source)):
        return None
    parent_id = source.get('parent_task_id')
    parents = [t for t in tasks if t.get('id') == parent_id] if parent_id else [source]
    if len(parents) != 1:
        return None
    author = parents[0]
    def same(t):
        return all(t.get(k) == source.get(k) for k in ('project', 'cwd', 'change', 'session'))
    if (not same(author) or not author.get('enabled') or author.get('completed')
        or (parent_id and source.get('parent_pane_id') != author.get('pane_id'))):
        return None
    reviewers = [t for t in tasks if t.get('parent_task_id') == author.get('id')
                 and t.get('parent_pane_id') == author.get('pane_id')
                 and t.get('pane_id') == author.get('reviewer_pane_id') and same(t)]
    reviewer = reviewers[0] if len(reviewers) == 1 else None
    if parent_id and reviewer is not source:
        return None
    result = dict(payload)
    for key, default in PAYLOAD_DEFAULTS.items():
        if result.get(key) is None:
            result[key] = copy.deepcopy(default)
    task_ids = [author.get('id')] + ([reviewer.get('id')] if reviewer else [])
    # Ignore supplied workflow data: trust only the on-disk registry.
    result['workflow'] = {
        'version': 2, 'source_role': 'reviewer' if parent_id else 'author',
        'author': project_task(author), 'reviewer': project_task(reviewer),
        'allow_handoff': bool(author.get('policy', {}).get('allow_continue')
                              and source.get('policy', {}).get('allow_continue')),
        'requires_live_identity_check': True,
        # This is a delivery-time snapshot, not a lease to mutate a pane later.
        # Full records and all registry writes go through the plugin tool.
        'registry_access': {
            'tool': 'herdr_task',
            'actions': ['get', 'update', 'note', 'decision'],
            'task_ids': task_ids,
            'snapshot_only': True,
            'requires_revalidation_before_mutation': True,
        },
    }
    return result


def main():
    home = HOME or Path(os.environ['HERMES_HOME'])
    payload = json.load(sys.stdin)
    config = load_pipeline_config(home / PIPELINE_CONFIG)
    if config is None:
        return  # this profile has no pipeline: stay silent
    path = home / 'state/herdr_tasks.json'
    registry = json.loads(path.read_text())
    normalized, _ = load_tasks(path)
    result = context(payload, registry, normalized, config)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
