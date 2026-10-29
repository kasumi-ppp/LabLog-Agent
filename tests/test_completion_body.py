"""Evidence presence cannot make a blank rendered completion field sendable."""
import pytest

from lablog import cli
from lablog.report import build_draft
from lablog.workflow import SubmissionBlocked, freeze_draft


@pytest.mark.parametrize('completed', ['', '   ', '- [证据:c]', '统计周期：2026-10-16 至 2026-10-16'])
def test_assisted_empty_completion_is_blocked_after_rendering(completed):
    template = {'report_template_fields': [{'field_name': '今日完成', 'field_sort': 0, 'field_type': 1},
                                          {'field_name': '工作日期', 'field_sort': 1, 'field_type': 5}]}
    evidence = {'facts': [{'evidence_id': 'c', 'source': 'codex', 'status': 'completed', 'text': '已修复分类脚本'}]}
    draft = build_draft('daily', '2026-10-16', template, evidence,
                        ai_result={'completed': completed, 'summary': '正在核对工作记录 [证据:c]', 'plan': '', 'citations': ['c']})
    assert 'empty completion body' in draft['blockers']


@pytest.mark.parametrize('manual_saved', [False, True])
def test_existing_marker_only_draft_is_blocked_before_any_remote_call(monkeypatch, manual_saved):
    draft = freeze_draft({'kind': 'daily', 'date': '2026-10-16', 'blockers': [], 'manual_saved': manual_saved,
                          'fields': [{'name': '今日完成', 'value': '统计周期：2026-10-16 至 2026-10-16\n  '}]})
    monkeypatch.setattr(cli, '_client', lambda *args: pytest.fail('empty report must be blocked before remote access'))
    with pytest.raises(SubmissionBlocked, match='completion body'):
        cli._preflight({}, draft)
