"""Tests for the keyring-backed credential storage (config.ConfigService).

Credentials are stored as a JSON envelope {fmt, src, ct} where ct is the
credentials JSON encrypted with a wrapping key sourced from the OS keychain
(src=keyring), an env var (src=env), or the legacy static constant (src=static).
A fake in-memory keyring is injected via ConfigService._keyring so no real OS
keychain is touched.
"""
import json
import os
import stat

import pytest

from config.config import ConfigService, CRED_FMT, KEYRING_SERVICE, KEYRING_KEY


CREDS = {
    'token': 'JWT-AAA',
    'newToken': 'JWT-BBB',
    'user': {'email': 'u@example.com', 'uuid': 'uid-1',
             'mnemonic': 'abandon abandon SECRETWORD about'},
}


class FakeKeyring:
    """Minimal in-memory stand-in for the `keyring` module."""
    def __init__(self):
        self.store = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value

    def delete_password(self, service, key):
        self.store.pop((service, key), None)


@pytest.fixture
def cs(tmp_path):
    c = ConfigService()
    c.internxt_cli_data_dir = tmp_path
    c.credentials_file = tmp_path / '.inxtcli'
    return c


def _raw(cs):
    return cs.credentials_file.read_text()


def test_keyring_roundtrip_and_key_in_keychain(cs, monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(cs, '_keyring', lambda: fake)

    cs.save_user_credentials(CREDS)

    env = json.loads(_raw(cs))
    assert env['fmt'] == CRED_FMT and env['src'] == 'keyring'
    # Only ciphertext on disk — no plaintext secrets leak.
    assert 'SECRETWORD' not in _raw(cs)
    assert 'JWT-AAA' not in _raw(cs)
    # The wrapping key lives in the (fake) keychain, not the file.
    assert fake.get_password(KEYRING_SERVICE, KEYRING_KEY)

    out = cs.read_user_credentials()
    assert out['token'] == 'JWT-AAA'
    assert out['user']['mnemonic'].startswith('abandon ')

    # Without the keychain key the file is undecryptable.
    fake.store.clear()
    assert cs.read_user_credentials() is None


def test_clear_removes_keyring_key_and_truncates(cs, monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(cs, '_keyring', lambda: fake)
    cs.save_user_credentials(CREDS)
    assert fake.get_password(KEYRING_SERVICE, KEYRING_KEY)

    cs.clear_user_credentials()
    assert fake.get_password(KEYRING_SERVICE, KEYRING_KEY) is None
    assert _raw(cs) == ''


def test_env_key_path(cs, monkeypatch):
    monkeypatch.setattr(cs, '_keyring', lambda: None)         # no keychain
    monkeypatch.setenv('INTERNXT_CREDENTIALS_KEY', 'ci-provided-secret')

    cs.save_user_credentials(CREDS)
    assert json.loads(_raw(cs))['src'] == 'env'
    assert cs.read_user_credentials()['token'] == 'JWT-AAA'

    # Without the env key, it can't be decrypted.
    monkeypatch.delenv('INTERNXT_CREDENTIALS_KEY')
    assert cs.read_user_credentials() is None


def test_static_fallback_and_file_perms(cs, monkeypatch):
    monkeypatch.setattr(cs, '_keyring', lambda: None)         # no keychain, no env key
    cs.save_user_credentials(CREDS)

    assert json.loads(_raw(cs))['src'] == 'static'
    assert 'SECRETWORD' not in _raw(cs)
    assert cs.read_user_credentials()['token'] == 'JWT-AAA'

    if os.name == 'posix':
        mode = stat.S_IMODE(os.stat(cs.credentials_file).st_mode)
        assert mode == 0o600, oct(mode)


def test_legacy_blob_is_read_and_migrated(cs, monkeypatch):
    monkeypatch.setattr(cs, '_keyring', lambda: None)
    # Write a bare legacy static-key blob (the old on-disk format).
    crypto = cs._get_crypto_service()
    cs.credentials_file.write_text(crypto.encrypt_text(json.dumps(CREDS)))

    out = cs.read_user_credentials()
    assert out['user']['email'] == 'u@example.com'

    # On read it is transparently upgraded to the envelope format.
    env = json.loads(_raw(cs))
    assert env['fmt'] == CRED_FMT
