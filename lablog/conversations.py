"""Shared native conversation directory and persistent report selection.

Only metadata, short summaries and byte locators are cached. Message bodies are
read from the original files when requested by the UI or report collector.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .config import resolve_path
from .native_log import open_log
from .sources import CHINA, DONE, PROGRESS, SECRET, _allowed, _canonical_message, _normal_path, period_bounds


class SelectionConflict(ValueError):
    pass


class EvidenceUnavailable(ValueError):
    pass


def enabled(cfg):
    return bool((cfg.get('conversations') or {}).get('enabled') or (cfg.get('workbuddy') or {}).get('enabled')
                or (cfg.get('chatgpt') or {}).get('enabled'))


def clean_text(value):
    return re.sub(r'(?i)Bearer\s+\S+|(?<![A-Za-z0-9])[A-Z]:[\\/][^\s\n，。]+', '[已隐藏]',
                  SECRET.sub('[敏感信息已隐藏]', str(value or '')))


def _iso(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 100000000000 else value, timezone.utc).isoformat()
    return str(value or '')


def _child(source):
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return False
    return isinstance(source, dict) and ('subagent' in source or source.get('kind') == 'subagent')


def _codex_event(row, sid):
    parsed = _canonical_message(row)
    if parsed:
        role, channel, text = parsed
        try:
            stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return None
        except (ValueError, TypeError, KeyError):
            return None
        event_id = hashlib.sha256((sid + '\0' + stamp.isoformat() + '\0' + role + '\0' + text).encode()).hexdigest()[:20]
        status = 'planned' if role == 'user' else ('completed' if channel in {'final', 'final_answer'} and DONE.search(text)
                                                   else 'in_progress' if channel == 'commentary' or PROGRESS.search(text) else 'unknown')
        return {'event_id': event_id, 'kind': 'message', 'role': role, 'time': stamp.astimezone(timezone.utc).isoformat(),
                'text': text, 'status': status, 'channel': channel, 'message_status': ''}
    payload = row.get('payload') or {}
    if row.get('type') == 'response_item' and payload.get('type') in {'function_call', 'function_call_output'}:
        try:
            stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        call_id = str(payload.get('call_id') or payload.get('id') or '')
        if not call_id:
            return None
        name = str(payload.get('name') or '工具结果')
        return {'event_id': payload['type'] + ':' + call_id, 'kind': 'tool', 'role': 'tool',
                'time': stamp.astimezone(timezone.utc).isoformat(), 'text': clean_text(name),
                'tool_name': clean_text(name), 'call_id': call_id, 'status': 'unknown',
                'message_status': 'recorded', 'channel': None}
    return None


class ConversationIndex:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = resolve_path(cfg, cfg.get('data_dir', 'data')) / 'conversations.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    key TEXT PRIMARY KEY, body TEXT NOT NULL,
                    selection INTEGER, revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS events (
                    conversation_key TEXT NOT NULL, event_id TEXT NOT NULL,
                    kind TEXT NOT NULL, time TEXT NOT NULL, body TEXT NOT NULL,
                    byte_offset INTEGER NOT NULL, byte_length INTEGER NOT NULL,
                    line INTEGER NOT NULL,
                    PRIMARY KEY(conversation_key,event_id));
                CREATE INDEX IF NOT EXISTS idx_events_period ON events(conversation_key,kind,time);
                CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS source_health (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            ''')

    @contextmanager
    def _connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA busy_timeout=30000')
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _rows(self):
        with self._connection() as conn:
            return [{**json.loads(r['body']), 'selection': r['selection'], 'revision': r['revision']}
                    for r in conn.execute('SELECT * FROM conversations')]

    def sources(self):
        with self._connection() as conn:
            return [json.loads(r['body']) for r in conn.execute('SELECT body FROM source_health ORDER BY id')]

    def _get(self, key):
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM conversations WHERE key=?', (key,)).fetchone()
        if row is None:
            raise EvidenceUnavailable('会话不存在，请重新同步。')
        return {**json.loads(row['body']), 'selection': row['selection'], 'revision': row['revision']}

    def _excluded(self, item):
        settings = self.cfg.get(item['source']) or {}
        if item['source'] == 'chatgpt' and item['id'] not in (settings.get('session_ids') or []):
            return True
        if item['source'] == 'codex' and settings.get('_explicit_run_scope'):
            ids = settings.get('session_ids') or []
            if (ids and item['id'] not in ids) or (not ids and not _allowed(item.get('cwd'), settings.get('project_paths', []), [])):
                return True
        excluded = settings.get('exclude_paths', (self.cfg.get('codex') or {}).get('exclude_paths', []))
        cwd = _normal_path(item.get('cwd'))
        return any(cwd == _normal_path(p) or cwd.startswith(_normal_path(p) + '/') for p in excluded)

    def _selected(self, item):
        if item['source'] in {'workbuddy', 'chatgpt'} and not (self.cfg.get(item['source']) or {}).get('enabled'):
            return False
        if item.get('deleted') or item.get('missing') or self._excluded(item):
            return False
        if item.get('selection') is not None:
            return bool(item['selection'])
        settings = self.cfg.get(item['source']) or {}
        ids = settings.get('session_ids') or []
        if ids:
            return item['id'] in ids
        projects = settings.get('project_paths', (self.cfg.get('codex') or {}).get('project_paths', []))
        return _allowed(item.get('cwd'), projects, settings.get('exclude_paths', []))

    def _project(self, item):
        if item['source'] == 'chatgpt':
            return 'chatgpt:monitored', 'ChatGPT'
        if item.get('temporary'):
            return 'workbuddy:temporary', '临时会话'
        cwd = str(item.get('cwd') or '')
        roots = list((self.cfg.get('codex') or {}).get('project_paths') or [])
        roots += list((self.cfg.get('workbuddy') or {}).get('project_paths') or [])
        matches = [p for p in roots if _allowed(cwd, [p], [])]
        root = max(matches, key=lambda p: len(_normal_path(p))) if matches else cwd
        identity = _normal_path(root) or 'unclassified'
        name = str(root).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] or '未分类'
        return hashlib.sha256(identity.encode()).hexdigest()[:20], name

    def _public(self, item):
        project_id, project_name = self._project(item)
        with self._connection() as conn:
            counts = {r['kind']: r['n'] for r in conn.execute(
                'SELECT kind,count(*) n FROM events WHERE conversation_key=? GROUP BY kind', (item['key'],))}
            indexed = conn.execute('SELECT 1 FROM cursors WHERE key=?', (item['key'],)).fetchone()
        return {'key': item['key'], 'id': item['id'], 'source': item['source'], 'title': clean_text(item['title'])[:160],
                'project_id': project_id, 'project_name': project_name, 'selected': self._selected(item),
                'revision': item['revision'], 'archived': bool(item.get('archived')), 'deleted': bool(item.get('deleted') or item.get('missing')),
                'temporary': bool(item.get('temporary')), 'updated_at': item.get('updated_at'),
                'created_at': item.get('created_at'), 'message_count': counts.get('message', 0) if indexed else None,
                'tool_count': counts.get('tool', 0) if indexed else None,
                'available': bool(item.get('path')) and not item.get('missing', False) and (
                    item['source'] not in {'workbuddy', 'chatgpt'} or bool((self.cfg.get(item['source']) or {}).get('enabled'))),
                'excluded': self._excluded(item)}

    def _discover_codex(self):
        settings = self.cfg.get('codex') or {}
        roots = [resolve_path(self.cfg, p) for p in settings.get('session_roots', [])]
        db = settings.get('metadata_db')
        candidates = [resolve_path(self.cfg, db)] if db else sorted({p for r in roots for p in r.parent.glob('state_*.sqlite')}, reverse=True)
        if candidates:
            try:
                conn = sqlite3.connect(candidates[0].resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
                try:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute('SELECT id,cwd,title,rollout_path,created_at,updated_at,archived,source FROM threads').fetchall()
                finally:
                    conn.close()
                return {'reachable': True, 'conversations': [
                    {'id': r['id'], 'source': 'codex', 'cwd': r['cwd'], 'title': r['title'] or r['id'],
                     'path': r['rollout_path'], 'created_at': _iso(r['created_at']), 'updated_at': _iso(r['updated_at']),
                     'archived': bool(r['archived']), 'deleted': False, 'temporary': False}
                    for r in rows if not _child(r['source'])], 'warnings': []}
            except (sqlite3.Error, OSError, ValueError):
                return {'reachable': False, 'conversations': [], 'warnings': ['Codex 会话索引无法读取']}
        # CLI-only installations may have rollouts without the desktop database.
        items, warnings = [], []
        titles = {}
        for root in roots:
            index = root.parent / 'session_index.jsonl'
            if index.is_file():
                try:
                    for line in index.read_text(encoding='utf-8').splitlines():
                        try:
                            entry = json.loads(line)
                            titles[entry['id']] = entry.get('thread_name')
                        except (ValueError, KeyError, TypeError):
                            continue
                except OSError:
                    warnings.append('Codex 标题索引暂不可读')
            files = [root] if root.is_file() else root.rglob('*.jsonl') if root.is_dir() else []
            for path in files:
                try:
                    with path.open(encoding='utf-8') as stream:
                        for _, line in zip(range(50), stream):
                            row = json.loads(line)
                            if row.get('type') != 'session_meta':
                                continue
                            meta = row.get('payload') or {}
                            if meta.get('id') and not _child(meta.get('source')):
                                items.append({'id': str(meta['id']), 'source': 'codex', 'cwd': meta.get('cwd', ''),
                                              'title': titles.get(meta['id']) or str(meta['id']), 'path': str(path),
                                              'created_at': row.get('timestamp'), 'updated_at': _iso(path.stat().st_mtime),
                                              'archived': root.name == 'archived_sessions', 'deleted': False, 'temporary': False})
                            break
                except (ValueError, OSError, UnicodeError):
                    warnings.append('部分 Codex 会话元数据不可读')
        return {'reachable': any(r.exists() for r in roots) if roots else None, 'conversations': items, 'warnings': list(dict.fromkeys(warnings))}

    def refresh(self):
        from .workbuddy import discover_workbuddy
        wb = self.cfg.get('workbuddy') or {}
        discoveries = {'codex': self._discover_codex()}
        discoveries['workbuddy'] = discover_workbuddy(resolve_path(self.cfg, wb.get('root', '~/.workbuddy'))) if wb.get('enabled') else {
            'reachable': None, 'conversations': [], 'warnings': []}
        if self.cfg.get('chatgpt') or any(r['source'] == 'chatgpt' for r in self._rows()):
            from .chatgpt import discover_chatgpt
            settings = self.cfg.get('chatgpt') or {}
            discoveries['chatgpt'] = discover_chatgpt(
                resolve_path(self.cfg, settings.get('root', 'data/chatgpt-source')),
                settings.get('session_ids', []), settings.get('max_age_hours', 24)) if settings.get('enabled') else {
                    'reachable': None, 'conversations': [], 'warnings': []}
        warnings = []
        for source, result in discoveries.items():
            warnings.extend(result.get('warnings', []))
            seen = set()
            with self._connection(write=True) as conn:
                for item in result['conversations']:
                    item = {**item, 'key': source + ':' + item['id'], 'missing': False}
                    seen.add(item['key'])
                    item['title'] = clean_text(item['title'])[:160]
                    conn.execute('''INSERT INTO conversations(key,body) VALUES(?,?)
                        ON CONFLICT(key) DO UPDATE SET body=excluded.body''', (item['key'], json.dumps(item)))
                if result.get('reachable') is True:
                    for row in conn.execute('SELECT key,body FROM conversations WHERE key LIKE ?', (source + ':%',)).fetchall():
                        if row['key'] not in seen:
                            body = json.loads(row['body'])
                            body['missing'] = True
                            conn.execute('UPDATE conversations SET body=? WHERE key=?', (json.dumps(body), row['key']))
                health = {'id': source, 'status': 'available' if result.get('reachable') else 'unavailable',
                          'reachable': result.get('reachable'), 'conversation_count': len(seen),
                          'message': '原生会话索引可用' if result.get('reachable') else '未配置或原始索引不可访问'}
                if source == 'chatgpt':
                    health['message'] = 'ChatGPT 同步快照可用；助手正文若不可读，仅统计真实提问' if result.get('reachable') else 'ChatGPT 同步快照缺失、过期或不可访问'
                    health['warnings'] = result.get('warnings', [])
                conn.execute('INSERT OR REPLACE INTO source_health VALUES(?,?)', (source, json.dumps(health)))
        for item in self._rows():
            if item.get('deleted') or item.get('missing') or self._excluded(item):
                continue
            if item['source'] == 'workbuddy' or self._selected(item):
                try:
                    warnings.extend(self._index_one(item))
                except EvidenceUnavailable as exc:
                    warnings.append(str(exc))
        return {'sources': self.sources(), 'warnings': list(dict.fromkeys(warnings))}

    def _source_path(self, item):
        if item['source'] in {'workbuddy', 'chatgpt'} and not (self.cfg.get(item['source']) or {}).get('enabled'):
            raise EvidenceUnavailable(item['source'] + ' 数据源已关闭。')
        if not item.get('path') or item.get('missing') or item.get('deleted') or self._excluded(item):
            raise EvidenceUnavailable('原始会话已删除、退出范围或不可访问；历史摘要保留。')
        try:
            requested = Path(item['path']).expanduser()
            if item['source'] == 'codex' and requested.suffix == '.jsonl' and not requested.exists():
                requested = requested.with_suffix('.jsonl.zst')
            path = requested.resolve(strict=True)
            if item['source'] == 'workbuddy':
                roots = [resolve_path(self.cfg, (self.cfg.get('workbuddy') or {}).get('root', '~/.workbuddy')) / 'projects']
            elif item['source'] == 'chatgpt':
                roots = [resolve_path(self.cfg, self.cfg['chatgpt'].get('root', 'data/chatgpt-source'))]
            else:
                roots = [resolve_path(self.cfg, p) for p in (self.cfg.get('codex') or {}).get('session_roots', [])]
            if not _allowed(path, [r.resolve() for r in roots], []):
                raise OSError('source outside configured roots')
            return path
        except (OSError, ValueError):
            raise EvidenceUnavailable('原始会话不可访问，请检查数据源。') from None

    def _parse(self, row, item):
        if item['source'] == 'chatgpt':
            from .chatgpt import parse_chatgpt_event
            return parse_chatgpt_event(row, item['id'])
        if item['source'] == 'workbuddy':
            from .workbuddy import parse_workbuddy_event
            return parse_workbuddy_event(row, item['id'])
        return _codex_event(row, item['id'])

    def _index_one(self, item):
        path = self._source_path(item)
        if item['source'] == 'chatgpt':
            return self._index_chatgpt(item, path)
        warnings = []
        try:
            stat = path.stat()
            file_id = f'{stat.st_dev}:{stat.st_ino}'
            with self._connection() as conn:
                row = conn.execute('SELECT body FROM cursors WHERE key=?', (item['key'],)).fetchone()
            cursor = json.loads(row['body']) if row else {}
            with path.open('rb') as stream:
                head = stream.read(min(stat.st_size, 4096))
            prefix = hashlib.sha256(head).hexdigest()
            old_prefix = hashlib.sha256(head[:min(cursor.get('size', 0), 4096)]).hexdigest()
            if cursor.get('version') == 2 and cursor.get('size') == stat.st_size and cursor.get('mtime') == stat.st_mtime_ns and cursor.get('path') == str(path) and cursor.get('file_id') == file_id:
                return cursor.get('warnings', [])
            append = (path.suffix != '.zst' and cursor.get('version') == 2 and cursor.get('path') == str(path) and cursor.get('size', 0) < stat.st_size
                      and cursor.get('prefix') == old_prefix and cursor.get('file_id') == file_id)
            offset, line_number = (cursor.get('offset', 0), cursor.get('line', 0)) if append else (0, 0)
            entries = []
            with open_log(path) as stream:
                stream.seek(offset)
                while True:
                    position = stream.tell()
                    raw = stream.readline()
                    if not raw:
                        break
                    # A partial tail must not advance the durable cursor.
                    if not raw.endswith(b'\n'):
                        warnings.append('会话尾行尚未写完 (partial)，下次同步重试')
                        break
                    line_number += 1
                    offset = stream.tell()
                    try:
                        row = json.loads(raw.decode('utf-8'))
                        if item['source'] == 'codex' and isinstance(row, dict) and row.get('type') == 'session_meta':
                            if str((row.get('payload') or {}).get('id')) != item['id']:
                                raise EvidenceUnavailable('原始日志与会话标识不一致，已保留历史摘要。')
                        event = self._parse(row, item)
                    except EvidenceUnavailable:
                        raise
                    except (ValueError, TypeError, UnicodeError, AttributeError):
                        warnings.append('会话中存在无法解析的事件，已跳过')
                        continue
                    if event is None:
                        continue
                    body = {k: v for k, v in event.items() if k != 'text'}
                    body['summary'] = clean_text(event.get('text'))[:600]
                    entries.append((item['key'], event['event_id'], event['kind'], event['time'], json.dumps(body),
                                    position, len(raw), line_number))
            with self._connection(write=True) as conn:
                if not append:
                    conn.execute('DELETE FROM events WHERE conversation_key=?', (item['key'],))
                conn.executemany('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?)', entries)
                conn.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (item['key'], json.dumps({
                    'version': 2, 'path': str(path), 'size': stat.st_size, 'mtime': stat.st_mtime_ns, 'prefix': prefix, 'file_id': file_id,
                    'offset': offset, 'line': line_number, 'warnings': list(dict.fromkeys(warnings))})))
            return list(dict.fromkeys(warnings))
        except (OSError, sqlite3.Error):
            raise EvidenceUnavailable('会话增量索引未完成，原有记录保留。') from None

    def _source_names(self):
        has_chatgpt = self.cfg.get('chatgpt') or any(r['source'] == 'chatgpt' for r in self._rows())
        return ('codex', 'workbuddy', 'chatgpt') if has_chatgpt else ('codex', 'workbuddy')

    def _index_chatgpt(self, item, path):
        from .chatgpt import read_snapshot, snapshot_events
        try:
            stat = path.stat()
            with self._connection() as conn:
                row = conn.execute('SELECT body FROM cursors WHERE key=?', (item['key'],)).fetchone()
            cursor = json.loads(row['body']) if row else {}
            if cursor.get('path') == str(path) and cursor.get('mtime') == stat.st_mtime_ns and cursor.get('size') == stat.st_size:
                return []
            snapshot = read_snapshot(path, item['id'])
            entries = []
            for line, row in enumerate(snapshot_events(snapshot, item['id']), 1):
                event = self._parse(row, item)
                if event:
                    body = {k: v for k, v in event.items() if k != 'text'}
                    body['summary'] = clean_text(event['text'])[:600]
                    entries.append((item['key'], event['event_id'], event['kind'], event['time'], json.dumps(body), 0, 0, line))
            with self._connection(write=True) as conn:
                conn.execute('DELETE FROM events WHERE conversation_key=?', (item['key'],))
                conn.executemany('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?)', entries)
                conn.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (item['key'], json.dumps({
                    'path': str(path), 'mtime': stat.st_mtime_ns, 'size': stat.st_size})))
            return []
        except (OSError, ValueError, TypeError):
            raise EvidenceUnavailable('ChatGPT 同步快照无法读取，历史摘要保留。') from None

    def projects(self):
        result = {}
        sources = self._source_names()
        for row in self._rows():
            key, name = self._project(row)
            item = result.setdefault(key, {'id': key, 'name': name, 'conversation_count': 0,
                                           'counts': {s: 0 for s in sources}})
            if not row.get('deleted') and not row.get('missing'):
                item['conversation_count'] += 1
                item['counts'][row['source']] += 1
        return sorted(result.values(), key=lambda x: x['name'].casefold())

    def list(self, project_id=None, source=None, q='', include_archived=False, include_deleted=False, limit=50, offset=0):
        rows = [r for r in self._rows() if (include_deleted or not r.get('missing'))
                and (include_deleted or not r.get('deleted')) and (include_archived or not r.get('archived'))
                and (not project_id or self._project(r)[0] == project_id)
                and (not q or q.casefold() in (r['title'] + ' ' + self._project(r)[1]).casefold())]
        counts = {s: sum(r['source'] == s for r in rows) for s in self._source_names()}
        if source:
            rows = [r for r in rows if r['source'] == source]
        rows.sort(key=lambda r: r.get('updated_at') or '', reverse=True)
        return {'items': [self._public(r) for r in rows[offset:offset + limit]], 'total': len(rows),
                'counts': counts,
                'selected_count': sum(self._selected(r) for r in rows)}

    def select(self, changes):
        with self._connection(write=True) as conn:
            for change in changes:
                row = conn.execute('SELECT * FROM conversations WHERE key=?', (change['key'],)).fetchone()
                if row is None or row['revision'] != change['revision']:
                    raise SelectionConflict('会话选择已更新，请刷新后重试。')
                item = json.loads(row['body'])
                if change['selected'] and item['source'] in {'workbuddy', 'chatgpt'} and not (self.cfg.get(item['source']) or {}).get('enabled'):
                    raise EvidenceUnavailable(item['source'] + ' 数据源已关闭，不能选入报告。')
                if change['selected'] and (item.get('deleted') or item.get('missing') or self._excluded(item)):
                    raise EvidenceUnavailable('该会话已删除或被明确排除，不能选入报告。')
                conn.execute('UPDATE conversations SET selection=?,revision=revision+1 WHERE key=?',
                             (change['selected'], change['key']))
        return [self._public(self._get(c['key'])) for c in changes]

    def _events(self, key, kind, day, view):
        start, end = period_bounds(kind, date.fromisoformat(str(day)))
        window = (self.cfg.get('chatgpt') or {}).get('daily_window') or {}
        if kind == 'daily' and key.startswith('chatgpt:') and window.get('enabled'):
            from datetime import timedelta
            end = datetime.combine(date.fromisoformat(str(day)), datetime.min.time(), CHINA).replace(hour=18)
            start = end - timedelta(days=1)
            start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        with self._connection() as conn:
            return conn.execute('''SELECT * FROM events WHERE conversation_key=? AND kind=? AND time>=? AND time<?
                ORDER BY time,line''', (key, 'tool' if view == 'tools' else 'message', start.isoformat(), end.isoformat())).fetchall()

    def _read(self, item, record):
        path = self._source_path(item)
        try:
            if item['source'] == 'chatgpt':
                from .chatgpt import read_snapshot, snapshot_events
                row = next((r for r in snapshot_events(read_snapshot(path, item['id']), item['id'])
                            if str(r.get('id')) == record['event_id']), None)
                event = self._parse(row, item) if row else None
                if not event:
                    raise ValueError('snapshot locator changed')
                return {**event, 'text': clean_text(event['text']), 'line': record['line']}
            with open_log(path) as stream:
                stream.seek(record['byte_offset'])
                row = json.loads(stream.read(record['byte_length']).decode('utf-8'))
            event = self._parse(row, item)
            if not event or event['event_id'] != record['event_id']:
                raise ValueError('locator changed')
            return {**event, 'text': clean_text(event['text']), 'line': record['line']}
        except (OSError, ValueError, KeyError, TypeError):
            raise EvidenceUnavailable('原始证据定位已变化，请重新同步。') from None

    def messages(self, key, kind, day, offset=0, limit=50, view='messages'):
        item = self._get(key)
        self._index_one(item)
        rows = self._events(key, kind, day, view)
        page = [self._read(item, r) for r in rows[offset:offset + limit]]
        return {'messages': page if view == 'messages' else [], 'tools': page if view == 'tools' else [],
                'total': len(rows), 'next_offset': offset + limit if offset + limit < len(rows) else None,
                'summary': self._public(item)}

    def evidence(self, key, message_id):
        item = self._get(key)
        self._index_one(item)
        with self._connection() as conn:
            record = conn.execute('SELECT * FROM events WHERE conversation_key=? AND event_id=?',
                                  (key, message_id)).fetchone()
        if record is None:
            raise EvidenceUnavailable('原始消息定位已变化，请重新同步。')
        return self._read(item, record)

    def collect(self, kind, day):
        health = self.refresh()
        facts, blocked = [], []
        unavailable = {s['id'] for s in health['sources'] if s.get('reachable') is False}
        if 'chatgpt' in unavailable:
            indexed_ids = {r['id'] for r in self._rows() if r['source'] == 'chatgpt'}
            if set((self.cfg.get('chatgpt') or {}).get('session_ids', [])) - indexed_ids:
                blocked.append('已授权 ChatGPT 会话尚无可用同步快照，本次自动提交已阻止')
        for item in self._rows():
            if not self._selected(item):
                continue
            if item['source'] in unavailable:
                blocked.append(item['source'] + ' 会话索引不可访问，本次自动提交已阻止')
                continue
            try:
                self._index_one(item)
                for record in self._events(item['key'], kind, day, 'messages'):
                    event = self._read(item, record)
                    ident = event['event_id'] if item['source'] == 'codex' else hashlib.sha256(
                        (item['key'] + ':' + event['event_id']).encode()).hexdigest()[:20]
                    facts.append({'evidence_id': ident, 'source': item['source'], 'source_type': item['source'],
                                  'session_id': item['id'], 'message_id': event['event_id'], 'conversation_key': item['key'],
                                  'project': item['cwd'], 'source_path': item['path'], 'source_line': record['line'],
                                  'time': event['time'], 'date': datetime.fromisoformat(event['time']).astimezone(CHINA).date().isoformat(),
                                  'role': event['role'], 'channel': event.get('channel'), 'status': event['status'],
                                  'text': event['text'], 'title': item['title'], 'task_id': ident, 'evidence': [ident],
                                  'confidence': 'medium' if event['status'] == 'completed' else 'low',
                                  'notes': 'assistant reported completion; not independently verified' if event['status'] == 'completed' else ''})
            except EvidenceUnavailable as exc:
                blocked.append(str(exc))
        return {'facts': sorted(facts, key=lambda f: f['time']), 'warnings': health['warnings'] + blocked,
                'sources': health['sources'], 'blockers': list(dict.fromkeys(blocked))}
