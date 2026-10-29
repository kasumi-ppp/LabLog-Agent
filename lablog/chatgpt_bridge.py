"""Import explicitly authorized read_thread results, never arbitrary ChatGPT chats."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, resolve_path
from .sources import _canonical_message
from .workflow import atomic_json, file_lock


def import_page(cfg, page):
    settings = cfg.get('chatgpt') or {}
    thread = page.get('thread') or {}
    sid = str(thread.get('id') or '')
    if not settings.get('enabled') or sid not in (settings.get('session_ids') or []):
        raise ValueError('ChatGPT conversation is not authorized')
    if not sid or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in sid):
        raise ValueError('invalid conversation id')
    path = resolve_path(cfg, settings.get('root', 'data/chatgpt-source')) / (sid + '.json')
    with file_lock(path):
        previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if previous and (previous.get('thread') or {}).get('id') != sid:
            raise ValueError('snapshot conversation id mismatch')
        turns = {t['id']: t for t in previous.get('turns', [])}
        partial = bool(previous.get('partial'))
        for turn in page.get('turns', []):
            messages = []
            for item in turn.get('items', []):
                if item.get('type') == 'userMessage':
                    role = 'user'
                    content = [{'type': 'input_text', 'text': b.get('text', '')}
                               for b in item.get('content', []) if b.get('type') == 'text']
                elif item.get('type') == 'agentMessage':
                    role = 'assistant'
                    text = str(item.get('text') or '')
                    if '::chatgpt-content-reference' in text or item.get('truncated'):
                        partial = True
                        continue
                    content = [{'type': 'output_text', 'text': text}]
                else:
                    continue
                parsed = _canonical_message({'type': 'response_item', 'payload': {
                    'type': 'message', 'role': role, 'channel': 'final' if role == 'assistant' else None,
                    'content': content}})
                if parsed and item.get('id'):
                    messages.append({'type': item['type'], 'id': item['id'],
                                     **({'content': [{'type': 'text', 'text': parsed[2]}]} if role == 'user' else {'text': parsed[2]})})
            if turn.get('id'):
                turns[turn['id']] = {'id': turn['id'], 'startedAt': turn.get('startedAt'),
                                     'completedAt': turn.get('completedAt'), 'items': messages}
        snapshot = {'thread': {k: thread.get(k) for k in ('id', 'title', 'createdAt', 'updatedAt')},
                    'turns': list(turns.values()), 'partial': partial,
                    'synced_at': datetime.now(timezone.utc).isoformat()}
        atomic_json(path, snapshot)
    return {'id': sid, 'turn_count': len(turns), 'partial': partial}


def main():
    parser = argparse.ArgumentParser(description='Import a read_thread page for an authorized ChatGPT monitor')
    parser.add_argument('--config', action='append', required=True)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding='utf-8'))
    pages = payload if isinstance(payload, list) else [payload]
    for config in args.config:
        cfg = load_config(config)
        for page in pages:
            print(json.dumps(import_page(cfg, page), ensure_ascii=False))


if __name__ == '__main__':
    main()
