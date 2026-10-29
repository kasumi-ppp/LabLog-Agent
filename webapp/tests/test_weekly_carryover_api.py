"""Web generation uses the same dated weekly pool as unattended generation."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import create_app
from lablog import cli


def test_weekly_web_generation_fills_previous_week_and_protects_saved_draft(tmp_path, monkeypatch):
    root = tmp_path / 'workbuddy'
    root.mkdir()
    stamp = datetime(2026, 10, 16, 1, tzinfo=timezone.utc).timestamp() * 1000
    with sqlite3.connect(root / 'workbuddy.db') as conn:
        conn.execute('CREATE TABLE sessions(id TEXT,cwd TEXT,title TEXT,custom_title TEXT,created_at INTEGER,updated_at INTEGER,last_activity_at INTEGER,deleted_at INTEGER,is_playground INTEGER)')
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)', ('weekly', 'E:/project/VR', '工作', None, stamp, stamp, stamp, None, 0))
    conn.close()
    log = root / 'projects/vr/weekly.jsonl'
    log.parent.mkdir(parents=True)
    rows = []
    for ident, day, text in [('current', 16, '已完成本周接口修复。'), ('old', 11, '已完成周末分类脚本验证。')]:
        rows.append({'id': ident, 'sessionId': 'weekly', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                     'timestamp': datetime(2026, 10, day, 1, tzinfo=timezone.utc).timestamp() * 1000,
                     'content': [{'type': 'output_text', 'text': text}]})
    log.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    cfg = {'data_dir': str(tmp_path / 'data'), 'conversations': {'enabled': True},
           'workbuddy': {'enabled': True, 'root': str(root)}, 'codex': {'project_paths': ['E:/project/VR']},
           'quality': {'weekly_carryover': {'enabled': True, 'target_chars': 200}},
           'templates': {'weekly': {'id': 't', 'name': '学生周报'}}}
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(cfg), encoding='utf-8')
    class FakeDws:
        def template(self, *args):
            return {'report_template_id': 't', 'report_template_name': '学生周报', 'allowAddReceivers': 1,
                    'report_template_fields': [{'field_name': '本周完成工作', 'field_type': 1, 'field_sort': 0},
                                              {'field_name': '本周工作总结', 'field_type': 1, 'field_sort': 1}]}
    monkeypatch.setattr(cli, '_client', lambda *args: FakeDws())
    with TestClient(create_app(tmp_path / 'web.db', workers=0, config_path=config)) as client:
        response = client.post('/api/v1/workspace/generate', json={'date': '2026-10-16', 'kind': 'weekly'})
        assert response.status_code == 200, response.text
        report = response.json()['data']
        assert '上周补充（原始工作日期：2026-10-11）' in report['sections']['completed']
        assert '本周接口修复' in report['sections']['completed'] and '周末分类脚本验证' in report['sections']['completed']
        path = tmp_path / 'data/drafts/weekly-2026-10-16.json'
        before = path.read_bytes()
        assert json.loads(before)['weekly_pool']['filled_items']
        repeated = client.post('/api/v1/workspace/generate', json={'date': '2026-10-16', 'kind': 'weekly'})
        assert repeated.status_code == 409
        assert path.read_bytes() == before
