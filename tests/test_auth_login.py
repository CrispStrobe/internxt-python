"""Tests for services/auth.py login + refresh flows."""
from unittest.mock import patch

import pytest

from services.auth import auth_service
from services.crypto import crypto_service


# ---------- is_2fa_needed ----------

def test_is_2fa_needed_returns_true_when_api_says_yes():
    with patch.object(auth_service.api, 'security_details',
                      return_value={'tfa': True, 'sKey': 'salt'}):
        assert auth_service.is_2fa_needed('u@example.com') is True


def test_is_2fa_needed_returns_false_when_api_says_no():
    with patch.object(auth_service.api, 'security_details',
                      return_value={'tfa': False, 'sKey': 'salt'}):
        assert auth_service.is_2fa_needed('u@example.com') is False


def test_is_2fa_needed_returns_false_on_api_error():
    """Network failure → assume no 2FA so the user gets the standard prompt."""
    with patch.object(auth_service.api, 'security_details',
                      side_effect=ConnectionError("network down")):
        assert auth_service.is_2fa_needed('u@example.com') is False


# ---------- compute_bridge_auth ----------

def test_compute_bridge_auth_hashes_user_id_with_sha256():
    import hashlib
    user_id = 'user-12345'
    expected = hashlib.sha256(user_id.encode()).hexdigest()
    user, password = auth_service.compute_bridge_auth('bridge_user', user_id)
    assert user == 'bridge_user'
    assert password == expected


def test_compute_bridge_auth_handles_int_user_id():
    """user_id may come in as int from older API responses; must still work."""
    user, _ = auth_service.compute_bridge_auth('bu', 42)
    assert user == 'bu'


# ---------- do_login: hydrated flow ----------

def test_do_login_full_flow():
    """Verify the four-step hydrated login: securityDetails → loginAccess →
    refresh → return decrypted mnemonic."""
    # Encrypt a mnemonic with the user's password so the final decryption works.
    real_mnemonic = ("abandon abandon abandon abandon abandon abandon "
                     "abandon abandon abandon abandon abandon about")
    password = "correct horse battery staple"
    encrypted_mnemonic = crypto_service.encrypt_text_with_key(real_mnemonic, password)

    # Build the salt envelope (encrypted hex of the actual PBKDF2 salt)
    salt_plain = "abcdef0123456789abcdef0123456789"
    s_key = crypto_service.encrypt_text(salt_plain)

    sec_details = {'sKey': s_key, 'tfa': False}
    access_res = {'newToken': 'temp-new-token'}
    hydrated = {
        'token': 'final-token',
        'newToken': 'final-new-token',
        'user': {
            'userId': 'u-42',
            'email': 'cli@example.com',
            'rootFolderId': 'root-uuid',
            'bridgeUser': 'cli@example.com',
            'mnemonic': encrypted_mnemonic,
            'bucket': 'bucket-id',
        },
    }

    with patch.object(auth_service.api, 'security_details',
                      return_value=sec_details), \
         patch.object(auth_service.api, 'login_access',
                      return_value=access_res) as mock_access, \
         patch.object(auth_service.api, 'refresh_token',
                      return_value=hydrated) as mock_refresh:
        result = auth_service.do_login('CLI@Example.com  ', password)

    # Email lowercased + stripped before being sent
    args, _ = mock_access.call_args
    assert args[0]['email'] == 'cli@example.com'

    # Refresh receives the temp token from access step
    mock_refresh.assert_called_once_with('temp-new-token')

    # Result has decrypted mnemonic + bridgePass populated
    assert result['user']['mnemonic'] == real_mnemonic
    assert result['user']['email'] == 'cli@example.com'
    assert result['token'] == 'final-token'
    assert result['newToken'] == 'final-new-token'
    assert result['user']['bridgePass']  # sha256 hex
    assert 'lastLoggedInAt' in result


def test_do_login_raises_when_security_details_lacks_skey():
    with patch.object(auth_service.api, 'security_details', return_value={'tfa': False}):
        with pytest.raises(ValueError, match="Salt"):
            auth_service.do_login('u@example.com', 'pw')


def test_do_login_strips_and_lowercases_email():
    """Whitespace + casing in user input must not propagate to the server."""
    salt_plain = "00" * 16
    s_key = crypto_service.encrypt_text(salt_plain)
    real_mnemonic = ("abandon abandon abandon abandon abandon abandon "
                     "abandon abandon abandon abandon abandon about")
    enc = crypto_service.encrypt_text_with_key(real_mnemonic, 'pw')

    captured = {}

    def fake_access(payload):
        captured['payload'] = payload
        return {'newToken': 't'}

    hydrated = {
        'token': 't', 'newToken': 'nt',
        'user': {
            'userId': 'x', 'email': 'a@b.com', 'rootFolderId': 'r',
            'bridgeUser': 'a@b.com', 'mnemonic': enc, 'bucket': 'b',
        },
    }
    with patch.object(auth_service.api, 'security_details',
                      return_value={'sKey': s_key, 'tfa': False}), \
         patch.object(auth_service.api, 'login_access', side_effect=fake_access), \
         patch.object(auth_service.api, 'refresh_token', return_value=hydrated):
        auth_service.do_login('  USER@EXAMPLE.COM  ', 'pw')

    assert captured['payload']['email'] == 'user@example.com'


# ---------- whoami / logout ----------

def test_whoami_returns_user_info_when_authenticated():
    fake_creds = {
        'token': 't', 'newToken': 'nt',
        'user': {
            'email': 'u@example.com', 'uuid': 'user-uuid',
            'rootFolderId': 'root-uuid',
            'mnemonic': 'm',
        },
    }
    with patch.object(auth_service, 'get_auth_details', return_value=fake_creds):
        info = auth_service.whoami()
    assert info == {
        'email': 'u@example.com',
        'uuid': 'user-uuid',
        'rootFolderId': 'root-uuid',
    }


def test_whoami_returns_none_when_not_logged_in():
    with patch.object(auth_service, 'get_auth_details',
                      side_effect=ValueError("MissingCredentialsError")):
        assert auth_service.whoami() is None


def test_get_auth_details_raises_when_credentials_incomplete():
    with patch.object(auth_service.config, 'read_user_credentials',
                      return_value={'token': 't'}):  # missing newToken + user
        with pytest.raises(ValueError, match="MissingCredentialsError"):
            auth_service.get_auth_details()


def test_get_auth_details_raises_when_no_credentials_file():
    with patch.object(auth_service.config, 'read_user_credentials', return_value=None):
        with pytest.raises(ValueError, match="MissingCredentialsError"):
            auth_service.get_auth_details()
