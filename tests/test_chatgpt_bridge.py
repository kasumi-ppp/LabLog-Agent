import json
import pytest

from lablog.chatgpt_bridge import import_page


def test_bridge_merges_only_authorized_safe_text_and_preserves_real_times(tmp_path):
    cfg = {'_base': str(tmp_path), 'chatgpt': {'enabled': True, 'root': 'bridge', 'session_ids': ['chosen']}}
    def page(ident, text):
        return {'thread': {'id': 'chosen', 'title': '论文分析', 'createdAt': 1792120000, 'updatedAt': 1792120100},
                'attachments': [{'path': 'private.pdf'}], 'turns': [{'id': ident, 'startedAt': 1792120000,
                'completedAt': 1792120100, 'items': [{'type': 'userMessage', 'id': ident, 'content': [{'type': 'text', 'text': text}]},
                {'type': 'agentMessage', 'id': ident + '-a', 'text': '::chatgpt-content-reference{index="0"}'}]}]}
    import_page(cfg, page('one', '解释公式'))
    import_page(cfg, page('two', '分析论文'))
    result = import_page(cfg, page('one', '解释公式'))
    assert result['turn_count'] == 2 and result['partial']
    path = tmp_path / 'bridge/chosen.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['turns'][0]['startedAt'] == 1792120000
    assert len(saved['turns'][0]['items']) == 1
    assert 'private.pdf' not in path.read_text(encoding='utf-8')
    import_page(cfg, page('secret', 'api_key=DO_NOT_COPY'))
    assert 'DO_NOT_COPY' not in path.read_text(encoding='utf-8')
    unauthorized = page('three', '禁止采集')
    unauthorized['thread']['id'] = 'other'
    with pytest.raises(ValueError):
        import_page(cfg, unauthorized)
    assert not (tmp_path / 'bridge/other.json').exists()
