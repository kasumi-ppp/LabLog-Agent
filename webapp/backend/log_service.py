"""Report workspace backed by the existing LabLog collection and submission flow."""
import hashlib
import json
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

from lablog import cli, state
from lablog.config import load_config, resolve_path
from lablog.conversations import ConversationIndex, EvidenceUnavailable, SelectionConflict, enabled as conversations_enabled
from lablog.report import FIELDS, build_draft
from lablog.sources import CHINA, SECRET, _canonical_message, period_bounds
from lablog.workflow import (atomic_json, draft_hash, file_lock, load_json_durable,
                             SubmissionBlocked)
from .service import AppError

SECTIONS = ('completed', 'progress', 'problems', 'solutions', 'plan')


def safe_text(value):
    text = str(value or '')
    text = SECRET.sub('[敏感信息已隐藏]', text)
    # Do not expose local account paths or bearer credentials in summaries.
    import re
    return re.sub(r'(?i)Bearer\s+\S+|(?<![A-Za-z0-9])[A-Z]:[\\/][^\s\n，。]+', '[已隐藏]', text)


def scope_key(cfg):
    scope = {key: cfg.get(key) for key in ('codex', 'workbuddy', 'chatgpt', 'conversations', 'supplemental')}
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


class LogService:
    def __init__(self, repo, config_path):
        self.repo = repo
        self.config_path = Path(config_path) if config_path else None
        self.lock = threading.RLock()

    def conversation_index(self):
        return ConversationIndex(self.config())

    def projects(self):
        index = self.conversation_index()
        index.refresh()
        return index.projects()

    def conversations(self, **filters):
        index = self.conversation_index()
        if not index.list(include_archived=True, include_deleted=True)['total']:
            index.refresh()
        return index.list(**filters)

    def select_conversations(self, changes):
        try:
            return self.conversation_index().select(changes)
        except SelectionConflict as exc:
            raise AppError('SELECTION_CONFLICT', str(exc), 409)
        except EvidenceUnavailable as exc:
            raise AppError('EVIDENCE_MISSING', str(exc), 410)

    def conversation_messages(self, key, **filters):
        try:
            return self.conversation_index().messages(key, **filters)
        except EvidenceUnavailable as exc:
            raise AppError('EVIDENCE_MISSING', str(exc), 410)

    def config(self):
        if self.config_path is None or not self.config_path.is_file():
            raise AppError('CONFIG_MISSING', '请通过 --config 指定现有 LabLog 配置，再同步或生成。', 409)
        cfg = load_config(self.config_path)
        scope = Path(cfg['_base']) / 'config' / 'scope.json'
        return cli._apply_scope(cfg, scope if scope.exists() else None)

    def _draft(self, kind, day):
        try:
            cfg = self.config()
        except AppError:
            return None, None
        path = cli._path(cfg, kind, day, 'drafts')
        if not path.is_file():
            return path, None
        draft, _ = load_json_durable(path)
        return path, draft

    def report(self, kind, day):
        _, draft = self._draft(kind, day)
        saved = self.repo.report(f'{kind}-{day}')
        if draft:
            sections = {key: '' for key in SECTIONS}
            names = FIELDS[kind]
            for field in draft.get('fields', []):
                for key, name in names.items():
                    if field['name'] == name and key in sections:
                        sections[key] = safe_text(field.get('value', ''))
                if field['name'] == names['summary']:
                    sections['progress'] = safe_text(field.get('value', ''))
            sections.update(draft.get('workspace_sections') or {})
            record = state.load_period(state_path=cli._state_path(self.config(), draft), draft=draft)
            return {'sections': sections, 'revision': draft.get('workspace_revision', draft.get('hash')),
                    'hash': draft.get('hash'), 'status': record.status or 'draft',
                    'manual_saved': bool(draft.get('manual_saved')), 'updated_at': draft.get('workspace_updated_at'),
                    'blockers': draft.get('blockers', []), 'template': draft.get('template_name'),
                    'recipients': [x.get('name', '已配置接收人') for x in (draft.get('approval_context') or {}).get('recipients', [])],
                    'readonly': bool(record.status in {'verified', 'submitted', 'submitting', 'unknown', 'mismatch'})}
        return saved or {'sections': {k: '' for k in SECTIONS}, 'revision': 0, 'status': 'empty',
                         'manual_saved': False, 'blockers': [], 'readonly': False, 'local_only': True}

    def view(self, kind, day):
        start, end = period_bounds(kind, date.fromisoformat(day))
        tasks = [x for x in self.repo.tasks() if start.astimezone(CHINA).date().isoformat() <= x['date'] < end.astimezone(CHINA).date().isoformat()]
        try:
            cfg = self.config()
            schedule = cfg.get('schedule') or {}
            enabled = bool(schedule.get('auto_submit_enabled'))
            paths = (cfg.get('codex') or {}).get('session_roots') or []
            available = bool(paths) and all(resolve_path(cfg, x).exists() for x in paths)
            automation = {'enabled': enabled, 'time': schedule.get('auto_daily_time') or schedule.get('daily_time'),
                          'timezone': 'Asia/Shanghai', 'engine': schedule.get('engine', 'extractive'),
                          'scheduler': '外部 WorkBuddy / Windows 计划任务',
                          'heartbeat': state.read_heartbeats(cfg)}
        except AppError:
            available, automation = False, {'enabled': False, 'time': None, 'timezone': 'Asia/Shanghai'}
        sync = self.repo.meta('sync') or {}
        source_states = [{'id': 'codex', 'status': 'available' if available else 'unavailable',
                          'message': '按已授权项目范围读取' if available else '未配置或源不可访问'},
                         {'id': 'workbuddy', 'status': 'unavailable', 'message': 'WorkBuddy 数据源尚未配置'}]
        indexed = False
        try:
            indexed = conversations_enabled(self.config())
            if indexed:
                health = self.conversation_index().sources()
                if health:
                    source_states = health
        except AppError:
            pass
        return {'tasks': tasks, 'report': self.report(kind, day), 'sync': sync, 'automation': automation,
                'sources': source_states, 'conversations_enabled': indexed}

    def sync(self, kind, day):
        if not self.lock.acquire(blocking=False):
            raise AppError('SYNC_BUSY', '同步或保存正在进行，请稍后重试。', 409)
        try:
            cfg = self.config()
            _, evidence = cli._collect(cfg, kind, day)
            active = set()
            for fact in evidence['facts']:
                key = hashlib.sha256((fact.get('source', '') + ':' + fact['evidence_id']).encode()).hexdigest()[:24]
                active.add(key)
                status = {'completed': 'implemented_unverified', 'committed': 'implemented_unverified',
                          'planned': 'planned', 'in_progress': 'in_progress'}.get(fact.get('status'), 'unknown')
                item = {'id': key, 'title': safe_text(fact.get('title') or fact.get('text', '').splitlines()[0])[:110],
                        'summary': safe_text(fact.get('text'))[:600], 'status': status,
                        'date': fact.get('date') or fact['time'][:10], 'time': fact.get('time'),
                        'project': Path(fact.get('project') or '未分类').name, 'source': fact['source'],
                        'session_id': fact.get('session_id') or fact.get('source', ''), 'evidence_id': fact['evidence_id']}
                locator = {k: fact.get(k) for k in ('source_path', 'source_line', 'role', 'channel', 'commit_hash', 'conversation_key', 'message_id')}
                locator['scope_key'] = scope_key(cfg)
                self.repo.upsert(item, locator)
            if evidence.get('retention', {}).get('action') != 'retain':
                start, end = period_bounds(kind, date.fromisoformat(day))
                first, last = start.astimezone(CHINA).date().isoformat(), end.astimezone(CHINA).date().isoformat()
                self.repo.remove([x['id'] for x in self.repo.tasks() if x['source'] != 'manual'
                                  and first <= x['date'] < last and x['id'] not in active])
            self.repo.meta('sync', {'at': datetime.now(CHINA).isoformat(), 'status': 'warning' if evidence.get('warnings') or evidence.get('retention', {}).get('action') == 'retain' else 'success',
                                    'count': len(evidence['facts']), 'warnings': [safe_text(x) for x in evidence.get('warnings', [])],
                                    'blockers': evidence.get('blockers', [])})
            return self.view(kind, day)
        finally:
            self.lock.release()

    def manual(self, payload):
        item = {**payload, 'id': uuid.uuid4().hex[:24], 'source': 'manual', 'session_id': 'manual',
                'time': datetime.now(CHINA).isoformat()}
        item['title'], item['summary'] = safe_text(item['title']), safe_text(item['summary'])
        self.repo.upsert(item, {})
        return item

    def evidence(self, key):
        item = self.repo.task(key)
        if not item:
            raise AppError('TASK_NOT_FOUND', '任务不存在。', 404)
        if item['source'] == 'manual':
            return {'messages': [{'role': 'manual', 'text': item['summary'], 'time': item['time']}], 'tools': []}
        loc = item['_locator']
        if loc.get('scope_key') != scope_key(self.config()):
            raise AppError('SOURCE_SCOPE_CHANGED', '数据源授权范围已变化，请重新同步。已保留历史摘要。', 403)
        if loc.get('conversation_key'):
            try:
                return {'messages': [self.conversation_index().evidence(loc['conversation_key'], loc['message_id'])], 'tools': []}
            except EvidenceUnavailable as exc:
                raise AppError('EVIDENCE_MISSING', str(exc), 410)
        try:
            path = Path(loc['source_path'])
            if item['source'] == 'codex':
                messages = []
                # A stable evidence id is checked before displaying the original message.
                with path.open(encoding='utf-8') as stream:
                    for index, line in enumerate(stream, 1):
                        if index != loc['source_line']:
                            continue
                        row = json.loads(line)
                        parsed = _canonical_message(row)
                        if parsed is None:
                            break
                        role, _, text = parsed
                        digest = hashlib.sha256((item['session_id'] + '\0' + role + '\0' + text).encode()).hexdigest()[:20]
                        # The source collector owns locator provenance; legacy fixtures can omit the digest.
                        if len(item['evidence_id']) == 20 and digest != item['evidence_id']:
                            break
                        messages.append({'role': role, 'text': safe_text(text), 'time': row.get('timestamp'), 'line': index})
                        break
                if not messages:
                    raise ValueError('locator changed')
                return {'messages': messages, 'tools': []}
            if not path.exists():
                raise OSError('source missing')
            return {'messages': [{'role': item['source'], 'text': item['summary'], 'time': item['time']}],
                    'tools': [], 'note': '补充来源摘要；原始对话仅适用于 Codex 会话。'}
        except (OSError, ValueError, TypeError, KeyError):
            raise AppError('EVIDENCE_MISSING', '原始证据不可访问或定位已变化，已保留任务摘要。', 410)

    def save(self, kind, day, revision, sections):
        with self.lock:
            path, draft = self._draft(kind, day)
            if draft:
                with file_lock(path):
                    return self._save_file(kind, day, revision, sections, path)
            current = self.report(kind, day)
            if current['revision'] != revision:
                raise AppError('DRAFT_CONFLICT', '草稿已更新，请刷新后合并你的修改。', 409)
            try:
                cfg = self.config()
            except AppError:
                cfg = {}
            if (cfg.get('templates') or {}).get(kind):
                # Bind a real template on the first save so the scheduler sees
                # the editor's version even if Generate was never clicked.
                bound = self.generate(kind, day, bind=True)
                path, _ = self._draft(kind, day)
                with file_lock(path):
                    return self._save_file(kind, day, bound['revision'], sections, path)
            body = {**current, 'sections': {**current['sections'], **sections}, 'revision': int(revision) + 1,
                    'manual_saved': True, 'status': 'draft', 'updated_at': datetime.now(CHINA).isoformat()}
            self.repo.put_report(f'{kind}-{day}', body)
            return body

    def _save_file(self, kind, day, revision, sections, path):
        current = self.report(kind, day)
        if current['readonly']:
            raise AppError('REPORT_FROZEN', '该报告已有提交记录，冻结版本不可修改。', 409)
        if current['revision'] != revision:
            raise AppError('DRAFT_CONFLICT', '草稿已更新，请刷新后合并你的修改。', 409)
        draft, _ = load_json_durable(path)
        sections = {**current['sections'], **sections}
        names = FIELDS[kind]
        for field in draft['fields']:
            for key, name in names.items():
                if key in sections and name == field['name']:
                    field['value'] = sections[key]
        # Additional workspace sections are explicitly mapped into the real summary field.
        summary = '\n\n'.join(f'{label}\n{sections[key]}' for key, label in
                              [('progress', '当前进展'), ('problems', '遇到的问题'), ('solutions', '解决措施')]
                              if sections.get(key))
        for field in draft['fields']:
            if field['name'] == names['summary']:
                field['value'] = summary
        draft.update(manual_saved=True, workspace_sections=sections, workspace_revision=uuid.uuid4().hex,
                     workspace_updated_at=datetime.now(CHINA).isoformat())
        draft['hash'] = draft_hash(draft)
        atomic_json(path, draft, backup=True)
        return self.report(kind, day)

    def generate(self, kind, day, *, bind=False):
        cfg = self.config()
        with self.lock:
            current = self.report(kind, day)
            if (current.get('manual_saved') and not (bind and not current.get('hash'))) or current.get('readonly'):
                raise AppError('MANUAL_DRAFT_PROTECTED', '已有人工草稿或提交记录，已保留原文。', 409)
            self.sync(kind, day)
            tasks = [x for x in self.view(kind, day)['tasks'] if x['selected']]
            facts = []
            for item in tasks:
                original = self.evidence(item['id'])['messages'][0]
                facts.append({'evidence_id': item.get('evidence_id', item['id']), 'source': item['source'],
                              'text': original['text'], 'role': original.get('role'),
                              'date': item['date'], 'time': item.get('time'), 'project': item.get('project'),
                              'session_id': item.get('session_id'),
                              'status': {'implemented_unverified': 'completed', 'verified_completed': 'completed'}.get(item['status'], item['status'])})
            template_cfg = cli._template_cfg(cfg, kind)
            template = cli._client(cfg).template(template_cfg['name'], template_cfg['id'])
            sync = self.repo.meta('sync') or {}
            evidence = {'facts': facts, 'warnings': sync.get('warnings', []), 'blockers': sync.get('blockers', [])}
            draft = build_draft(kind, day, template, evidence)
            cli._apply_weekly_carryover(cfg, kind, day, draft, evidence)
            draft.update(approval_context=cli._approval_context(cfg, kind, day), generation_engine='extractive',
                         workspace_revision=uuid.uuid4().hex, workspace_updated_at=datetime.now(CHINA).isoformat(),
                         workspace_generated=True)
            # Manual selection is durable and the unattended run uses this same draft.
            draft['manual_saved'] = True
            draft['hash'] = draft_hash(draft)
            path = cli._path(cfg, kind, day, 'drafts')
            with file_lock(path):
                # The background CLI may have changed the file while collection was running.
                _, live = self._draft(kind, day)
                if live and (live.get('manual_saved') or live.get('submission')):
                    raise AppError('DRAFT_CONFLICT', '后台草稿已更新，请刷新后重试。', 409)
                atomic_json(path, draft, backup=True)
            return self.report(kind, day)

    def history(self):
        cfg = self.config()
        root = resolve_path(cfg, cfg.get('data_dir', 'data')) / 'drafts'
        items = []
        for path in sorted(root.glob('*.json'), reverse=True)[:100]:
            try:
                draft, _ = load_json_durable(path)
                report = self.report(draft['kind'], draft['date'])
                items.append({'kind': draft['kind'], 'date': draft['date'], 'status': report['status'],
                              'revision': report['revision'], 'template': report.get('template'),
                              'updated_at': report.get('updated_at')})
            except (OSError, ValueError, KeyError):
                continue
        return items

    def submit(self, kind, day, expected_hash):
        cfg = self.config()
        with self.lock:
            path, draft = self._draft(kind, day)
            if not draft or draft.get('hash') != expected_hash:
                raise AppError('DRAFT_CONFLICT', '提交版本已变化，请重新预览。', 409)
            try:
                return cli._submit(cfg, path, expected_hash)
            except SubmissionBlocked:
                raise AppError('SUBMISSION_BLOCKED', '提交已阻止。请检查模板、接收人或历史回执；结果未知时请先核查。', 409)
