"""Login credential-input paths: env vars and --password-stdin (no network)."""
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


_FAKE_LOGIN = {'user': {'email': 'u@example.com', 'uuid': 'uid', 'rootFolderId': 'root'}}


def test_login_reads_email_and_password_from_env(runner, monkeypatch):
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    monkeypatch.setenv('INTERNXT_PASSWORD', 'envpass')
    with patch('cli.auth_service.is_2fa_needed', return_value=False), \
         patch('cli.auth_service.login', return_value=_FAKE_LOGIN) as m:
        result = runner.invoke(cli, ['login'])
    assert result.exit_code == 0, result.output
    m.assert_called_once()
    email, password = m.call_args[0][0], m.call_args[0][1]
    assert email == 'u@example.com'
    assert password == 'envpass'


def test_login_password_stdin(runner, monkeypatch):
    monkeypatch.setenv('INTERNXT_EMAIL', 'u@example.com')
    with patch('cli.auth_service.is_2fa_needed', return_value=False), \
         patch('cli.auth_service.login', return_value=_FAKE_LOGIN) as m:
        result = runner.invoke(cli, ['login', '--password-stdin'], input='s3cr3t\n')
    assert result.exit_code == 0, result.output
    assert m.call_args[0][1] == 's3cr3t'   # newline stripped, not from argv


def test_login_rejects_both_password_and_stdin(runner):
    with patch('cli.auth_service.login') as m:
        result = runner.invoke(
            cli, ['login', '-e', 'u@example.com', '-p', 'pw', '--password-stdin'],
            input='x\n')
    assert result.exit_code == 1
    m.assert_not_called()
    assert 'not both' in result.output.lower()
