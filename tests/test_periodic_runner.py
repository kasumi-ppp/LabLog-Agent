import copy
import json
from datetime import datetime, timedelta

import pytest

from lablog import periodic, state
from lablog.periods import CHINA
from lablog.workflow import SubmissionBlocked, atomic_json
from lablog.dws import DwsBlocked
from test_periodic_reports import Client, config, local, remote


FIXED_RECIPIENTS = [{'name': '固定收件人', 'user_id': 'teacher'}]


def declare_fixed_recipients(cfg):
    """weekly/monthly declare a template-fixed receiver list; _guard requires it
    to match cfg['recipients'] exactly."""
    cfg['recipients'] = copy.deepcopy(FIXED_RECIPIENTS)
    for kind in ('weekly', 'monthly'):
        cfg['templates'][kind]['fixed_recipients'] = copy.deepcopy(FIXED_RECIPIENTS)
    return cfg


def configured(tmp_path):
    cfg = config(tmp_path)
    declare_fixed_recipients(cfg)
    return periodic.periodic_config(cfg)


def template(kind):
    names = {'daily': ['今日完成', '心得体会', '明日计划', '工作日期'],
             'weekly': ['本周完成工作', '本周工作总结', '下周工作计划', '考勤-日报-请假情况'],
             'monthly': ['本月工作内容', '本月工作总结', '下月工作计划', '本月考勤情况']}
    return {'report_template_id': kind, 'report_template_name': kind, 'allowAddReceivers': 1,
            'report_template_fields': [{'field_name': n, 'field_type': 1, 'field_sort': i} for i, n in enumerate(names[kind])]}


def fake_client(monkeypatch, cfg, now):
    client = Client([remote('r', 'daily', now.date().isoformat(), '完成量化校准', now - timedelta(hours=1))])
    client.auth = lambda: {**cfg['identity'], 'authenticated': True}
    client.template = lambda name, tid: template(tid)
    client.contact = lambda uid: {'orgUserName': '固定收件人', 'orgName': cfg['identity'].get('corp_name')}
    client.submit = lambda *args, **kwargs: {'dry_run': True, 'executed': False}
    monkeypatch.setattr(periodic.cli, '_client', lambda cfg: client)
    return client


def assisted(job):
    snap = json.loads(__import__('pathlib').Path(job['snapshot']).read_text(encoding='utf-8'))
    fact = next(f for f in snap['facts'] if f['status'] == 'completed')
    return {'completed': f"完成量化校准 [证据:{fact['evidence_id']}]", 'summary': '', 'plan': '',
            'citations': [fact['evidence_id']]}


def test_periodic_config_preserves_daily_and_is_disabled(tmp_path):
    cfg = config(tmp_path)
    declare_fixed_recipients(cfg)
    cfg['schedule'] = {'daily_time': '21:00'}
    original = copy.deepcopy(cfg)
    result = periodic.periodic_config(cfg)
    assert cfg == original
    assert result['periodic']['enabled'] is False
    assert result['notify']['enabled'] is False
    assert result['data_dir'] == 'data-periodic'
    assert result['periodic']['authorization']['recipients'] == cfg['recipients']


def test_offday_exits_and_gap_reports_missed_without_backfill(tmp_path):
    cfg = configured(tmp_path)
    assert periodic.create_batch(cfg, datetime(2026, 10, 24, 22, tzinfo=CHINA))['status'] == 'not_due'
    outcome = periodic.create_batch(cfg, datetime(2026, 10, 26, 22, tzinfo=CHINA))
    assert outcome['status'] == 'not_due'
    assert outcome['missed'] == [{'date': '2026-10-25', 'kinds': ['weekly']}]
    assert outcome['notify_workbuddy'] is True


def test_month_end_sunday_order_and_duplicate_batch(tmp_path):
    cfg = configured(tmp_path)
    now = datetime(2026, 5, 31, 22, tzinfo=CHINA)
    first = periodic.create_batch(cfg, now)
    second = periodic.create_batch(cfg, now + timedelta(minutes=2))
    assert first['kinds'] == ['weekly', 'monthly']
    assert first['batch'] == second['batch']
    assert first['cutoff'] == second['cutoff']
    with pytest.raises(SubmissionBlocked, match='weekly'):
        periodic.prepare(cfg, first['batch'], 'monthly', now=now)


def test_smoke_generates_from_frozen_snapshot_and_cannot_send(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    now = datetime(2026, 10, 28, 22, tzinfo=CHINA)
    fake_client(monkeypatch, cfg, now)
    batch = periodic.create_batch(cfg, now, smoke=True)
    job = periodic.prepare(cfg, batch['batch'], 'weekly', now=now)
    ai = assisted(job)
    ai_path = tmp_path / 'ai.json'
    atomic_json(ai_path, ai)
    # Collection is never repeated between WorkBuddy's generation and preflight.
    monkeypatch.setattr(periodic, 'collect_reports', lambda *a, **kw: pytest.fail('snapshot recollected'))
    monkeypatch.setattr(periodic.cli, '_preview', lambda *args: {'duplicate_check': {'status': 'none_found'}, 'preview_markdown': 'preview.md'})
    monkeypatch.setattr(periodic.cli, '_preflight', lambda *a, **kw: (periodic.cli._client(cfg), template('weekly'), ['teacher'], [{}]))
    out = periodic.finish(cfg, job['job'], ai_path, now=now)
    assert out['status'] == 'ready' and out['submitted'] is False
    draft = json.loads(__import__('pathlib').Path(out['draft']).read_text(encoding='utf-8'))
    assert draft['source_snapshot']['hash'] == job['snapshot_hash']
    assert any('缺少明确考勤' in f['value'] for f in draft['fields'])
    with pytest.raises(SubmissionBlocked, match='smoke'):
        periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)


def test_stale_batch_and_changed_authorization_block(tmp_path):
    cfg = configured(tmp_path)
    now = datetime(2026, 11, 1, 22, tzinfo=CHINA)
    batch = periodic.create_batch(cfg, now)
    with pytest.raises(SubmissionBlocked, match='date'):
        periodic.prepare(cfg, batch['batch'], 'weekly', now=now + timedelta(days=1))
    cfg['recipients'][0]['user_id'] = 'other'
    with pytest.raises(SubmissionBlocked, match='authorization'):
        periodic.create_batch(cfg, now)


@pytest.mark.parametrize('root,status,legacy_template', [
    ('data', 'unknown', False),
    ('data-auto', 'submitting', True),
    ('data-auto', 'mismatch', False),
])
def test_unresolved_attempt_in_legacy_data_root_blocks_new_cycle(
        tmp_path, root, status, legacy_template):
    cfg = configured(tmp_path)
    cfg['periodic']['enabled'] = True
    now = datetime(2026, 11, 1, 22, tzinfo=CHINA)
    batch = periodic.create_batch(cfg, now)
    scoped = copy.deepcopy(cfg)
    scoped['data_dir'] = root
    template_id = 'legacy-weekly' if legacy_template else cfg['templates']['weekly']['id']
    locator = {'kind': 'weekly', 'date': now.date().isoformat(), 'template_id': template_id,
               'approval_context': periodic.cli._approval_context(
                   cfg, 'weekly', now.date().isoformat())}
    draft_path = (tmp_path / root / 'drafts' /
                  ('weekly-' + now.date().isoformat() + '.json'))
    state.write_attempt(
        periodic.cli._state_path(scoped, locator),
        status=status,
        phase=(state.PHASE_READBACK_MISMATCH
               if status == 'mismatch' else state.PHASE_REQUEST_PENDING),
        draft_path=draft_path,
        template_id=template_id)
    with pytest.raises((SubmissionBlocked, DwsBlocked), match='reconcile|reconciliation'):
        periodic.prepare(cfg, batch['batch'], 'weekly', now=now)


@pytest.mark.parametrize('kind,day,old_day', [
    ('weekly', '2026-11-01', '2026-10-28'),
    ('monthly', '2026-10-31', '2026-10-13'),
])
def test_legacy_attempt_on_another_date_in_same_period_blocks(tmp_path, kind, day, old_day):
    cfg = configured(tmp_path)
    scoped = copy.deepcopy(cfg)
    scoped['data_dir'] = 'data-auto'
    locator = {'kind': kind, 'date': old_day, 'template_id': 'legacy-template',
               'approval_context': periodic.cli._approval_context(cfg, kind, old_day)}
    state.write_attempt(
        periodic.cli._state_path(scoped, locator), status='unknown',
        phase=state.PHASE_REQUEST_PENDING,
        draft_path=tmp_path / 'data-auto' / 'drafts' / f'{kind}-{old_day}.json')
    with pytest.raises(SubmissionBlocked, match='reconciliation'):
        periodic._existing(cfg, kind, day)


class SubmissionClient(Client):
    def __init__(self, cfg, now, outcome='verified'):
        super().__init__([remote('daily-source', 'daily', now.date().isoformat(), '完成量化校准', now - timedelta(hours=1))])
        self.cfg, self.now, self.outcome, self.writes = cfg, now, outcome, []

    def auth(self):
        return self.cfg['identity']

    def template(self, name, tid):
        return template(tid)

    def contact(self, uid):
        return {'orgUserName': '固定收件人', 'orgName': self.cfg['identity'].get('corp_name')}

    def submit(self, tid, contents, users, *, dry_run=False):
        if dry_run:
            return {'dry_run': True, 'executed': False}
        self.writes.append(tid)
        if self.outcome == 'timeout':
            raise DwsBlocked('DWS timeout; outcome unknown')
        rid = tid + '-submitted'
        self.reports.append({'reportId': rid, 'templateId': tid, 'modifyTime': (self.now + timedelta(minutes=1)).isoformat(),
                             'contents': copy.deepcopy(contents)})
        if self.outcome == 'mismatch':
            self.reports[-1]['contents'][0]['content'] = 'wrong body'
        return rid


def production_job(tmp_path, monkeypatch, outcome='verified'):
    cfg = configured(tmp_path)
    cfg['periodic']['enabled'] = True
    now = datetime(2026, 11, 1, 22, tzinfo=CHINA)
    client = SubmissionClient(cfg, now, outcome)
    monkeypatch.setattr(periodic.cli, '_client', lambda cfg: client)
    batch = periodic.create_batch(cfg, now)
    job = periodic.prepare(cfg, batch['batch'], 'weekly', now=now)
    ai_path = tmp_path / 'ai.json'
    atomic_json(ai_path, assisted(job))
    return cfg, now, client, batch, job, ai_path


def test_actual_submission_chain_repeated_trigger_writes_once(tmp_path, monkeypatch):
    cfg, now, client, batch, job, ai_path = production_job(tmp_path, monkeypatch)
    result = periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
    assert result['status'] == 'verified' and result['submitted'] is True
    repeat = periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
    assert repeat['status'] == 'already_submitted'
    assert periodic.prepare(cfg, batch['batch'], 'weekly', now=now)['status'] == 'already_submitted'
    assert client.writes == ['weekly']


@pytest.mark.parametrize('outcome', ['timeout', 'mismatch'])
def test_unknown_and_mismatch_never_write_again(tmp_path, monkeypatch, outcome):
    cfg, now, client, batch, job, ai_path = production_job(tmp_path, monkeypatch, outcome)
    with pytest.raises((DwsBlocked, SubmissionBlocked)):
        periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
    with pytest.raises(SubmissionBlocked):
        periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
    assert client.writes == ['weekly']
    draft_path = periodic.cli._path(cfg, 'weekly', now.date().isoformat(), 'drafts')
    assert json.loads(draft_path.read_text(encoding='utf-8'))['submission']['status'] == (
        'unknown' if outcome == 'timeout' else 'mismatch')


def test_month_end_sunday_submits_each_once_and_uses_same_cutoff_weekly(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    cfg['periodic']['enabled'] = True
    now = datetime(2027, 1, 31, 22, tzinfo=CHINA)
    client = SubmissionClient(cfg, now)
    monkeypatch.setattr(periodic.cli, '_client', lambda cfg: client)
    batch = periodic.create_batch(cfg, now)
    for kind in ('weekly', 'monthly'):
        job = periodic.prepare(cfg, batch['batch'], kind, now=now)
        ai_path = tmp_path / (kind + '.json')
        atomic_json(ai_path, assisted(job))
        out = periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
        assert out['status'] == 'verified'
        periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
        if kind == 'monthly':
            snap = json.loads(__import__('pathlib').Path(job['snapshot']).read_text(encoding='utf-8'))
            assert any(s.get('report_id') == 'weekly-submitted' for s in snap['sources'])
            assert snap['cutoff'] == batch['cutoff']
    assert client.writes == ['weekly', 'monthly']


def test_changed_template_and_disabled_submission_stop(tmp_path, monkeypatch):
    cfg, now, client, batch, job, ai_path = production_job(tmp_path, monkeypatch)
    cfg['periodic']['enabled'] = False
    with pytest.raises(SubmissionBlocked, match='disabled'):
        periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
    cfg['periodic']['enabled'] = True
    original_template = client.template
    client.template = lambda *args: {**original_template(*args), 'lastModifiedTime': 999}
    with pytest.raises(SubmissionBlocked, match='drift'):
        periodic.finish(cfg, job['job'], ai_path, submit=True, now=now)
    assert not client.writes
