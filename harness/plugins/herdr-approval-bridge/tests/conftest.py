"""Run entirely offline with a scratch Hermes home before importing core."""
import json
import os
from pathlib import Path
import tempfile

HOME = Path(tempfile.mkdtemp(prefix='approval-bridge-test-'))
(HOME / 'herdr-pipeline.json').write_text(json.dumps({
    'profile': 'exampleapp', 'project': 'ExampleApp', 'cwd_prefixes': ['/srv/example-app'],
    'route': 'herdr-agent-events', 'chat_id': '111111111', 'owner_user_id': '111111111',
    'approval_tag': '[ExampleApp Herdr approval]'}))
os.environ['HERMES_HOME'] = str(HOME)
for key in list(os.environ):
    if key.startswith('HERMES_SESSION_') or key.endswith(('_TOKEN', '_SECRET', '_PASSWORD', '_API_KEY', '_CREDENTIALS')):
        os.environ.pop(key, None)
os.environ['TZ'] = 'UTC'
