"""The `logout` command's exit code (issue #9).

A failed logout must exit non-zero so a scripted `login && … | rcat … && logout`
pipeline can trap it.
"""
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_logout_success_exits_zero(runner):
    with patch('cli.auth_service.logout'):
        result = runner.invoke(cli, ['logout'])
    assert result.exit_code == 0
    assert 'Successfully logged out' in result.output


def test_logout_failure_exits_nonzero(runner):
    with patch('cli.auth_service.logout', side_effect=Exception("disk error")):
        result = runner.invoke(cli, ['logout'])
    assert result.exit_code == 1
    assert 'Error during logout' in result.output
