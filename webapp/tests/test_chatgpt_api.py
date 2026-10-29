"""Cloud monitor selection and evidence reuse the existing unattended collector."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import create_app
from lablog import cli
from lablog.chatgpt_bridge import import_page
from lablog.config import load_config


def test_chatgpt_api_selection_and_evidence_are_shared_with_cli(tmp_path):
    config = tmp_path / 'config.yaml'
    cfg = {'data_dir': str(tmp_path / 'data'), 'conversations': {'enabled': True},
           'chatgpt': {'enabled': True, 'root': str(tmp_path / 'bridge'), 'session_ids': ['paper-chat']}}
    config.write_text(yaml.safe_dump(cfg), encoding='utf-8')
    loaded = load_config(config)
    stamp = datetime(2026, 10, 16, 1, tzinfo=timezone.utc).timestamp()
    import_page(loaded, {'thread': {'id': 'paper-chat', 'title': '论文分析', 'createdAt': stamp, 'updatedAt': stamp},
                        'turns': [{'id': 't1', 'startedAt': stamp, 'completedAt': stamp + 5,
                                   'items': [{'type': 'userMessage', 'id': 'u1', 'content': [{'type': 'text', 'text': '解释论文公式'}]},
                                             {'type': 'agentMessage', 'id': 'a1', 'text': '::chatgpt-content-reference{}'}]}]})
    with TestClient(create_app(tmp_path / 'web.db', workers=0, config_path=config)) as client:
        project = client.get('/api/v1/workspace/projects').json()['data'][0]
        assert project['name'] == 'ChatGPT' and project['counts']['chatgpt'] == 1
        listing = client.get('/api/v1/workspace/conversations?source=chatgpt').json()['data']
        row = listing['items'][0]
        assert row['selected'] and listing['counts']['chatgpt'] == 1
        assert 'path' not in row and 'cwd' not in row
        messages = client.get('/api/v1/workspace/conversations/chatgpt:paper-chat/messages?day=2026-10-16').json()['data']
        assert messages['total'] == 1 and messages['messages'][0]['role'] == 'user'
        assert 'chatgpt-content-reference' not in json.dumps(messages)
        assert client.patch('/api/v1/workspace/conversations/selection', json={'changes': [
            {'key': row['key'], 'selected': False, 'revision': row['revision']}]}).status_code == 200
        assert cli._collect(loaded, 'daily', '2026-10-16')[1]['facts'] == []
        row = client.get('/api/v1/workspace/conversations?source=chatgpt').json()['data']['items'][0]
        assert client.patch('/api/v1/workspace/conversations/selection', json={'changes': [
            {'key': row['key'], 'selected': True, 'revision': row['revision']}]}).status_code == 200
        fact = cli._collect(loaded, 'daily', '2026-10-16')[1]['facts'][0]
        assert fact['source'] == 'chatgpt' and fact['status'] == 'planned'
        (tmp_path / 'bridge/paper-chat.json').unlink()
        assert cli._collect(loaded, 'daily', '2026-10-16')[1]['blockers']
