"""Report workspace regressions; all external services use local fixtures."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import create_app


@pytest.fixture
def workspace(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('data_dir: data\ncodex: {}\n', encoding='utf-8')
    app = create_app(tmp_path / 'app.db', workers=0, config_path=config)
    with TestClient(app) as client:
        yield client, app, tmp_path, config


def test_manual_work_selection_and_draft_survive_restart(workspace):
    client, app, root, config = workspace
    item = client.post('/api/v1/workspace/manual', json={
        'date': '2026-10-15', 'title': '线下测试', 'summary': '完成设备测试',
        'status': 'implemented_unverified', 'project': '设备'}).json()['data']
    response = client.patch('/api/v1/workspace/selection', json={
        'ids': [item['id']], 'selected': False, 'revision': 1})
    assert response.status_code == 200
    draft = client.patch('/api/v1/workspace/report', json={
        'date': '2026-10-15', 'kind': 'daily', 'revision': 0,
        'sections': {'completed': '完成设备测试，等待联调。'}})
    assert draft.status_code == 200, draft.text
    with TestClient(create_app(root / 'app.db', workers=0, config_path=config)) as restarted:
        data = restarted.get('/api/v1/workspace', params={'day': '2026-10-15'}).json()['data']
        assert data['tasks'][0]['selected'] is False
        assert data['report']['sections']['completed'] == '完成设备测试，等待联调。'
        assert data['report']['manual_saved'] is True
    stale = client.patch('/api/v1/workspace/report', json={
        'date': '2026-10-15', 'kind': 'daily', 'revision': 0, 'sections': {'completed': '旧版本'}})
    assert stale.status_code == 409


def test_missing_config_and_workbuddy_are_explicit(workspace):
    client, _, _, _ = workspace
    data = client.get('/api/v1/workspace', params={'day': '2026-10-15'}).json()['data']
    wb = next(x for x in data['sources'] if x['id'] == 'workbuddy')
    assert wb['status'] == 'unavailable'
    assert not data['tasks']
    assert client.get('/api/v1/workspace', params={'day': '../bad'}).status_code == 422


def test_sync_is_idempotent_and_evidence_reads_original(workspace, monkeypatch):
    client, app, root, _ = workspace
    source = root / 'source.jsonl'
    row = {'type': 'response_item', 'timestamp': '2026-10-15T01:00:00Z',
           'payload': {'type': 'message', 'role': 'assistant', 'channel': 'final',
                       'content': [{'type': 'output_text', 'text': '已实现布局，等待验证。'}]}}
    source.write_text(json.dumps(row), encoding='utf-8')
    facts = [{'evidence_id': 'one', 'source': 'codex', 'source_path': str(source),
              'source_line': 1, 'session_id': 'session', 'project': 'UI',
              'time': row['timestamp'], 'date': '2026-10-15', 'status': 'completed',
              'text': '已实现布局，等待验证。', 'title': '布局'}]
    monkeypatch.setattr('lablog.cli._collect', lambda *args: (root / 'e.json', {'facts': facts, 'warnings': []}))
    for _ in range(2):
        assert client.post('/api/v1/workspace/sync', json={'date': '2026-10-15', 'kind': 'daily'}).status_code == 200
    data = client.get('/api/v1/workspace', params={'day': '2026-10-15'}).json()['data']
    assert len(data['tasks']) == 1
    task = data['tasks'][0]
    assert task['status'] == 'implemented_unverified'
    assert 'source_path' not in task
    evidence = client.get(f"/api/v1/workspace/tasks/{task['id']}/evidence")
    assert evidence.json()['data']['messages'][0]['text'] == '已实现布局，等待验证。'
    source.unlink()
    assert client.get(f"/api/v1/workspace/tasks/{task['id']}/evidence").status_code == 410
    # A new complete snapshot after narrowing scope removes old automatic
    # candidates but keeps explicitly recorded offline work.
    client.post('/api/v1/workspace/manual', json={
        'date': '2026-10-15', 'title': '线下复核', 'summary': '核对结果', 'project': 'UI'})
    facts.clear()
    client.post('/api/v1/workspace/sync', json={'date': '2026-10-15', 'kind': 'daily'})
    remaining = client.get('/api/v1/workspace?day=2026-10-15').json()['data']['tasks']
    assert len(remaining) == 1
    assert remaining[0]['source'] == 'manual'


def test_cross_origin_mutation_is_rejected(workspace):
    client, *_ = workspace
    response = client.post('/api/v1/workspace/sync', headers={'Origin': 'https://evil.example'},
                           json={'date': '2026-10-15', 'kind': 'daily'})
    assert response.status_code == 403


def test_first_save_binds_live_template_and_writes_shared_scheduler_draft(workspace, monkeypatch):
    client, _, root, config = workspace
    config.write_text('data_dir: data\ncodex: {}\ntemplates:\n  daily:\n    id: t\n    name: 学生日报\n', encoding='utf-8')
    template = {'report_template_id': 't', 'report_template_name': '学生日报', 'report_template_fields': [
        {'field_name': '今日完成', 'field_type': 1, 'field_sort': 0},
        {'field_name': '心得体会', 'field_type': 1, 'field_sort': 1}]}
    class FakeDws:
        def template(self, *args): return template
    monkeypatch.setattr('lablog.cli._client', lambda cfg: FakeDws())
    monkeypatch.setattr('lablog.cli._collect', lambda *args: (root / 'e.json', {'facts': [], 'warnings': []}))
    response = client.patch('/api/v1/workspace/report', json={
        'date': '2026-10-15', 'kind': 'daily', 'revision': 0,
        'sections': {'completed': '人工保存的具体工作。', 'progress': '保留进展原文。'}})
    assert response.status_code == 200, response.text
    draft = json.loads((root / 'data/drafts/daily-2026-10-15.json').read_text(encoding='utf-8'))
    assert draft['template_id'] == 't'
    assert draft['manual_saved'] is True
    assert next(f['value'] for f in draft['fields'] if f['name'] == '今日完成') == '人工保存的具体工作。'
    assert 'no dated completion evidence' in draft['blockers']
    cleared = client.patch('/api/v1/workspace/report', json={
        'date': '2026-10-15', 'kind': 'daily', 'revision': response.json()['data']['revision'],
        'sections': {'progress': '', 'problems': '', 'solutions': ''}})
    assert cleared.status_code == 200
    draft = json.loads((root / 'data/drafts/daily-2026-10-15.json').read_text(encoding='utf-8'))
    assert next(f['value'] for f in draft['fields'] if f['name'] == '心得体会') == ''


def test_untrusted_host_is_rejected_on_loopback(workspace):
    client, *_ = workspace
    response = client.get('/api/v1/workspace?day=2026-10-15', headers={'Host': 'evil.example'})
    assert response.status_code == 403


def test_redaction_preserves_public_links_but_removes_local_paths_and_tokens():
    from backend.log_service import safe_text
    assert safe_text('https://github.com/org/repo') == 'https://github.com/org/repo'
    assert 'private' not in safe_text('文件 C:/Users/private/file.txt')
    assert 'token-value' not in safe_text('Bearer token-value')
