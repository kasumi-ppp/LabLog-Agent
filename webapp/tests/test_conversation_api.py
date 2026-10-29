"""Project/session contract exercised with a real, isolated native source."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import create_app


@pytest.fixture
def session_app(tmp_path):
    root = tmp_path / 'workbuddy'
    root.mkdir()
    with sqlite3.connect(root / 'workbuddy.db') as conn:
        conn.execute('CREATE TABLE sessions(id TEXT,cwd TEXT,title TEXT,custom_title TEXT,status TEXT,created_at INTEGER,updated_at INTEGER,last_activity_at INTEGER,deleted_at INTEGER,is_playground INTEGER)')
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)',
                     ('s1', 'E:/project/钉钉', '原标题', '持久化联调', 'completed', 1792026000000, 1792026000000, 1792026000000, None, 0))
    log = root / 'projects' / 'dingtalk' / 's1.jsonl'
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({'id': 'm1', 'sessionId': 's1', 'type': 'message',
                              'timestamp': 1792026000000, 'role': 'assistant', 'status': 'completed',
                              'content': [{'type': 'output_text', 'text': '已完成界面联调，等待运行时验证。'}]}, ensure_ascii=False) + '\n', encoding='utf-8')
    import yaml
    cfg = {'data_dir': 'data', 'conversations': {'enabled': True},
           'workbuddy': {'enabled': True, 'root': str(root)},
           'codex': {'project_paths': ['E:/project/VR']}}
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
    app = create_app(tmp_path / 'web.db', workers=0, config_path=config)
    with TestClient(app) as client:
        yield client, app, tmp_path, config, log


def test_projects_native_titles_selection_and_original_messages(session_app):
    client, app, root, config, log = session_app
    response = client.get('/api/v1/workspace/projects')
    assert response.status_code == 200
    projects = response.json()['data']
    project = next(p for p in projects if p['name'] == '钉钉')
    assert project['counts']['workbuddy'] == 1
    listing = client.get('/api/v1/workspace/conversations', params={'project_id': project['id']}).json()['data']
    item = listing['items'][0]
    assert item['title'] == '持久化联调'
    assert item['selected'] is False
    assert 'path' not in item and 'cwd' not in item
    changed = client.patch('/api/v1/workspace/conversations/selection', json={'changes': [
        {'key': item['key'], 'selected': True, 'revision': item['revision']}]})
    assert changed.status_code == 200
    stale = client.patch('/api/v1/workspace/conversations/selection', json={'changes': [
        {'key': item['key'], 'selected': False, 'revision': item['revision']}]})
    assert stale.status_code == 409
    evidence = client.get(f"/api/v1/workspace/conversations/{item['key']}/messages",
                          params={'day': '2026-10-15', 'kind': 'daily'})
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()['data']['messages'][0]['text'] == '已完成界面联调，等待运行时验证。'
    with TestClient(create_app(root / 'web.db', workers=0, config_path=config)) as restarted:
        listing = restarted.get('/api/v1/workspace/conversations').json()['data']
        assert listing['items'][0]['selected'] is True
    log.unlink()
    assert client.get(f"/api/v1/workspace/conversations/{item['key']}/messages",
                      params={'day': '2026-10-15'}).status_code == 410


def test_browser_choice_is_used_by_cli_auto_and_verified_fake_submission(session_app, monkeypatch):
    client, app, root, config, log = session_app
    import yaml
    from lablog import cli
    cfg = yaml.safe_load(config.read_text(encoding='utf-8'))
    cfg.update(dws={'profile': 'test'}, identity={'corp_id': 'corp', 'user_id': 'self', 'user_name': 'Self'},
               recipients=[{'name': 'Recipient', 'user_id': 'r', 'corp_name': 'lab'}],
               templates={'daily': {'id': 't', 'name': '学生日报'}},
               date_encoding={'verified': True, 'format': 'iso-date'}, history_from='2026-10-01',
               calendar={'file': 'calendar.json'})
    config.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding='utf-8')
    (root / 'calendar.json').write_text(json.dumps({'year': 2026, 'source': 'fixture', 'holidays': [], 'makeup_workdays': []}), encoding='utf-8')
    template = {'report_template_id': 't', 'report_template_name': '学生日报', 'allowAddReceivers': 1,
                'report_template_fields': [{'field_name': '今日完成', 'field_sort': 0, 'field_type': 1},
                                           {'field_name': '工作日期', 'field_sort': 1, 'field_type': 5}]}
    class FakeDws:
        actual = []
        def auth(self): return {'corp_id': 'corp', 'user_id': 'self', 'user_name': 'Self'}
        def template(self, *args): return template
        def contact(self, ident): return {'orgUserName': 'Recipient', 'orgName': 'lab'}
        def outbox(self, *args): return []
        def submit(self, ident, contents, users, dry_run=False):
            if dry_run: return {'status': 'dry_run'}
            self.actual.append(contents)
            return 'workbuddy-report'
        def detail(self, ident): return {'report_template_id': 't', 'reportId': ident, 'contents': self.actual[-1]}
    fake = FakeDws()
    monkeypatch.setattr(cli, '_client', lambda config: fake)
    monkeypatch.setattr(cli, 'send_notification', lambda *a, **k: pytest.fail('must not send notifications'))
    client.get('/api/v1/workspace/projects')
    item = client.get('/api/v1/workspace/conversations').json()['data']['items'][0]
    assert item['selected'] is False
    changed = client.patch('/api/v1/workspace/conversations/selection', json={'changes': [
        {'key': item['key'], 'selected': True, 'revision': item['revision']}]})
    assert changed.status_code == 200
    loaded = cli.load_config(config)
    _, evidence = cli._collect(loaded, 'daily', '2026-10-15')
    assert evidence['facts'][0]['source'] == 'workbuddy'
    assert evidence['facts'][0]['session_id'] == 's1'
    # A manually saved report must not bypass a now-unavailable selected source.
    from lablog.workflow import atomic_json, freeze_draft
    draft_path, draft = cli._generate(loaded, 'daily', '2026-10-15', 'extractive')
    draft['manual_saved'] = True
    atomic_json(draft_path, freeze_draft(draft))
    source_root = loaded['workbuddy']['root']
    loaded['workbuddy']['root'] = str(root / 'missing-native-source')
    monkeypatch.setattr(cli, 'send_notification', lambda *a, **k: {'status': 'disabled'})
    blocked = cli._auto(loaded, 'daily', '2026-10-15', 'extractive', submit=True)
    assert blocked['status'] == 'blocked'
    assert fake.actual == []
    loaded['workbuddy']['root'] = source_root
    outcome = cli._auto(loaded, 'daily', '2026-10-15', 'extractive', submit=True)
    assert outcome['status'] == 'verified', outcome
    assert len(fake.actual) == 1
    assert '已完成界面联调，等待运行时验证。' in fake.actual[0][0]['content']
    repeat = cli._auto(loaded, 'daily', '2026-10-15', 'extractive', submit=True)
    assert repeat['status'] == 'already_submitted'
    assert len(fake.actual) == 1
