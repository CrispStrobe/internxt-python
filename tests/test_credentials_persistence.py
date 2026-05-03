"""Tests for config_service credential & webdav-config persistence.

These touch the filesystem (real encryption + real disk I/O) but use a
temporary directory so nothing leaks into the user's ~/.internxt-cli.
"""

import pytest


@pytest.fixture
def isolated_config_service(tmp_path, monkeypatch):
    """Build a fresh ConfigService whose data dir is a tmp path."""
    from config.config import ConfigService

    cs = ConfigService()
    cs.internxt_cli_data_dir = tmp_path
    cs.credentials_file = tmp_path / 'credentials.json'
    cs.webdav_configs_file = tmp_path / 'webdav.json'
    cs.webdav_pid_file = tmp_path / 'webdav.pid'
    return cs


# ---------- user credential round-trip ----------

def test_save_then_read_user_credentials_roundtrip(isolated_config_service):
    cs = isolated_config_service
    creds = {
        'token': 'abc.def.ghi',
        'newToken': 'jkl.mno.pqr',
        'user': {
            'email': 'u@example.com',
            'uuid': 'user-uuid',
            'rootFolderId': 'root-uuid',
            'mnemonic': ('abandon abandon abandon abandon abandon abandon '
                         'abandon abandon abandon abandon abandon about'),
        },
    }
    cs.save_user_credentials(creds)
    # File now exists and contains ciphertext (not the plaintext token)
    raw = cs.credentials_file.read_text()
    assert raw and 'abc.def.ghi' not in raw

    # Round-trip recovers all fields
    out = cs.read_user_credentials()
    assert out is not None
    assert out['token'] == creds['token']
    assert out['newToken'] == creds['newToken']
    assert out['user']['email'] == 'u@example.com'
    assert out['user']['mnemonic'].startswith('abandon ')


def test_read_returns_none_when_file_missing(isolated_config_service):
    cs = isolated_config_service
    assert not cs.credentials_file.exists()
    assert cs.read_user_credentials() is None


def test_read_returns_none_for_empty_file(isolated_config_service):
    cs = isolated_config_service
    cs.credentials_file.write_text('')
    assert cs.read_user_credentials() is None


def test_clear_user_credentials_truncates_file(isolated_config_service):
    cs = isolated_config_service
    cs.save_user_credentials({'token': 't', 'user': {'email': 'x'}})
    assert cs.credentials_file.read_text() != ''
    cs.clear_user_credentials()
    assert cs.credentials_file.read_text() == ''


def test_clear_when_file_missing_is_noop(isolated_config_service):
    cs = isolated_config_service
    # Should not raise
    cs.clear_user_credentials()
    assert not cs.credentials_file.exists()


def test_clear_raises_when_already_empty(isolated_config_service):
    """Mirrors the TypeScript reference: an already-empty file is an error."""
    cs = isolated_config_service
    cs.credentials_file.write_text('')
    with pytest.raises(ValueError, match="already empty"):
        cs.clear_user_credentials()


# ---------- webdav config persistence ----------

def test_webdav_config_save_read_roundtrip(isolated_config_service):
    cs = isolated_config_service
    config = {
        'host': '127.0.0.1',
        'port': '9999',
        'protocol': 'https',
        'timeoutMinutes': '60',
    }
    cs.save_webdav_config(config)
    out = cs.read_webdav_config()
    assert out['host'] == '127.0.0.1'
    assert str(out['port']) == '9999'
    assert out['protocol'] == 'https'


def test_webdav_config_returns_empty_dict_when_missing(isolated_config_service):
    cs = isolated_config_service
    out = cs.read_webdav_config()
    assert isinstance(out, dict)


# ---------- pid file ----------

def test_webdav_pid_save_read_clear_cycle(isolated_config_service):
    cs = isolated_config_service
    assert cs.read_webdav_pid() is None  # nothing yet
    cs.save_webdav_pid(12345)
    assert cs.read_webdav_pid() == 12345
    cs.clear_webdav_pid()
    assert cs.read_webdav_pid() is None


def test_webdav_pid_garbage_returns_none(isolated_config_service):
    cs = isolated_config_service
    cs.webdav_pid_file.write_text('not-a-number')
    assert cs.read_webdav_pid() is None
