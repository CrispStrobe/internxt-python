"""Tests for auth_service.refresh_tokens — the token-rotation path."""
from unittest.mock import patch

import pytest

from services.auth import auth_service


def test_refresh_tokens_updates_credentials_and_session():
    """refresh_tokens must:
       1) Call api.refresh_token with the current newToken
       2) Update bridgeAuth from the response's user metadata
       3) Persist the rotated credentials
       4) Push the new tokens into the api session
    """
    initial_creds = {
        'token': 'old-t',
        'newToken': 'old-nt',
        'user': {'email': 'u@example.com', 'userId': 'old-uid',
                 'bridgeUser': 'old-uid'},
    }
    refreshed = {
        'token': 'new-t',
        'newToken': 'new-nt',
        'user': {'userId': 'new-uid', 'bridgeUser': 'new-uid'},
    }

    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=initial_creds), \
         patch.object(auth_service.api, 'refresh_token',
                      return_value=refreshed) as mock_refresh, \
         patch.object(auth_service.config, 'save_user_credentials') as mock_save, \
         patch.object(auth_service.api, 'set_auth_tokens') as mock_set:
        result = auth_service.refresh_tokens()

    # Refresh API gets the OLD newToken
    mock_refresh.assert_called_once_with('old-nt')

    # Tokens rotated in returned creds
    assert result['token'] == 'new-t'
    assert result['newToken'] == 'new-nt'

    # bridgeAuth populated from refreshed user metadata
    assert result['user']['bridgeAuth'][0] == 'new-uid'  # bridgeUser
    # bridgeAuth password is sha256(userId)
    import hashlib
    assert result['user']['bridgeAuth'][1] == hashlib.sha256(b'new-uid').hexdigest()

    # Persisted to disk
    mock_save.assert_called_once()
    saved = mock_save.call_args[0][0]
    assert saved['token'] == 'new-t'

    # Session updated with rotated tokens
    mock_set.assert_called_once_with('new-t', 'new-nt')


def test_refresh_tokens_propagates_api_failure():
    initial_creds = {
        'token': 't', 'newToken': 'nt',
        'user': {'email': 'u', 'userId': 'uid'},
    }
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=initial_creds), \
         patch.object(auth_service.api, 'refresh_token',
                      side_effect=ConnectionError("network down")), \
         patch.object(auth_service.config, 'save_user_credentials') as mock_save, \
         patch.object(auth_service.api, 'set_auth_tokens') as mock_set:
        with pytest.raises(ConnectionError):
            auth_service.refresh_tokens()

    # On failure: must NOT persist or update session
    mock_save.assert_not_called()
    mock_set.assert_not_called()


def test_login_persists_credentials_and_sets_session():
    """auth_service.login() = do_login + save + set_auth_tokens."""
    fake_result = {
        'token': 't', 'newToken': 'nt',
        'user': {'email': 'u@example.com', 'mnemonic': 'm', 'rootFolderId': 'r'},
        'lastLoggedInAt': '2026-01-01T00:00:00Z',
    }
    with patch.object(auth_service, 'do_login', return_value=fake_result), \
         patch.object(auth_service.config, 'save_user_credentials') as mock_save, \
         patch.object(auth_service.api, 'set_auth_tokens') as mock_set:
        result = auth_service.login('u@example.com', 'pw')

    assert result == fake_result
    mock_save.assert_called_once_with(fake_result)
    mock_set.assert_called_once_with('t', 'nt')


def test_logout_clears_credentials_and_session():
    with patch.object(auth_service.config, 'clear_user_credentials') as mock_clear, \
         patch.object(auth_service.api, 'set_auth_tokens') as mock_set:
        auth_service.logout()
    mock_clear.assert_called_once()
    mock_set.assert_called_once_with(None, None)


def test_get_auth_details_pushes_tokens_into_session():
    """Reading credentials must also update the api session bearer header."""
    creds = {
        'token': 't1', 'newToken': 'nt1',
        'user': {'email': 'u', 'mnemonic': 'm'},
    }
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value=creds), \
         patch.object(auth_service.api, 'set_auth_tokens') as mock_set:
        out = auth_service.get_auth_details()

    assert out is creds
    mock_set.assert_called_once_with('t1', 'nt1')
