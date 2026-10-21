"""Tests for the selectable macOS keychain and the keychain-stored 2FA secret.

The keychain is chosen with `config --set-keychain` (persisted in
settings.json) or INTERNXT_KEYCHAIN, and accessed through MacKeychain, which
drives the `security` tool. `security` is faked here, so no real keychain is
ever touched.
"""
import json
import stat
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import config.config as config_mod
from config.config import ConfigService, MacKeychain, KEYRING_SERVICE, TFA_SECRET_ACCOUNT_PREFIX
from services.auth import auth_service
from cli import cli


@pytest.fixture
def cs(tmp_path, monkeypatch):
    monkeypatch.delenv('INTERNXT_KEYCHAIN', raising=False)
    c = ConfigService()
    c.internxt_cli_data_dir = tmp_path
    c.credentials_file = tmp_path / '.inxtcli'
    c.settings_file = tmp_path / 'settings.json'
    return c


class FakeSecurity:
    """Stands in for subprocess.run(['security', ...]) with an in-memory store."""

    def __init__(self, broken_writes=False):
        self.items = {}
        self.calls = []
        self.broken_writes = broken_writes

    def __call__(self, argv, input=None, capture_output=True, text=True):
        self.calls.append((argv, input))
        result = type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        cmd = argv[1]
        if cmd == '-i':
            if not self.broken_writes:
                import shlex
                args = shlex.split(input)
                opts = dict(zip(args[2:8:2], args[3:8:2]))
                self.items[(args[-1], opts['-s'], opts['-a'])] = opts['-w']
            return result  # `security -i` exits 0 even on failure
        service, account, keychain = argv[argv.index('-s') + 1], argv[argv.index('-a') + 1], argv[-1]
        key = (keychain, service, account)
        if key not in self.items:
            result.returncode = MacKeychain.ITEM_NOT_FOUND
        elif cmd == 'find-generic-password':
            result.stdout = self.items[key] + '\n'
        elif cmd == 'delete-generic-password':
            del self.items[key]
        return result


# ---------- settings ----------

def test_setting_round_trip_and_unset(cs):
    assert cs.get_setting('keychain') is None
    cs.set_setting('keychain', 'work')
    assert cs.get_setting('keychain') == 'work'
    assert json.loads(cs.settings_file.read_text()) == {'keychain': 'work'}
    assert stat.S_IMODE(cs.settings_file.stat().st_mode) == 0o600
    cs.set_setting('keychain', None)
    assert cs.get_setting('keychain') is None


def test_env_overrides_saved_keychain(cs, monkeypatch):
    cs.set_setting('keychain', 'saved')
    assert cs.get_keychain() == 'saved'
    monkeypatch.setenv('INTERNXT_KEYCHAIN', 'from-env')
    assert cs.get_keychain() == 'from-env'


def test_configured_keychain_selects_mac_backend(cs, monkeypatch):
    monkeypatch.delenv('INTERNXT_NO_KEYRING')
    monkeypatch.setattr(config_mod.sys, 'platform', 'darwin')
    cs.set_setting('keychain', 'work')
    kr = cs._keyring()
    assert isinstance(kr, MacKeychain) and kr.keychain == 'work'


def test_no_keyring_env_disables_configured_keychain(cs):
    cs.set_setting('keychain', 'work')
    assert cs._keyring() is None  # INTERNXT_NO_KEYRING=1 from conftest


# ---------- MacKeychain ----------

def test_mac_keychain_round_trip_keeps_secret_out_of_argv(monkeypatch):
    fake = FakeSecurity()
    monkeypatch.setattr(config_mod.subprocess, 'run', fake)
    kc = MacKeychain('/tmp/my kc.keychain-db')
    secret = 'se cr"et\\x'

    assert kc.get_password('svc', 'acct') is None
    kc.set_password('svc', 'acct', secret)
    assert kc.get_password('svc', 'acct') == secret
    assert all(secret not in ' '.join(argv) for argv, _ in fake.calls)

    kc.delete_password('svc', 'acct')
    assert kc.get_password('svc', 'acct') is None
    kc.delete_password('svc', 'acct')  # deleting a missing item is fine


def test_mac_keychain_write_failure_raises(monkeypatch):
    monkeypatch.setattr(config_mod.subprocess, 'run', FakeSecurity(broken_writes=True))
    with pytest.raises(RuntimeError, match='Cannot write to keychain'):
        MacKeychain('locked').set_password('svc', 'acct', 'x')


# ---------- 2FA secret ----------

def test_tfa_secret_saved_per_normalised_email(cs, monkeypatch):
    store = {}
    fake = type('K', (), {
        'get_password': lambda self, s, a: store.get((s, a)),
        'set_password': lambda self, s, a, p: store.__setitem__((s, a), p),
    })()
    monkeypatch.setattr(cs, '_keyring', lambda: fake)
    cs.save_tfa_secret(' User@Example.com ', 'BASE32SECRET')
    assert store == {(KEYRING_SERVICE, TFA_SECRET_ACCOUNT_PREFIX + 'user@example.com'): 'BASE32SECRET'}
    assert cs.get_tfa_secret('user@example.com') == 'BASE32SECRET'


def test_save_tfa_secret_without_keychain_raises(cs):
    with pytest.raises(RuntimeError, match='No keychain'):
        cs.save_tfa_secret('u@example.com', 'X')
    assert cs.get_tfa_secret('u@example.com') is None


def test_auto_login_uses_keychain_tfa_secret(monkeypatch):
    pyotp = pytest.importorskip('pyotp')
    secret = pyotp.random_base32()
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    monkeypatch.setenv('INTERNXT_PASSWORD', 'pw')
    monkeypatch.delenv('INTERNXT_TFA_SECRET', raising=False)
    with patch.object(auth_service.config, 'read_user_credentials', return_value=None), \
         patch.object(auth_service.config, 'get_tfa_secret', return_value=secret), \
         patch.object(auth_service, 'is_2fa_needed', return_value=True), \
         patch.object(auth_service, 'login', return_value={'user': {}}) as mock_login:
        auth_service.refresh_tokens()
    assert mock_login.call_args[0][2] == pyotp.TOTP(secret).now()


# ---------- CLI ----------

_FAKE_CREDS = {'user': {'email': 'u@x.com', 'uuid': 'uid', 'rootFolderId': 'rf'}, 'token': 'tok'}


def test_login_save_tfa_secret_stores_it(monkeypatch):
    pyotp = pytest.importorskip('pyotp')
    secret = pyotp.random_base32()
    monkeypatch.delenv('INTERNXT_TFA_SECRET', raising=False)
    with patch('cli.auth_service.is_2fa_needed', return_value=True), \
         patch('cli.auth_service.login', return_value=_FAKE_CREDS), \
         patch('cli.config_service.save_tfa_secret') as mock_save:
        result = CliRunner().invoke(cli, [
            'login', '--non-interactive', '--email', 'u@x.com', '--password', 'pw',
            '--tfa-secret', secret, '--save-tfa-secret'])
    assert result.exit_code == 0, result.output
    mock_save.assert_called_once_with('u@x.com', secret)


def test_login_uses_saved_tfa_secret(monkeypatch):
    pyotp = pytest.importorskip('pyotp')
    secret = pyotp.random_base32()
    monkeypatch.delenv('INTERNXT_TFA_SECRET', raising=False)
    with patch('cli.auth_service.is_2fa_needed', return_value=True), \
         patch('cli.config_service.get_tfa_secret', return_value=secret), \
         patch('cli.auth_service.login', return_value=_FAKE_CREDS) as mock_login:
        result = CliRunner().invoke(cli, [
            'login', '--non-interactive', '--email', 'u@x.com', '--password', 'pw'])
    assert result.exit_code == 0, result.output
    assert mock_login.call_args[0][2] == pyotp.TOTP(secret).now()


def test_login_save_tfa_secret_requires_secret(monkeypatch):
    monkeypatch.delenv('INTERNXT_TFA_SECRET', raising=False)
    with patch('cli.auth_service.is_2fa_needed', return_value=True):
        result = CliRunner().invoke(cli, [
            'login', '--non-interactive', '--email', 'u@x.com', '--password', 'pw',
            '--save-tfa-secret'])
    assert result.exit_code == 1
    assert '--save-tfa-secret needs --tfa-secret' in result.output


def test_config_set_and_unset_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr('cli.config_service.settings_file', tmp_path / 'settings.json')
    monkeypatch.setattr('cli.config_service.internxt_cli_data_dir', tmp_path)
    monkeypatch.setattr('cli.sys.platform', 'linux')  # skip the `security` probe
    runner = CliRunner()
    result = runner.invoke(cli, ['config', '--set-keychain', 'work'])
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / 'settings.json').read_text()) == {'keychain': 'work'}
    result = runner.invoke(cli, ['config', '--unset-keychain'])
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / 'settings.json').read_text()) == {}
