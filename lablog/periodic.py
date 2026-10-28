"""WorkBuddy's local prepare -> assisted JSON -> validate/submit protocol.

No timer, model call, notification channel or direct submit implementation lives
here. The timer and prose belong to WorkBuddy; cli._submit owns the remote write.
"""
from __future__ import annotations

import argparse
import calendar
import copy
import json
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import cli, state
from .ai import validate_ai_result
from .config import load_config, resolve_path
from .dws import DwsBlocked, template_fields, template_fingerprint
from .periods import CHINA, period_labels
from .report import FIELDS, build_draft
from .report_sources import (EXTRA_CATEGORIES, collect_reports, digest, excluded,
                             source_context, verify_snapshot)
from .workflow import (SubmissionBlocked, atomic_json, atomic_text, draft_hash,
                       file_lock, freeze_draft)


def due_kinds(day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    result = ['weekly'] if day.weekday() == 6 else []
    if day.day == calendar.monthrange(day.year, day.month)[1]:
        result.append('monthly')
    return result


def _now(now=None):
    value = now or datetime.now(CHINA)
    if value.tzinfo is None:
        raise SubmissionBlocked('timezone-aware clock required')
    return value.astimezone(CHINA)


def _grant(cfg):
    return {'version': 1, 'kinds': ['weekly', 'monthly'], 'timezone': 'Asia/Shanghai',
            'profile': cfg.get('dws', {}).get('profile'), 'identity': cfg.get('identity'),
            'recipients': cfg.get('recipients'), 'templates': cfg.get('templates'),
            'source_mode': 'reports', 'source_context': source_context(cfg)}


def periodic_config(base):
    """Derive a separate disabled configuration; never alter daily configuration."""
    cfg = {k: copy.deepcopy(base[k]) for k in ('_base', 'dws', 'identity', 'recipients',
           'templates', 'history_from', 'date_encoding') if k in base}
    cfg.update({'data_dir': 'data-periodic', 'notify': {'enabled': False},
                'quality': {'omit_hours': True, 'carryover': {'enabled': False},
                            'weekly_carryover': {'enabled': False}},
                'conversations': {'enabled': False}, 'workbuddy': {'enabled': False},
                'chatgpt': {'enabled': False}, 'supplemental': {'git_paths': [], 'markdown_paths': []},
                'codex': {'project_paths': [], 'session_ids': [],
                          'exclude_paths': copy.deepcopy(base.get('codex', {}).get('exclude_paths', []))},
                'periodic': {'enabled': False, 'draft_roots': ['data', 'data-auto', 'data-periodic'],
                             'exclude_terms': [], 'authorization_basis': 'user-approved WorkBuddy weekly/monthly plan'}})
    cfg['periodic']['authorization'] = copy.deepcopy(_grant(cfg))
    _guard(cfg)
    return cfg


def _guard(cfg, *, submit=False):
    periodic = cfg.get('periodic', {})
    if periodic.get('authorization') != _grant(cfg):
        raise SubmissionBlocked('periodic authorization changed or missing; reinitialize and repeat smoke checks')
    identity = cfg.get('identity', {})
    if not identity.get('corp_id') or not identity.get('user_id') or not cfg.get('dws', {}).get('profile'):
        raise SubmissionBlocked('periodic fixed account incomplete')
    recipients = cfg.get('recipients', [])
    templates = cfg.get('templates') or {}
    if not isinstance(recipients, list) or not recipients:
        raise SubmissionBlocked('periodic recipients missing')
    if any(not (isinstance(r, dict) and r.get('name') and r.get('user_id')) for r in recipients):
        raise SubmissionBlocked('periodic recipients missing or unlabeled')
    ids = [str(r.get('user_id')) for r in recipients]
    if len(set(ids)) != len(ids):
        raise SubmissionBlocked('periodic recipients contain duplicates')
    ids = set(ids)
    for kind in ('weekly', 'monthly'):
        declared = (templates.get(kind) or {}).get('fixed_recipients') or []
        declared_ids = {str(r.get('user_id')) for r in declared if isinstance(r, dict) and r.get('user_id')}
        if not declared_ids or declared_ids != ids:
            raise SubmissionBlocked(f'periodic {kind} fixed recipient list is missing or does not match recipients')
    if cfg.get('data_dir') != 'data-periodic' or cfg.get('notify', {}).get('enabled'):
        raise SubmissionBlocked('periodic data isolation/WorkBuddy notification policy changed')
    if submit and periodic.get('enabled') is not True:
        raise SubmissionBlocked('periodic submission disabled; two live smoke checks and enable required')


def _root(cfg):
    return resolve_path(cfg, cfg['data_dir'])


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sealed_read(path):
    obj = _read(path)
    if obj.get('hash') != digest(obj):
        raise SubmissionBlocked('frozen batch/job hash changed')
    return obj


def _seal(path, obj):
    obj['hash'] = digest(obj)
    atomic_json(path, obj)
    return obj


def _inside(cfg, path, folder):
    path = Path(path).resolve()
    if not path.is_relative_to((_root(cfg) / folder).resolve()):
        raise SubmissionBlocked('periodic artifact path outside its data directory')
    return path


def create_batch(cfg, now=None, *, smoke=False):
    _guard(cfg)
    now = _now(now)
    day = now.date().isoformat()
    kinds = ['weekly', 'monthly'] if smoke else due_kinds(day)
    missed = []
    if not smoke:
        clock_path = _root(cfg) / 'scheduler' / 'last-check.json'
        with file_lock(clock_path):
            previous = _read(clock_path)['date'] if clock_path.exists() else day
            cursor = date.fromisoformat(previous) + timedelta(days=1)
            while cursor < now.date():
                due = due_kinds(cursor)
                if due:
                    missed.append({'date': cursor.isoformat(), 'kinds': due})
                cursor += timedelta(days=1)
            if previous > day:
                raise SubmissionBlocked('clock date moved backwards')
            atomic_json(clock_path, {'date': day, 'checked_at': now.isoformat()})
    if not kinds:
        return {'status': 'not_due', 'kinds': [], 'missed': missed, 'notify_workbuddy': bool(missed)}
    name = ('smoke-' + uuid.uuid4().hex if smoke else day) + '.json'
    path = _root(cfg) / 'batches' / name
    with file_lock(path):
        if path.exists():
            batch = _sealed_read(path)
            if batch['grant'] != digest(_grant(cfg)):
                raise SubmissionBlocked('batch authorization differs from current configuration')
        else:
            batch = _seal(path, {'schema': 1, 'mode': 'smoke' if smoke else 'production',
                                'date': day, 'cutoff': now.isoformat(), 'kinds': kinds,
                                'grant': digest(_grant(cfg))})
    return {'status': 'due', 'batch': str(path.resolve()), 'kinds': batch['kinds'],
            'cutoff': batch['cutoff'], 'missed': missed, 'notify_workbuddy': bool(missed)}


def _batch(cfg, path, now):
    batch = _sealed_read(_inside(cfg, path, 'batches'))
    if batch.get('grant') != digest(_grant(cfg)):
        raise SubmissionBlocked('batch authorization mismatch')
    if batch.get('date') != _now(now).date().isoformat():
        raise SubmissionBlocked('batch date expired; offline reports are never backfilled automatically')
    if datetime.fromisoformat(batch['cutoff']) > _now(now):
        raise SubmissionBlocked('batch cutoff is in the future')
    return batch


def _job_path(cfg, batch_path, kind):
    return _root(cfg) / 'jobs' / (Path(batch_path).stem + '-' + kind + '.json')


def _result_path(cfg, job_path):
    return _root(cfg) / 'results' / Path(job_path).name


def _ordered(cfg, batch_path, batch, kind):
    if kind not in batch['kinds']:
        raise SubmissionBlocked('report is not due in this batch')
    # No-write smoke probes are independent so one blocked template cannot
    # hide diagnostics for the other. Production always enforces weekly first.
    if batch['mode'] == 'smoke':
        return []
    same_batch_ids = []
    if kind == 'monthly' and 'weekly' in batch['kinds']:
        result_path = _result_path(cfg, _job_path(cfg, batch_path, 'weekly'))
        result = _read(result_path) if result_path.exists() else {}
        allowed = {'verified', 'already_submitted'}
        if result.get('status') not in allowed:
            raise SubmissionBlocked('weekly must finish successfully before monthly preparation')
        if result.get('report_id'):
            same_batch_ids.append(result['report_id'])
    return same_batch_ids


def _live(cfg):
    client = cli._client(cfg)
    auth = client.auth()
    identity = cfg['identity']
    if any(auth.get(k) != identity.get(k) for k in ('corp_id', 'user_id')) or (
            identity.get('user_name') and auth.get('user_name') != identity['user_name']):
        raise SubmissionBlocked('periodic DWS identity mismatch')
    _, _, blockers = cli._verify_recipients(cfg, client)
    if blockers:
        raise SubmissionBlocked('; '.join(blockers))
    return client


def _existing_at_root(cfg, root, kind, day):
    """Different draft dates can identify the same week or calendar month."""
    period = period_labels(kind, day)
    days = {day}
    directory = resolve_path(cfg, root)
    candidates = list((directory / 'drafts').glob(kind + '-*.json'))
    for candidate in (directory / 'state').glob('*.json'):
        try:
            saved = state.read_state(candidate)
        except (OSError, ValueError):
            continue
        if saved and saved.get('draft_path'):
            candidates.append(Path(saved['draft_path']))
    for path in candidates:
        if not path.stem.startswith(kind + '-'):
            continue
        try:
            candidate_day = date.fromisoformat(path.stem[len(kind) + 1:]).isoformat()
        except ValueError:
            continue
        if period_labels(kind, candidate_day) == period:
            days.add(candidate_day)
    results = [_existing_on_day(cfg, root, kind, candidate_day) for candidate_day in sorted(days)]
    verified = [result for result in results if result]
    if len({result.get('report_id') for result in verified}) > 1:
        raise SubmissionBlocked('conflicting verified attempts in the same report period; reconcile first')
    return verified[0] if verified else None


def _existing_on_day(cfg, root, kind, day):
    """Inspect one draft root without ever creating its directories."""
    scoped = copy.deepcopy(cfg)
    scoped['data_dir'] = root
    path = cli._path(scoped, kind, day, 'drafts')
    raw = None
    draft = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('draft is not a JSON object')
            draft = cli._read_draft(path)
        except (OSError, ValueError, json.JSONDecodeError, SubmissionBlocked) as exc:
            # A state file can still prove an attempted send when the draft
            # mirror is missing or damaged, so continue with the raw object.
            if not isinstance(raw, dict):
                raise SubmissionBlocked(
                    f'periodic draft in {root} is unreadable; review before regeneration') from exc
    locator = draft or raw or {
        'kind': kind,
        'date': day,
        'template_id': cfg['templates'][kind]['id'],
        'approval_context': cli._approval_context(cfg, kind, day),
    }
    state_paths = [cli._state_path(scoped, locator)]
    state_dir = resolve_path(scoped, root) / 'state'
    if state_dir.is_dir():
        for candidate in state_dir.glob('*.json'):
            try:
                saved = state.read_state(candidate)
            except (OSError, ValueError):
                continue
            recorded_path = (saved or {}).get('draft_path')
            if recorded_path and Path(recorded_path).name == path.name:
                state_paths.append(candidate)
    verified = None
    for state_path in dict.fromkeys(Path(item).resolve() for item in state_paths):
        record = state.load_period(state_path=state_path, draft=draft or raw, draft_path=path)
        if record.status not in state.ATTEMPTED:
            continue
        if not record.consistent:
            raise SubmissionBlocked(
                f'legacy {kind} state in {root} is inconsistent: '
                f'{state.block_reason(record)}; reconcile before regeneration')
        if record.status == 'verified':
            verified = {'status': 'already_submitted', 'report_id': record.report_id,
                        'draft': str(path), 'data_root': root, 'submitted': False}
        else:
            raise SubmissionBlocked(
                f'legacy {kind} submission in {root} needs reconciliation: '
                f'{state.block_reason(record)}')
    if verified:
        return verified
    if path.exists() and draft is None:
        raise SubmissionBlocked(
            f'legacy {kind} draft in {root} is damaged or unvalidated; review before regeneration')
    return None


def _existing(cfg, kind, day):
    roots = [cfg['data_dir'], *cfg.get('periodic', {}).get(
        'draft_roots', ['data', 'data-auto', 'data-periodic'])]
    seen, verified = set(), []
    for root in roots:
        resolved = resolve_path(cfg, root).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result = _existing_at_root(cfg, root, kind, day)
        if result:
            verified.append(result)
    if not verified:
        return None
    report_ids = {str(item.get('report_id')) for item in verified if item.get('report_id')}
    if len(verified) > 1 and len(report_ids) != 1:
        raise SubmissionBlocked(
            'the same report period has conflicting verified attempts across data roots; '
            'reconcile before regeneration')
    return verified[0]


def prepare(cfg, batch_path, kind, *, now=None):
    _guard(cfg)
    batch = _batch(cfg, batch_path, now)
    same_batch_ids = _ordered(cfg, batch_path, batch, kind)
    job_path = _job_path(cfg, batch_path, kind)
    with file_lock(job_path):
        prior = _existing(cfg, kind, batch['date']) if batch['mode'] == 'production' else None
        if prior:
            atomic_json(_result_path(cfg, job_path), prior)
            return prior
        if job_path.exists():
            job = _sealed_read(job_path)
            verify_snapshot(_sealed_read(job['snapshot']), cfg, kind, batch['date'])
        else:
            client = _live(cfg)
            template_cfg = cfg['templates'][kind]
            template = client.template(template_cfg['name'], template_cfg['id'])
            # Source templates are live too; no October cache is a specification.
            upstream_templates = {}
            for upstream in (('daily',) if kind == 'weekly' else ('daily', 'weekly')):
                spec = cfg['templates'][upstream]
                upstream_templates[upstream] = client.template(spec['name'], spec['id'])
            known = set(FIELDS[kind].values()) | set(EXTRA_CATEGORIES)
            unknown = [f['field_name'] for f in template_fields(template)
                       if int(f['field_type']) == 1 and f['field_name'] not in known]
            if unknown:
                raise SubmissionBlocked('live template has unmapped text fields: ' + ', '.join(unknown))
            snapshot = collect_reports(cfg, client, kind, batch['date'], datetime.fromisoformat(batch['cutoff']),
                                       same_batch_ids=same_batch_ids)
            snapshot_path = _root(cfg) / 'snapshots' / job_path.name
            atomic_json(snapshot_path, snapshot)
            job = _seal(job_path, {'schema': 1, 'kind': kind, 'date': batch['date'],
                                   'batch': str(Path(batch_path).resolve()), 'batch_hash': batch['hash'],
                                   'snapshot': str(snapshot_path.resolve()), 'snapshot_hash': snapshot['hash'],
                                   'template': template, 'upstream_templates': upstream_templates,
                                   'approval_context': cli._approval_context(cfg, kind, batch['date'])})
        snapshot = _sealed_read(job['snapshot'])
        return {'status': 'blocked' if snapshot['blockers'] else 'prepared', 'job': str(job_path.resolve()),
                'snapshot': job['snapshot'], 'snapshot_hash': job['snapshot_hash'],
                'blockers': snapshot['blockers'], 'notify_workbuddy': bool(snapshot['blockers']),
                'extra_fields': {f['field_name']: EXTRA_CATEGORIES[f['field_name']]
                                 for f in template_fields(job['template']) if f['field_name'] in EXTRA_CATEGORIES}}


def finish(cfg, job_path, ai_path, *, submit=False, now=None):
    _guard(cfg)
    job_path = _inside(cfg, job_path, 'jobs')
    with file_lock(job_path):
        job = _sealed_read(job_path)
        batch = _batch(cfg, job['batch'], now)
        if batch['hash'] != job['batch_hash']:
            raise SubmissionBlocked('job batch changed')
        _ordered(cfg, job['batch'], batch, job['kind'])
        if submit and batch['mode'] == 'smoke':
            raise SubmissionBlocked('smoke batch cannot submit')
        _guard(cfg, submit=submit)
        if job['approval_context'] != cli._approval_context(cfg, job['kind'], job['date']):
            raise SubmissionBlocked('job approval context changed')
        if batch['mode'] == 'production':
            prior = _existing(cfg, job['kind'], job['date'])
            if prior:
                atomic_json(_result_path(cfg, job_path), prior)
                return prior
        snapshot = _sealed_read(_inside(cfg, job['snapshot'], 'snapshots'))
        verify_snapshot(snapshot, cfg, job['kind'], job['date'])
        if snapshot['hash'] != job['snapshot_hash'] or snapshot['cutoff'] != batch['cutoff']:
            raise SubmissionBlocked('job source snapshot changed')
        if snapshot['blockers']:
            raise SubmissionBlocked('; '.join(snapshot['blockers']))
        template = job['template']
        extras = {f['field_name']: EXTRA_CATEGORIES[f['field_name']] for f in template_fields(template)
                  if f['field_name'] in EXTRA_CATEGORIES}
        ai = _read(ai_path)
        ai.setdefault('extra_fields', {})
        # Missing business records have deterministic truthful text. If real
        # records exist, WorkBuddy must summarize them explicitly for that field.
        for name, category in extras.items():
            if not ai['extra_fields'].get(name):
                missing = next((f for f in snapshot['facts'] if f.get('category') == category
                                and f['status'] == 'unknown'), None)
                if not missing:
                    raise SubmissionBlocked('assisted summary missing business field with records: ' + name)
                ai['extra_fields'][name] = missing['text'] + ' [证据:' + missing['evidence_id'] + ']'
                if missing['evidence_id'] not in ai['citations']:
                    ai['citations'].append(missing['evidence_id'])
                if category == 'attendance':
                    coverage = next(f for f in snapshot['facts'] if f['evidence_id'] == 'coverage-attendance')
                    ai['extra_fields'][name] += '\n' + coverage['text'] + ' [证据:coverage-attendance]'
                    if 'coverage-attendance' not in ai['citations']:
                        ai['citations'].append('coverage-attendance')
        validate_ai_result(ai, snapshot['facts'], extra_fields=extras)
        if any(excluded(text, cfg) for text in [ai['completed'], ai['summary'], ai['plan'], *ai['extra_fields'].values()]):
            raise SubmissionBlocked('assisted output reintroduced an excluded project')
        draft = build_draft(job['kind'], job['date'], template, snapshot, ai_result=ai)
        draft.update({'generation_engine': 'workbuddy-assisted-reports', 'approval_context': job['approval_context'],
                      'source_snapshot': snapshot, 'template_last_modified': template.get('lastModifiedTime'),
                      'periodic_authorization': digest(_grant(cfg))})
        draft = freeze_draft(draft)
        # Smoke uses a separate folder and never changes a production draft.
        folder = 'smoke-drafts' if batch['mode'] == 'smoke' else 'drafts'
        draft_path = (_root(cfg) / folder / job_path.name) if batch['mode'] == 'smoke' else cli._path(cfg, job['kind'], job['date'], folder)
        with file_lock(draft_path):
            if draft_path.exists():
                previous = cli._read_draft(draft_path)
                if previous.get('submission', {}).get('status'):
                    raise SubmissionBlocked('existing submission attempt cannot be replaced')
                if previous.get('hash') != draft['hash']:
                    atomic_json(_root(cfg) / 'draft-archives' / (uuid.uuid4().hex + '.json'), previous)
            atomic_json(draft_path, draft)
        preview = cli._preview(cfg, draft_path)
        duplicate = preview['duplicate_check']['status']
        result = {'status': 'ready', 'submitted': False, 'draft': str(draft_path.resolve()),
                  'hash': draft['hash'], 'snapshot_hash': snapshot['hash'],
                  'preview_markdown': preview.get('preview_markdown'), 'job_hash': job['hash'],
                  'grant': digest(_grant(cfg)), 'template_fingerprint': template_fingerprint(template),
                  'checked_at': _now(now).isoformat()}
        if duplicate == 'possible_duplicate':
            result['status'] = 'already_submitted'
        elif duplicate != 'none_found':
            raise SubmissionBlocked('remote duplicate check incomplete: ' + duplicate)
        else:
            client, live_template, users, contents = cli._preflight(cfg, draft)
            client.submit(draft['template_id'], contents, users, dry_run=True)
            result['dry_run'] = True
            if submit:
                # The existing state machine arms before network, freezes the
                # exact contents and requires readback before marking verified.
                result.update(cli._submit(cfg, draft_path, draft['hash']))
                result['submitted'] = result['status'] == 'verified'
        result['notify_workbuddy'] = result['status'] not in {'ready', 'verified', 'already_submitted'}
        atomic_json(_result_path(cfg, job_path), result)
        return result


def enable(cfg):
    _guard(cfg)
    client = _live(cfg)
    proof = {}
    for path in sorted((_root(cfg) / 'jobs').glob('smoke-*.json'), key=lambda p: p.stat().st_mtime):
        job = _sealed_read(path)
        result_path = _result_path(cfg, path)
        result = _read(result_path) if result_path.exists() else {}
        if (result.get('status') == 'ready' and result.get('dry_run') is True
                and result.get('grant') == digest(_grant(cfg)) and result.get('job_hash') == job['hash']
                and datetime.fromisoformat(result['checked_at']).date() == _now().date()):
            proof[job['kind']] = result
    for kind in ('weekly', 'monthly'):
        spec = cfg['templates'][kind]
        live = client.template(spec['name'], spec['id'])
        if kind not in proof or proof[kind]['template_fingerprint'] != template_fingerprint(live):
            raise SubmissionBlocked('both same-day live weekly/monthly no-write smoke checks required before enable')
    cfg['periodic']['enabled'] = True
    return {'status': 'enabled', 'kinds': ['weekly', 'monthly'], 'scheduler_created': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config/config.periodic.local.yaml')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--base-config', default='config/config.auto.yaml')
    commands.add_parser('enable')
    due = commands.add_parser('due')
    due.add_argument('--smoke', action='store_true')
    prep = commands.add_parser('prepare')
    prep.add_argument('--batch', required=True)
    prep.add_argument('--kind', choices=['weekly', 'monthly'], required=True)
    final = commands.add_parser('finish')
    final.add_argument('--job', required=True)
    final.add_argument('--ai-result-file', required=True)
    final.add_argument('--submit', action='store_true')
    args = parser.parse_args(argv)
    path = cli._config_path(args.config)
    try:
        if args.command == 'init':
            if path.exists():
                raise SubmissionBlocked('periodic configuration already exists; preserve it and inspect before updating')
            cfg = periodic_config(load_config(cli._config_path(args.base_config)))
            atomic_text(path, yaml.safe_dump({k: v for k, v in cfg.items() if k != '_base'}, allow_unicode=True, sort_keys=False))
            result = {'status': 'initialized_disabled', 'config': str(path), 'scheduler_created': False}
        else:
            cfg = load_config(path)
            if args.command == 'due':
                result = create_batch(cfg, smoke=args.smoke)
            elif args.command == 'prepare':
                result = prepare(cfg, args.batch, args.kind)
            elif args.command == 'finish':
                result = finish(cfg, args.job, args.ai_result_file, submit=args.submit)
            else:
                result = enable(cfg)
                atomic_text(path, yaml.safe_dump({k: v for k, v in cfg.items() if k != '_base'}, allow_unicode=True, sort_keys=False))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('notify_workbuddy') or result.get('status') == 'blocked' else 0
    except (OSError, ValueError, KeyError, TypeError, SubmissionBlocked, DwsBlocked) as exc:
        result = {'status': 'blocked', 'reason': f'{type(exc).__name__}: {exc}', 'notify_workbuddy': True}
        if args.command == 'finish':
            try:
                failed_job = _sealed_read(_inside(cfg, args.job, 'jobs'))
                failed_batch = _sealed_read(failed_job['batch'])
                draft_path = (cli._path(cfg, failed_job['kind'], failed_job['date'], 'drafts')
                              if failed_batch['mode'] == 'production' else
                              _root(cfg) / 'smoke-drafts' / Path(args.job).name)
                result['draft'] = str(draft_path.resolve())
                if draft_path.exists():
                    recorded = _read(draft_path).get('submission', {}).get('status')
                    if recorded in {'unknown', 'mismatch', 'submitting'}:
                        result['status'] = recorded
                atomic_json(_result_path(cfg, args.job), result)
            except (OSError, ValueError, KeyError, SubmissionBlocked):
                pass
        # Local failure receipt only. WorkBuddy owns user-facing notifications.
        try:
            atomic_json(_root(cfg) / 'failures' / (datetime.now(CHINA).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json'), result)
        except (OSError, UnboundLocalError, KeyError):
            pass
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    sys.exit(main())
