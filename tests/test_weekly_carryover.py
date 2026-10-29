"""Weekly work reuse preserves original dates and consumes only verified reports."""
import json
from pathlib import Path

import pytest

from lablog import cli
from lablog.dws import DwsBlocked
from lablog.periods import format_period_marker
from lablog.report import build_draft
from lablog.workflow import atomic_json, draft_hash, freeze_draft

DAY = '2026-10-16'
TEMPLATE = {'report_template_id': 'weekly-t', 'report_template_name': '学生周报', 'allowAddReceivers': 1,
            'report_template_fields': [{'field_name': '本周完成工作', 'field_sort': 0, 'field_type': 1},
                                      {'field_name': '本周工作总结', 'field_sort': 1, 'field_type': 1}]}


def config(tmp_path):
    return {'_base': str(tmp_path), 'data_dir': 'data', 'quality': {'weekly_carryover': {'enabled': True, 'target_chars': 200}},
            'dws': {'profile': 'test'}, 'identity': {'corp_id': 'corp', 'user_id': 'self', 'user_name': 'Self'},
            'recipients': [{'user_id': 'r', 'name': 'Recipient', 'corp_name': 'lab'}],
            'templates': {'weekly': {'id': 'weekly-t', 'name': '学生周报'}},
            'history_from': '2026-10-01', 'date_encoding': {'verified': True, 'format': 'iso-date'}}


def fact(ident, day, text):
    return {'evidence_id': ident, 'evidence_ids': [ident], 'source': 'codex', 'role': 'assistant',
            'status': 'completed', 'date': day, 'time': day + 'T02:00:00Z', 'text': text,
            'project': 'E:/Open Science', 'session_id': 'science'}


def prepare(tmp_path, monkeypatch, current=None, old=None, old_blockers=None):
    cfg = config(tmp_path)
    current = [] if current is None else current
    old = [fact('old', '2026-10-11', '已完成周末论文分类脚本测试。')] if old is None else old
    evidence = {'facts': current, 'warnings': [], 'blockers': []}
    calls = []
    def collect(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return Path(tmp_path) / 'unused.json', {'facts': old, 'warnings': [], 'blockers': old_blockers or []}
    monkeypatch.setattr(cli, '_collect', collect)
    draft = build_draft('weekly', DAY, TEMPLATE, evidence)
    result = cli._apply_weekly_carryover(cfg, 'weekly', DAY, draft, evidence)
    return cfg, draft, result, calls


def test_weekly_fills_short_body_with_previous_weekend_and_preserves_dates(tmp_path, monkeypatch):
    current = [fact('now', DAY, '已完成本周接口修复。')]
    cfg, draft, result, calls = prepare(tmp_path, monkeypatch, current)
    body = draft['fields'][0]['value']
    assert body.startswith(format_period_marker('weekly', DAY))
    assert body.index('本周接口修复') < body.index('周末论文分类')
    assert '2026-10-11' in body and '上周补充' in body
    assert not draft['blockers']
    assert result['filled_items']
    assert calls == [(('weekly', '2026-10-11'), {'persist': False})]
    assert set(draft['citations']['本周完成工作']) == {'now', 'old'}
    assert draft['weekly_pool']['target_chars'] == 200


def test_weekly_does_not_fill_when_current_body_is_long_enough(tmp_path, monkeypatch):
    _, draft, result, _ = prepare(tmp_path, monkeypatch, [fact('now', DAY, '已完成验证。' * 50)])
    assert '上周补充' not in draft['fields'][0]['value']
    assert result['filled_items'] == []


def test_weekly_can_use_real_old_work_when_current_week_has_no_completion(tmp_path, monkeypatch):
    _, draft, _, _ = prepare(tmp_path, monkeypatch)
    assert not draft['blockers']
    assert '上周补充' in draft['fields'][0]['value']


def test_weekly_keeps_source_failures_blocking_and_does_not_change_daily(tmp_path, monkeypatch):
    cfg, draft, _, _ = prepare(tmp_path, monkeypatch, old_blockers=['offline source'])
    assert 'offline source' in draft['blockers']
    daily = {'kind': 'daily', 'fields': [], 'blockers': []}
    before = json.dumps(daily)
    assert cli._apply_weekly_carryover(cfg, 'daily', DAY, daily, {'facts': []}) == {'status': 'disabled'}
    assert json.dumps(daily) == before


def test_missing_supplemental_source_does_not_erase_previous_candidates(tmp_path, monkeypatch):
    from lablog import weekly_pool
    cfg = config(tmp_path)
    weekly_pool.sync(cfg, DAY, [fact('old', '2026-10-11', '已完成应保留的历史工作。')])
    before = weekly_pool.load(cfg)
    monkeypatch.setattr(cli, '_collect', lambda *a, **k: (tmp_path / 'unused', {
        'facts': [], 'blockers': [], 'sources': [{'name': 'git', 'configured': True, 'reachable': False}]}))
    evidence = {'facts': []}
    draft = build_draft('weekly', DAY, TEMPLATE, evidence)
    result = cli._apply_weekly_carryover(cfg, 'weekly', DAY, draft, evidence)
    assert result['status'] == 'blocked'
    assert weekly_pool.load(cfg) == before


@pytest.mark.parametrize('status', ['verified', 'unknown'])
def test_existing_weekly_reports_prevent_reuse_of_used_or_uncertain_items(tmp_path, monkeypatch, status):
    cfg = config(tmp_path)
    old = fact('old', '2026-10-09', '已完成上周分类脚本验证。')
    saved = build_draft('weekly', '2026-10-09', TEMPLATE, {'facts': [old]})
    saved['approval_context'] = cli._approval_context(cfg, 'weekly', '2026-10-09')
    saved = freeze_draft(saved)
    saved['submission'] = {'status': status, 'report_id': 'old-report'}
    atomic_json(tmp_path / 'data/drafts/weekly-2026-10-09.json', saved)
    _, draft, result, _ = prepare(tmp_path, monkeypatch, [fact('now', DAY, '已完成本周接口修复。')], [old])
    assert result['filled_items'] == []
    assert '上周补充' not in draft['fields'][0]['value']


def test_manual_removal_consumes_only_items_still_present_in_verified_body(tmp_path, monkeypatch):
    from lablog import weekly_pool
    cfg, draft, _, _ = prepare(tmp_path, monkeypatch, [fact('now', DAY, '已完成本周接口修复。')])
    draft['fields'][0]['value'] = format_period_marker('weekly', DAY) + '\n' + draft['weekly_pool']['current_body']
    draft['manual_saved'] = True
    assert cli._record_weekly_pool_receipt(cfg, draft, 'edited-report') == 1
    selected = weekly_pool.select(cfg, DAY, 200)
    assert len(selected) == 1 and selected[0]['work_date'] == '2026-10-11'


def test_previous_week_collection_does_not_rewrite_its_evidence_file(tmp_path):
    cfg = config(tmp_path)
    cfg['codex'] = {'session_roots': []}
    previous = tmp_path / 'data/evidence/weekly-2026-10-11.json'
    previous.parent.mkdir(parents=True)
    previous.write_text('{"historical": true}', encoding='utf-8')
    stamp = previous.stat().st_mtime_ns
    _, evidence = cli._collect(cfg, 'weekly', '2026-10-11', persist=False)
    assert evidence['facts'] == []
    assert previous.read_text(encoding='utf-8') == '{"historical": true}'
    assert previous.stat().st_mtime_ns == stamp


def test_weekend_after_friday_report_can_fill_the_following_week(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    friday = fact('friday', '2026-10-16', '已完成工作日接口验证。')
    saved = build_draft('weekly', '2026-10-16', TEMPLATE, {'facts': [friday]})
    saved['approval_context'] = cli._approval_context(cfg, 'weekly', '2026-10-16')
    saved = freeze_draft(saved)
    saved['submission'] = {'status': 'verified', 'report_id': 'friday-report'}
    atomic_json(tmp_path / 'data/drafts/weekly-2026-10-16.json', saved)
    weekend = [fact('sat', '2026-10-17', '已完成周六分类验证。'), fact('sun', '2026-10-18', '已完成周日接口回归。')]
    def collect(cfg, kind, day, **kwargs):
        assert day == '2026-10-18' and kwargs == {'persist': False}
        return tmp_path / 'unused', {'facts': [friday, *weekend], 'warnings': [], 'blockers': []}
    monkeypatch.setattr(cli, '_collect', collect)
    evidence = {'facts': [fact('current', '2026-10-23', '已完成本周小修复。')]}
    draft = build_draft('weekly', '2026-10-23', TEMPLATE, evidence)
    cli._apply_weekly_carryover(cfg, 'weekly', '2026-10-23', draft, evidence)
    body = draft['fields'][0]['value']
    assert '2026-10-17' in body and '2026-10-18' in body
    assert '工作日接口验证' not in body  # Already included in the previous Friday's report.


def test_unsubmitted_edited_weekly_draft_does_not_claim_pool_items(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    old = fact('old', '2026-10-09', '已完成未提交的分类验证。')
    saved = build_draft('weekly', '2026-10-09', TEMPLATE, {'facts': [old]})
    saved['approval_context'] = cli._approval_context(cfg, 'weekly', '2026-10-09')
    saved = freeze_draft(saved)
    saved['fields'][0]['value'] += '\n未提交的人工修改'
    atomic_json(tmp_path / 'data/drafts/weekly-2026-10-09.json', saved)
    _, draft, result, _ = prepare(tmp_path, monkeypatch, old=[old])
    assert result['filled_items'] and '上周补充' in draft['fields'][0]['value']


def test_history_seeding_does_not_reconsume_a_removed_generated_supplement(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    current = [fact('now', DAY, '已完成本周主要接口修复。'), fact('extra', '2026-10-15', '已完成可候补的分类验证。')]
    monkeypatch.setattr(cli, '_collect', lambda *a, **k: (tmp_path / 'unused', {'facts': [], 'blockers': []}))
    draft = build_draft('weekly', DAY, TEMPLATE, {'facts': current},
                        ai_result={'completed': '已完成本周主要接口修复。 [证据:now]', 'summary': '', 'plan': '', 'citations': ['now']})
    cli._apply_weekly_carryover(cfg, 'weekly', DAY, draft, {'facts': current})
    assert draft['weekly_pool']['filled_items']
    draft['fields'][0]['value'] = format_period_marker('weekly', DAY) + '\n' + draft['weekly_pool']['current_body']
    draft.update(manual_saved=True, approval_context=cli._approval_context(cfg, 'weekly', DAY))
    draft = freeze_draft(draft)
    draft['submission'] = {'status': 'verified', 'report_id': 'edited-report'}
    cli._record_weekly_pool_receipt(cfg, draft, 'edited-report')
    atomic_json(tmp_path / 'data/drafts/weekly-2026-10-16.json', draft)
    monkeypatch.setattr(cli, '_collect', lambda *a, **k: (tmp_path / 'unused', {'facts': current, 'blockers': []}))
    evidence = {'facts': [fact('new', '2026-10-23', '已完成新一周接口检查。')]}
    new = build_draft('weekly', '2026-10-23', TEMPLATE, evidence)
    cli._apply_weekly_carryover(cfg, 'weekly', '2026-10-23', new, evidence)
    assert '可候补的分类验证' in new['fields'][0]['value']


@pytest.mark.parametrize('outcome', ['verified', 'timeout', 'readback-later'])
def test_fake_weekly_submit_marks_pool_only_after_verified_and_preserves_hash(tmp_path, monkeypatch, outcome):
    from lablog import weekly_pool
    cfg, draft, _, _ = prepare(tmp_path, monkeypatch)
    draft.update(approval_context=cli._approval_context(cfg, 'weekly', DAY))
    draft = freeze_draft(draft)
    path = Path(tmp_path) / 'data/drafts/weekly-2026-10-16.json'
    atomic_json(path, draft)
    class FakeDws:
        submitted = []
        fail_detail = outcome == 'readback-later'
        def auth(self): return {'corp_id': 'corp', 'user_id': 'self', 'user_name': 'Self'}
        def template(self, *args): return TEMPLATE
        def contact(self, ident): return {'orgUserName': 'Recipient', 'orgName': 'lab'}
        def outbox(self, *args): return []
        def submit(self, ident, contents, users):
            if outcome == 'timeout': raise DwsBlocked('timeout')
            self.submitted = contents
            return 'weekly-report'
        def detail(self, ident):
            if self.fail_detail: raise DwsBlocked('readback unavailable')
            return {'report_template_id': 'weekly-t', 'reportId': ident, 'contents': self.submitted}
    fake = FakeDws()
    monkeypatch.setattr(cli, '_client', lambda *args: fake)
    if outcome == 'verified':
        assert cli._submit(cfg, path, draft['hash'])['status'] == 'verified'
    else:
        with pytest.raises(DwsBlocked): cli._submit(cfg, path, draft['hash'])
        assert weekly_pool.select(cfg, '2026-10-16', 200)
        if outcome == 'readback-later':
            fake.fail_detail = False
            assert cli._reconcile(cfg, path)['status'] == 'verified'
    if outcome != 'timeout':
        assert weekly_pool.select(cfg, '2026-10-16', 200) == []
        stored = json.loads(path.read_text(encoding='utf-8'))
        assert draft_hash(stored) == stored['hash']
