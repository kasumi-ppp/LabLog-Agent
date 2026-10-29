import pytest

from lablog.ai import validate_ai_result
from lablog.report import build_draft


def result():
    return {'completed': '完成量化校准 [证据:c]', 'summary': '', 'plan': '', 'citations': ['c', 'a'],
            'extra_fields': {'本月考勤情况': '缺少明确考勤和请假记录 [证据:a]'}}


FACTS = [{'evidence_id': 'c', 'text': '完成量化校准', 'status': 'completed', 'categories': []},
         {'evidence_id': 'a', 'text': '缺少明确考勤和请假记录', 'status': 'unknown', 'categories': ['attendance']}]


def test_extra_fields_require_semantic_category_and_live_field_whitelist():
    ai = result()
    assert validate_ai_result(ai, FACTS, extra_fields={'本月考勤情况': 'attendance'}) == ai
    with pytest.raises(ValueError):
        validate_ai_result(ai, FACTS, extra_fields={})
    ai['extra_fields']['本月考勤情况'] = '正常出勤 [证据:c]'
    with pytest.raises(ValueError):
        validate_ai_result(ai, FACTS, extra_fields={'本月考勤情况': 'attendance'})


def test_extra_mapping_keeps_citations_and_checks_live_constraints():
    template = {'report_template_id': 'm', 'report_template_fields': [
        {'field_name': '本月工作内容', 'field_sort': 0, 'field_type': 1},
        {'field_name': '本月考勤情况', 'field_sort': 1, 'field_type': 1, 'required': True, 'minLength': 50}]}
    draft = build_draft('monthly', '2026-10-31', template, {'facts': FACTS}, ai_result=result())
    attendance = next(f for f in draft['fields'] if f['name'] == '本月考勤情况')
    assert attendance['value'] == '缺少明确考勤和请假记录'
    assert draft['citations']['本月考勤情况'] == ['a']
    assert any('minLength' in b for b in draft['blockers'])


def test_required_unmapped_template_field_blocks():
    template = {'report_template_id': 'm', 'report_template_fields': [
        {'field_name': '本月工作内容', 'field_sort': 0, 'field_type': 1},
        {'field_name': '新增必填字段', 'field_sort': 1, 'field_type': 1, 'isRequired': True}]}
    draft = build_draft('monthly', '2026-10-31', template, {'facts': FACTS})
    assert any('新增必填字段' in b for b in draft['blockers'])
