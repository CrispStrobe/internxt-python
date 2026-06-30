"""Tests for implicit env-based auto-login (issue #9).

When there's no usable stored session, refresh_tokens()/get_auth_details() fall
back to logging in from INTERNXT_EMAIL / INTERNXT_PASSWORD (+ INTERNXT_TFA_SECRET)
so piped/unattended commands (e.g. `pg_dump | rcat`) need no separate `login`.
"""
from unittest.mock import patch

import pytest

from services.auth import auth_service

_FAKE_CREDS = {
    'token': 't', 'newToken': 'nt',
    'user': {'email': 'u@example.com', 'mnemonic': 'm'},
}


@pytest.fixture(autouse=True)
def _clear_login_env(monkeypatch):
    """Isolate from a developer's real Internxt env vars."""
    for var in ('INTERNXT_EMAIL', 'INTERNXT_PASSWORD', 'INTERNXT_TFA_SECRET'):
        monkeypatch.delenv(var, raising=False)


def test_refresh_tokens_auto_logs_in_when_no_stored_session(monkeypatch):
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    monkeypatch.setenv('INTERNXT_PASSWORD', 'pw')
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=None), \
         patch.object(auth_service, 'is_2fa_needed', return_value=False), \
         patch.object(auth_service, 'login',
                      return_value=_FAKE_CREDS) as mock_login:
        out = auth_service.refresh_tokens()
    mock_login.assert_called_once_with('u@example.com', 'pw', None)
    assert out is _FAKE_CREDS


def test_get_auth_details_auto_logs_in_when_no_stored_session(monkeypatch):
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    monkeypatch.setenv('INTERNXT_PASSWORD', 'pw')
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=None), \
         patch.object(auth_service, 'is_2fa_needed', return_value=False), \
         patch.object(auth_service, 'login', return_value=_FAKE_CREDS), \
         patch.object(auth_service.api, 'set_auth_tokens') as mock_set:
        out = auth_service.get_auth_details()
    assert out is _FAKE_CREDS
    mock_set.assert_called_once_with('t', 'nt')


def test_no_creds_and_no_env_raises():
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=None):
        with pytest.raises(ValueError, match="No valid credentials"):
            auth_service.get_auth_details()
        with pytest.raises(ValueError, match="No valid credentials"):
            auth_service.refresh_tokens()


def test_rotation_failure_falls_back_to_env_login(monkeypatch):
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    monkeypatch.setenv('INTERNXT_PASSWORD', 'pw')
    stale = {'token': 't', 'newToken': 'nt',
             'user': {'email': 'u@example.com', 'userId': 'uid'}}
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=stale), \
         patch.object(auth_service.api, 'refresh_token',
                      side_effect=Exception("token revoked")), \
         patch.object(auth_service, 'is_2fa_needed', return_value=False), \
         patch.object(auth_service, 'login',
                      return_value=_FAKE_CREDS) as mock_login:
        out = auth_service.refresh_tokens()
    mock_login.assert_called_once()
    assert out is _FAKE_CREDS


def test_auto_login_derives_2fa_from_totp_secret(monkeypatch):
    pytest.importorskip('pyotp')
    import pyotp
    secret = pyotp.random_base32()
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    monkeypatch.setenv('INTERNXT_PASSWORD', 'pw')
    monkeypatch.setenv('INTERNXT_TFA_SECRET', secret)
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=None), \
         patch.object(auth_service, 'is_2fa_needed', return_value=True), \
         patch.object(auth_service, 'login',
                      return_value=_FAKE_CREDS) as mock_login:
        auth_service.refresh_tokens()
    # login called with a freshly-generated 6-digit TOTP code
    code = mock_login.call_args[0][2]
    assert code == pyotp.TOTP(secret).now()
    assert len(code) == 6 and code.isdigit()


def test_auto_login_requires_2fa_secret_when_2fa_enabled(monkeypatch):
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    monkeypatch.setenv('INTERNXT_PASSWORD', 'pw')
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=None), \
         patch.object(auth_service, 'is_2fa_needed', return_value=True):
        with pytest.raises(ValueError, match="INTERNXT_TFA_SECRET"):
            auth_service.get_auth_details()
