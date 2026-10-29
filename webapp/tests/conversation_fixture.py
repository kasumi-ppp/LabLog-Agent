"""Build an isolated native-format source for browser tests. No real accounts."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
wb = root / 'native-workbuddy'
cx = root / 'native-codex'
wb.mkdir(exist_ok=True)
cx.mkdir(exist_ok=True)
stamp = datetime(2026, 10, 15, 1, tzinfo=timezone.utc)
millis = int(stamp.timestamp() * 1000)
with sqlite3.connect(wb / 'workbuddy.db') as conn:
    conn.execute('CREATE TABLE sessions(id TEXT,cwd TEXT,title TEXT,custom_title TEXT,status TEXT,created_at INTEGER,updated_at INTEGER,last_activity_at INTEGER,deleted_at INTEGER,is_playground INTEGER)')
    for sid, deleted in [('wb-main', None), ('wb-deleted', millis)]:
        conn.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (sid, 'E:/project/钉钉', '原始标题', 'WorkBuddy · 布局与异常恢复', 'completed', millis, millis, millis, deleted, 0))
        path = wb / 'projects' / 'dingtalk' / (sid + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [{'id': 'u1', 'sessionId': sid, 'type': 'message', 'timestamp': millis, 'role': 'user',
                   'content': [{'type': 'input_text', 'text': '完善操作面板布局，补充异常恢复。'}]}]
        for n in range(55):
            events.append({'id': f'a{n}', 'sessionId': sid, 'type': 'message', 'timestamp': millis + n + 1,
                           'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text',
                           'text': '已完成布局调整，等待运行时验证。' if n == 0 else f'正在联调第 {n} 项交互，尚未完成最终验证。'}]})
        events += [{'id': 'tool1', 'sessionId': sid, 'type': 'function_call', 'timestamp': millis + 100,
                    'name': 'run_tests', 'callId': 'call1', 'arguments': 'do not display'},
                   {'id': 'tool2', 'sessionId': sid, 'type': 'function_call_result', 'timestamp': millis + 101,
                    'name': 'run_tests', 'callId': 'call1', 'status': 'completed', 'output': 'do not display'}]
        path.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8')
conn.close()
with sqlite3.connect(cx / 'state_5.sqlite') as conn:
    conn.execute('CREATE TABLE threads(id TEXT,cwd TEXT,title TEXT,rollout_path TEXT,created_at INTEGER,updated_at INTEGER,archived INTEGER,source TEXT)')
    for sid, project, archived, source in [('cx-main', '钉钉', 0, 'vscode'), ('cx-archive', '钉钉', 1, 'vscode'),
                                           ('cx-vr', 'VR', 0, 'vscode'), ('cx-agent', '钉钉', 0, '{"subagent":{"other":"guardian"}}')]:
        path = cx / 'sessions' / (sid + '.jsonl')
        path.parent.mkdir(exist_ok=True)
        conn.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)', (sid, 'E:/project/' + project,
                     'Codex · 代码审查' + ('（归档）' if archived else ''), str(path), int(stamp.timestamp()), int(stamp.timestamp()), archived, source))
        rows = [{'type': 'session_meta', 'payload': {'id': sid, 'cwd': 'E:/project/' + project}},
                {'type': 'response_item', 'timestamp': stamp.isoformat(), 'payload': {'type': 'message', 'role': 'assistant',
                 'channel': 'final', 'content': [{'type': 'output_text', 'text': '已完成状态切换代码审查，建议补充运行时测试。'}]}}]
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
conn.close()
config = {'data_dir': str(root / 'data'), 'conversations': {'enabled': True},
          'workbuddy': {'enabled': True, 'root': str(wb)}, 'codex': {'metadata_db': str(cx / 'state_5.sqlite'),
          'session_roots': [str(cx / 'sessions')], 'project_paths': ['E:/project/VR']}}
(root / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
print('Isolated source fixture created')
