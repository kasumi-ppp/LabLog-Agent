"""Frozen, report-only evidence for weekly and monthly reports.

The outbox is creation/modification-time indexed. Query the configured history
in bounded windows, then select by the work dates in the actual report body.
Never fall back to sessions when a remote read is incomplete.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

from .config import resolve_path
from .periods import CHINA, parse_period_marker, period_labels
from .report import FIELDS, reader_text
from .sources import SECRET
from .workflow import SubmissionBlocked, draft_hash

EXCLUDED = re.compile(r'(?i)comfy[\s_-]*ui|galgame|tgga|官网素材|游戏素材|作品组|\bOP\b|备用\s*CG')
CATEGORY_PATTERNS = {
    'attendance': re.compile(r'考勤|请假|出勤|签到|迟到|早退|缺勤|日报.*(?:提交|缺失)'),
    'reading': re.compile(r'阅读|精读|培训|学习|读书'),
    'service': re.compile(r'公共事务|公共服务|服务性工作|实验室服务|团队管理|值日|志愿|组织组会'),
    'coordination': re.compile(r'(?:需|需要|请求|亟需).{0,8}(?:协调|帮助|支持)|阻塞|困难|待协助'),
}
EXTRA_CATEGORIES = {
    '需协调与帮助': 'coordination', '需帮助与支持': 'coordination',
    '考勤-日报-请假情况': 'attendance', '本月考勤情况': 'attendance',
    '自己团队管理和公共服务情况': 'service', '公共事务及其他服务性工作': 'service',
    '本月论文阅读-培训情况': 'reading',
}
MISSING_TEXT = {
    'attendance': '缺少明确考勤和请假记录，不能据此判断出勤情况。',
    'reading': '未找到明确的论文阅读或培训记录，相关情况待补充。',
    'service': '未找到明确的团队管理、公共事务或服务记录，相关情况待补充。',
    'coordination': '未找到明确的需协调或帮助记录，相关情况待补充。',
}


def digest(value):
    body = {k: v for k, v in value.items() if k != 'hash'}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def source_context(cfg):
    return {'profile': cfg.get('dws', {}).get('profile'), 'identity': cfg.get('identity'),
            'templates': cfg.get('templates'), 'history_from': cfg.get('history_from'),
            'draft_roots': cfg.get('periodic', {}).get('draft_roots', ['data', 'data-auto', 'data-periodic']),
            'exclude_paths': cfg.get('codex', {}).get('exclude_paths', []),
            'exclude_terms': cfg.get('periodic', {}).get('exclude_terms', [])}


def excluded(text, cfg=None):
    if EXCLUDED.search(str(text)):
        return True
    terms = ((cfg or {}).get('periodic') or {}).get('exclude_terms', [])
    paths = ((cfg or {}).get('codex') or {}).get('exclude_paths', [])
    normalized = str(text).replace('\\', '/').casefold()
    return any(str(t).replace('\\', '/').casefold() in normalized for t in [*terms, *paths] if t)


def _fields(report, local=False):
    items = report.get('fields') if local else (report.get('contents') or report.get('report_entry_contents')
              or report.get('reportContents') or report.get('report_content'))
    if not isinstance(items, list):
        raise SubmissionBlocked('upstream report fields unavailable')
    fields = {}
    for item in items:
        name = item.get('name') if local else item.get('key', item.get('field_name'))
        value = item.get('value') if local else item.get('content', item.get('value'))
        if not name or name in fields:
            raise SubmissionBlocked('ambiguous upstream fields')
        if isinstance(value, str):
            fields[name] = value
    return fields


def _period(report, fields, local=False):
    markers = set()
    for value in fields.values():
        found = parse_period_marker(value)
        if found:
            markers.add(found)
    if fields.get('工作日期'):
        day = date.fromisoformat(fields['工作日期'].strip()).isoformat()
        markers.add((day, day))
    for key in ('work_period', 'workPeriod', 'period', 'reportPeriod'):
        value = report.get(key)
        if isinstance(value, dict) and value.get('start') and value.get('end'):
            markers.add((str(value['start'])[:10], str(value['end'])[:10]))
    if report.get('startDate') and report.get('endDate'):
        markers.add((str(report['startDate'])[:10], str(report['endDate'])[:10]))
    if local:
        markers.add(period_labels(report['kind'], report['date']))
    if len(markers) != 1:
        raise SubmissionBlocked('upstream actual work period unavailable or conflicting')
    start, end = next(iter(markers))
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise SubmissionBlocked('invalid upstream work period')
    return start, end


def _time(value):
    if isinstance(value, (float, int)) or str(value).isdigit():
        seconds = float(value)
        return datetime.fromtimestamp(seconds / 1000 if seconds > 1e11 else seconds, CHINA)
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise SubmissionBlocked('upstream timestamp timezone missing')
    return parsed.astimezone(CHINA)


def _modified(report):
    times = [_time(report[k]) for k in ('modifyTime', 'modifiedTime', 'modified_at', 'updateTime',
             'gmtModified', 'createTime', 'createdTime', 'created_at', 'gmtCreate') if report.get(k) is not None]
    if not times:
        raise SubmissionBlocked('upstream version timestamp missing; cutoff cannot be verified')
    return max(times)


def _remote(cfg, client, source_kind, cutoff, target_period, same_batch_ids, queries):
    first = cfg.get('history_from')
    if not first or date.fromisoformat(first) > date.fromisoformat(target_period[0]):
        raise SubmissionBlocked('history_from does not cover source work period')
    current = datetime.combine(date.fromisoformat(first), datetime.min.time(), CHINA)
    finish = max(cutoff, datetime.now(CHINA))
    template = cfg['templates'][source_kind]
    listed = {}
    while current < finish:
        end = min(current + timedelta(days=19), finish)
        page = client.outbox(template['name'], current.isoformat(), end.isoformat())
        queries.append({'template': template, 'start': current.isoformat(), 'end': end.isoformat(),
                        'complete': True, 'listed_count': len(page)})
        for item in page:
            rid = item.get('reportId') or item.get('report_id') or item.get('report_Id')
            if not rid:
                raise SubmissionBlocked('outbox item has no stable report ID')
            listed[str(rid)] = item
        current = end
    results = []
    for rid, item in listed.items():
        detail = client.detail(rid)
        returned_id = detail.get('reportId') or detail.get('report_id') or detail.get('report_Id') or detail.get('id')
        if returned_id and str(returned_id) != rid:
            raise SubmissionBlocked('upstream report ID mismatch')
        template_id = detail.get('templateId') or detail.get('template_id') or detail.get('report_template_id')
        if template_id != template['id']:
            raise SubmissionBlocked('upstream template mismatch')
        author = detail.get('creatorUserId') or detail.get('userId') or detail.get('userid') or detail.get('creator_id')
        if author and author != cfg['identity']['user_id']:
            raise SubmissionBlocked('upstream author mismatch')
        fields = _fields(detail)
        period = _period(detail, fields)
        if period[1] < target_period[0] or period[0] > target_period[1]:
            continue
        if period[0] > cutoff.date().isoformat():
            continue
        if source_kind == 'daily' and period[0] != period[1]:
            raise SubmissionBlocked('daily source has non-daily period')
        if source_kind == 'weekly' and period_labels('weekly', period[0]) != period:
            raise SubmissionBlocked('weekly source has non-weekly period')
        modified = _modified({**item, **detail})
        # A weekly report just submitted by this same batch contains only facts
        # from the batch cutoff, so its newly assigned ID is an explicit exception.
        after_cutoff = modified > cutoff and rid not in same_batch_ids
        results.append({'kind': source_kind, 'period': list(period), 'fields': fields,
                        'origin': 'remote', 'report_id': rid, 'version': digest(detail),
                        'modified_at': modified.isoformat(), 'submitted': True,
                        'after_cutoff': after_cutoff, 'reference': 'reportId:' + rid})
    return results


def _local(cfg, source_kind, cutoff, target_period, warnings):
    results = []
    for root in source_context(cfg)['draft_roots']:
        directory = resolve_path(cfg, root) / 'drafts'
        if not directory.exists():
            continue
        for path in sorted(directory.glob(source_kind + '-*.json')):
            try:
                # Check filename before opening unrelated historical bodies.
                day = date.fromisoformat(path.stem[len(source_kind) + 1:])
                period = period_labels(source_kind, day)
                if period[1] < target_period[0] or period[0] > target_period[1]:
                    continue
                if period[0] > cutoff.date().isoformat():
                    continue
                report = json.loads(path.read_text(encoding='utf-8'))
                if report.get('hash') != draft_hash(report) or report.get('blockers'):
                    raise ValueError('unvalidated draft')
                ctx = report.get('approval_context', {})
                if (ctx.get('profile') != cfg['dws']['profile'] or ctx.get('identity') != cfg['identity']
                        or report.get('template_id') != cfg['templates'][source_kind]['id']
                        or report.get('kind') != source_kind or report.get('date') != day.isoformat()):
                    raise ValueError('draft identity/template/date mismatch')
                status = report.get('submission', {}).get('status')
                if status in {'unknown', 'submitting', 'mismatch'}:
                    raise ValueError('draft submission outcome unresolved')
                modified = datetime.fromtimestamp(path.stat().st_mtime, CHINA)
                if modified > cutoff:
                    continue
                fields = _fields(report, local=True)
                period = _period(report, fields, local=True)
                results.append({'kind': source_kind, 'period': list(period), 'fields': fields,
                                'origin': 'draft', 'path': str(path.resolve()), 'version': report['hash'],
                                'modified_at': modified.isoformat(), 'submitted': False,
                                'local_submission_status': status or 'not_submitted',
                                'reference': str(path.resolve())})
            except (OSError, ValueError, KeyError, SubmissionBlocked) as exc:
                warnings.append(f'ignored local draft {path.name}: {exc}')
    return results


def _fact_key(text):
    text = re.sub(r'\d{4}-\d{2}-\d{2}', '', reader_text(text))
    return re.sub(r'[\W_]+', '', text).casefold()


def collect_reports(cfg, client, kind, day, cutoff, *, same_batch_ids=()):
    if kind not in {'weekly', 'monthly'} or cutoff.tzinfo is None:
        raise SubmissionBlocked('invalid report source request')
    cutoff = cutoff.astimezone(CHINA)
    period = period_labels(kind, day)
    warnings, blockers, candidates, queries = [], [], [], []
    for upstream_kind in (('daily',) if kind == 'weekly' else ('daily', 'weekly')):
        candidates.extend(_remote(cfg, client, upstream_kind, cutoff, period, set(same_batch_ids), queries))
        candidates.extend(_local(cfg, upstream_kind, cutoff, period, warnings))
    selected = {}
    for source in candidates:
        key = (source['kind'], *source['period'])
        rank = (source['origin'] == 'remote', source['modified_at'])
        prior = selected.get(key)
        if prior and rank == prior[0] and source['version'] != prior[1]['version']:
            raise SubmissionBlocked('conflicting source versions for the same work period')
        if not prior or rank > prior[0]:
            selected[key] = (rank, source)
    sources, rejected_sources, facts, by_text = [], [], [], {}
    # Daily facts precede weekly summaries, so repeated accomplishments keep
    # their dated daily origin. All merged provenance remains in origins[].
    for _, source in sorted(selected.values(), key=lambda entry: (entry[1]['kind'] != 'daily', entry[1]['period'])):
        manifest = {k: v for k, v in source.items() if k not in {'fields', 'after_cutoff'}}
        if source.get('after_cutoff'):
            rejected_sources.append(manifest)
            blockers.append('upstream report changed after task cutoff; previous version unavailable')
            continue
        sources.append(manifest)
        names = FIELDS[source['kind']]
        for field_name, value in source['fields'].items():
            if field_name not in {names['completed'], names['summary'], names['plan'], names['coordination'], *EXTRA_CATEGORIES}:
                continue
            value = re.sub(r'统计周期\s*[：:]\s*\d{4}-\d{2}-\d{2}\s*(?:至|到|—|–|\.\.)\s*\d{4}-\d{2}-\d{2}', '', value)
            field_status = 'completed' if field_name == names['completed'] else ('planned' if field_name == names['plan'] else 'observed')
            for snippet in re.split(r'[\n。；]+', reader_text(value)):
                text = snippet.strip(' \t-*•#>')
                if not text or excluded(text, cfg):
                    continue
                explicit_dates = re.findall(r'\d{4}-\d{2}-\d{2}', text)
                cross_month = source['period'][0] < period[0] or source['period'][1] > period[1]
                if cross_month and (not explicit_dates or any(not period[0] <= d <= period[1] for d in explicit_dates)):
                    warnings.append('omitted cross-month weekly item without unambiguous target-month dates')
                    continue
                if any(d > cutoff.date().isoformat() for d in explicit_dates) and field_status != 'planned':
                    continue
                text = SECRET.sub('[REDACTED]', text)
                status = field_status
                if status == 'completed':
                    if re.search(r'(?i)^(?:计划(?!书)|打算|准备|待办|todo|next\b)|(?:明天|下周|下月)(?:将|继续|准备|计划)', text):
                        status = 'planned'
                    elif re.search(r'正在|进行中|尚未|未完成|未能|没有完成|计划未', text):
                        status = 'observed'
                key = (_fact_key(text), status)
                origin = {**manifest, 'field': field_name}
                if key in by_text:
                    by_text[key]['origins'].append(origin)
                    continue
                categories = [cat for cat, pattern in CATEGORY_PATTERNS.items() if pattern.search(text)]
                if field_name in EXTRA_CATEGORIES:
                    categories.append(EXTRA_CATEGORIES[field_name])
                missing_record = bool(re.search(r'缺少明确|未找到明确|相关情况待补充|不能据此判断|日志数量不代表|草稿不计为已提交日报|本次来源中已提交日报覆盖|有效本地草稿补入', text))
                if missing_record:
                    status = 'unknown'
                fact = {'evidence_id': 'report-' + hashlib.sha256((str(key) + source['version']).encode()).hexdigest()[:16],
                        'source': 'upstream_report', 'text': text, 'status': status,
                        'time': (min(explicit_dates) if cross_month else source['period'][0]) + 'T00:00:00+08:00',
                        'categories': sorted(set(categories)),
                        'origins': [origin], 'missing_record': missing_record}
                facts.append(fact)
                by_text[key] = fact
    coverage = {'submitted_daily_dates': sorted(s['period'][0] for s in sources if s['kind'] == 'daily' and s['submitted']),
                'draft_daily_dates': sorted(s['period'][0] for s in sources if s['kind'] == 'daily' and not s['submitted'])}
    for category, text in MISSING_TEXT.items():
        if not any(category in f['categories'] and f['status'] != 'unknown' for f in facts):
            facts.append({'evidence_id': 'missing-' + category, 'source': 'source_coverage', 'status': 'unknown',
                          'category': category, 'categories': [category], 'text': text})
    submitted_dates = coverage['submitted_daily_dates']
    draft_dates = coverage['draft_daily_dates']
    facts.append({'evidence_id': 'coverage-attendance', 'source': 'source_coverage', 'status': 'observed',
                  'categories': ['attendance'],
                  'text': f"本次来源中已提交日报覆盖{len(submitted_dates)}个工作日期（{'、'.join(submitted_dates) or '未找到'}）；"
                          f"有效本地草稿补入{len(draft_dates)}个工作日期（{'、'.join(draft_dates) or '未补入'}），"
                          '草稿不计为已提交日报，日志数量不代表实际出勤。'})
    if not any(f['status'] == 'completed' for f in facts):
        blockers.append('no sufficient dated completion evidence in upstream reports')
    snapshot = {'schema': 1, 'kind': kind, 'date': day, 'period': list(period), 'cutoff': cutoff.isoformat(),
                'context': source_context(cfg), 'complete': True, 'queries': queries, 'sources': sources,
                'rejected_sources': rejected_sources, 'facts': facts,
                'coverage': coverage, 'warnings': sorted(set(warnings)), 'blockers': blockers}
    snapshot['hash'] = digest(snapshot)
    return snapshot


def verify_snapshot(snapshot, cfg, kind, day):
    if (snapshot.get('hash') != digest(snapshot) or snapshot.get('schema') != 1
            or snapshot.get('context') != source_context(cfg) or snapshot.get('kind') != kind
            or snapshot.get('date') != day or snapshot.get('period') != list(period_labels(kind, day))
            or snapshot.get('complete') is not True):
        raise SubmissionBlocked('report source snapshot invalid, modified or context changed')
    _time(snapshot['cutoff'])
    return True
