import pytest

from lablog.dws import DwsBlocked, DwsClient


def test_periodic_auth_validates_selected_slot_without_readonly(monkeypatch):
    client = DwsClient('dws.exe', 'fixed-profile', validate_auth=True)
    calls = []
    def call(parts):
        calls.append(parts)
        return {'success': True, 'authenticated': True, 'token_valid': True}
    monkeypatch.setattr(client, 'call', call)
    client.auth()
    assert calls == [['auth', 'status']]


def test_unknown_token_validity_never_counts_as_validated(monkeypatch):
    client = DwsClient('dws.exe', 'fixed-profile', validate_auth=True)
    monkeypatch.setattr(client, 'call', lambda parts: {'success': True, 'authenticated': True, 'token_valid': None})
    with pytest.raises(DwsBlocked):
        client.auth()
