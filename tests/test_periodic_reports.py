import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lablog.periodic import due_kinds
from lablog.report_sources import collect_reports, verify_snapshot
from lablog.workflow import SubmissionBlocked, atomic_json, freeze_draft
from lablog.periods import format_period_marker

CHINA = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 11, 1, 22, tzinfo=CHINA)


def config(tmp_path):
    return {'_base': str(tmp_path), 'data_dir': 'data-periodic', 'history_from': '2026-10-01',
            'dws': {'profile': 'lab'}, 'identity': {'corp_id': 'c', 'user_id': 'u'},
            'templates': {k: {'id': k, 'name': k} for k in ('daily', 'weekly', 'monthly')},
            'periodic': {'draft_roots': ['data', 'data-auto', 'data-periodic']}}


def remote(rid, kind, day, text, modified=None):
    names = {'daily': '今日完成', 'weekly': '本周完成工作'}
    return {'reportId': rid, 'templateId': kind, 'creatorUserId': 'u',
            'modifyTime': (modified or NOW - timedelta(hours=1)).isoformat(),
            'contents': [{'key': names[kind], 'content': format_period_marker(kind, day) + '\n' + text}]}


class Client:
    def __init__(self, reports=()):
        self.reports = list(reports)
        self.windows = []

    def outbox(self, name, start, end):
        self.windows.append((datetime.fromisoformat(start), datetime.fromisoformat(end)))
        return [{'reportId': r['reportId']} for r in self.reports if r['templateId'] == name]

    def detail(self, rid):
        return next(r for r in self.reports if r['reportId'] == rid)


@pytest.mark.parametrize('day,expected', [
    ('2026-10-28', []), ('2026-11-01', ['weekly']), ('2026-10-31', ['monthly']),
    ('2026-05-31', ['weekly', 'monthly']), ('2027-02-28', ['weekly', 'monthly']),
    ('2028-02-29', ['monthly']), ('2026-04-30', ['monthly']), ('2026-12-31', ['monthly'])])
def test_calendar(day, expected):
    assert due_kinds(day) == expected


def local(cfg, kind, day, text, *, root='data', blockers=()):
    draft = freeze_draft({'kind': kind, 'date': day, 'template_id': kind,
                         'approval_context': {'profile': 'lab', 'identity': cfg['identity']},
                         'fields': [{'name': '今日完成' if kind == 'daily' else '本周完成工作',
                                     'type': 1, 'sort': 0, 'value': format_period_marker(kind, day) + '\n' + text}],
                         'blockers': list(blockers)})
    path = __import__('pathlib').Path(cfg['_base']) / root / 'drafts' / f'{kind}-{day}.json'
    atomic_json(path, draft)
    return path


def test_remote_wins_and_valid_draft_fills_gap(tmp_path):
    cfg = config(tmp_path)
    local(cfg, 'daily', '2026-10-30', '旧版本完成解析器')
    local(cfg, 'daily', '2026-10-31', '完成校准流程')
    local(cfg, 'daily', '2026-10-29', '无效草稿', blockers=['failed'])
    client = Client([remote('r1', 'daily', '2026-10-30', '完成解析器修复')])
    snap = collect_reports(cfg, client, 'weekly', '2026-11-01', NOW)
    assert len(snap['sources']) == 2
    assert snap['coverage']['submitted_daily_dates'] == ['2026-10-30']
    assert snap['coverage']['draft_daily_dates'] == ['2026-10-31']
    assert '旧版本' not in str(snap['facts'])
    assert all(end - start <= timedelta(days=20) for start, end in client.windows)
    assert verify_snapshot(snap, cfg, 'weekly', '2026-11-01')
    snap['facts'][0]['text'] = '篡改内容'
    with pytest.raises(SubmissionBlocked, match='snapshot'):
        verify_snapshot(snap, cfg, 'weekly', '2026-11-01')


def test_cross_month_week_uses_only_explicit_target_dates(tmp_path):
    cfg = config(tmp_path)
    client = Client([remote('d', 'daily', '2026-10-31', '完成解析器修复'),
                     remote('w', 'weekly', '2026-11-01',
                            '2026-10-31 完成解析器修复。2026-11-01 完成新任务。未标日期的阶段总结')])
    snap = collect_reports(cfg, client, 'monthly', '2026-10-31', NOW)
    texts = str(snap['facts'])
    assert '新任务' not in texts and '未标日期' not in texts
    assert sum('完成解析器修复' in f['text'] for f in snap['facts']) == 1


def test_residual_excluded_projects_and_unknown_records(tmp_path):
    cfg = config(tmp_path)
    client = Client([remote('r', 'daily', '2026-10-30',
                            '完成量化校准。完成ComfyUI工作流。Galgame News 官网素材采集。完成论文公式复核')])
    snap = collect_reports(cfg, client, 'weekly', '2026-11-01', NOW)
    texts = str(snap['facts'])
    assert 'ComfyUI' not in texts and 'Galgame' not in texts
    assert '量化校准' in texts and '论文公式' in texts
    missing = [f for f in snap['facts'] if f.get('category') == 'attendance']
    assert missing and all(f['status'] == 'unknown' for f in missing)
    assert '正常出勤' not in str(missing)


def test_incomplete_or_ambiguous_remote_blocks_instead_of_draft_fallback(tmp_path):
    cfg = config(tmp_path)
    local(cfg, 'daily', '2026-10-30', '完成解析器')
    client = Client([{'reportId': 'bad', 'templateId': 'daily', 'contents': []}])
    with pytest.raises(SubmissionBlocked):
        collect_reports(cfg, client, 'weekly', '2026-11-01', NOW)


def test_future_modified_report_is_not_used(tmp_path):
    snap = collect_reports(config(tmp_path), Client([remote('r', 'daily', '2026-10-30',
                           '完成解析器', NOW + timedelta(minutes=1))]), 'weekly', '2026-11-01', NOW)
    assert not snap['sources']
    assert snap['blockers']


def test_snapshot_bound_to_identity_and_period(tmp_path):
    cfg = config(tmp_path)
    snap = collect_reports(cfg, Client(), 'weekly', '2026-11-01', NOW)
    changed = copy.deepcopy(cfg)
    changed['identity']['user_id'] = 'another'
    with pytest.raises(SubmissionBlocked):
        verify_snapshot(snap, changed, 'weekly', '2026-11-01')


def test_period_marker_on_same_line_does_not_remove_work_or_promote_plans(tmp_path):
    report = remote('r', 'daily', '2026-10-30', '')
    report['contents'][0]['content'] = '统计周期：2026-10-30 至 2026-10-30 完成量化校准。计划明天复核公式。正在排查显存'
    snap = collect_reports(config(tmp_path), Client([report]), 'weekly', '2026-11-01', NOW)
    assert any(f['status'] == 'completed' and '量化校准' in f['text'] for f in snap['facts'])
    assert not any(f['status'] == 'completed' and ('计划' in f['text'] or '正在' in f['text']) for f in snap['facts'])


def test_smoke_cannot_use_future_work_dates_even_if_draft_exists(tmp_path):
    cfg = config(tmp_path)
    local(cfg, 'daily', '2026-11-01', '完成未来任务')
    earlier = datetime(2026, 10, 28, 22, tzinfo=CHINA)
    snap = collect_reports(cfg, Client(), 'weekly', '2026-10-28', earlier)
    assert not snap['coverage']['draft_daily_dates']

