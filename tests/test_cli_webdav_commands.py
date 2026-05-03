"""CLI tests for WebDAV server commands and the `config` info command."""
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


# ---------- webdav-stop ----------

def test_webdav_stop_success(runner):
    with patch('cli.webdav_server.stop',
               return_value={'success': True, 'message': 'Stopped'}):
        result = runner.invoke(cli, ['webdav-stop'])
    assert result.exit_code == 0, result.output
    assert 'Stopped' in result.output


def test_webdav_stop_failure_exits_nonzero(runner):
    with patch('cli.webdav_server.stop',
               return_value={'success': False, 'message': 'Not running'}):
        result = runner.invoke(cli, ['webdav-stop'])
    assert result.exit_code == 1
    assert 'Not running' in result.output


def test_webdav_stop_swallowed_exception_returns_error(runner):
    with patch('cli.webdav_server.stop', side_effect=RuntimeError("kaboom")):
        result = runner.invoke(cli, ['webdav-stop'])
    assert result.exit_code == 1
    assert 'kaboom' in result.output


# ---------- webdav-status ----------

def test_webdav_status_running(runner):
    fake = {
        'running': True, 'url': 'http://127.0.0.1:8080/',
        'protocol': 'http', 'port': 8080, 'host': '127.0.0.1',
        'server': 'waitress',
    }
    with patch('cli.webdav_server.status', return_value=fake):
        result = runner.invoke(cli, ['webdav-status'])
    assert result.exit_code == 0, result.output
    assert 'running' in result.output.lower()
    assert 'http://127.0.0.1:8080/' in result.output
    assert 'internxt-webdav' in result.output  # password reminder


def test_webdav_status_not_running(runner):
    with patch('cli.webdav_server.status',
               return_value={'running': False, 'message': 'not running'}):
        result = runner.invoke(cli, ['webdav-status'])
    assert result.exit_code == 0
    assert 'not running' in result.output.lower()
    assert 'webdav-start' in result.output  # next-step hint


# ---------- webdav-mount ----------

def test_webdav_mount_requires_running_server(runner):
    with patch('cli.webdav_server.status',
               return_value={'running': False}):
        result = runner.invoke(cli, ['webdav-mount'])
    assert result.exit_code == 1


def test_webdav_mount_shows_all_platforms(runner):
    fake_status = {'running': True, 'url': 'http://127.0.0.1:8080/'}
    fake_inst = {
        'macos': 'macOS Finder steps...',
        'windows': 'Windows File Explorer...',
        'linux': 'davfs2 instructions...',
    }
    with patch('cli.webdav_server.status', return_value=fake_status), \
         patch('cli.webdav_server.get_mount_instructions', return_value=fake_inst):
        result = runner.invoke(cli, ['webdav-mount'])
    assert result.exit_code == 0, result.output
    for platform in ('MACOS', 'WINDOWS', 'LINUX'):
        assert platform in result.output


# ---------- config command ----------

def test_config_command_renders_endpoints_and_paths(runner):
    """The `config` command must run end-to-end and print the active endpoints
    even if some keys (like DRIVE_WEB_URL) aren't configured.

    Regression: this command used to crash on `DRIVE_WEB_URL` being absent."""
    result = runner.invoke(cli, ['config'])
    assert result.exit_code == 0, result.output
    assert 'Drive API' in result.output
    assert 'Network API' in result.output
    # Should print path info
    assert 'Config Dir' in result.output
    # Should print WebDAV section
    assert 'WebDAV' in result.output


def test_config_handles_missing_drive_web_url_gracefully(runner):
    """DRIVE_WEB_URL has been commented out for a while — the config command
    must NOT crash, just label it as not configured."""
    result = runner.invoke(cli, ['config'])
    assert result.exit_code == 0
    # Either rendered with a value, or labeled "(not configured)"
    assert 'Drive Web' in result.output


# ---------- help_extended (verify it doesn't crash) ----------

def test_help_extended_runs(runner):
    result = runner.invoke(cli, ['help-extended'])
    assert result.exit_code == 0
    assert 'AUTHENTICATION' in result.output


# ---------- top-level CLI version flag ----------

def test_root_version_flag(runner):
    result = runner.invoke(cli, ['--version'])
    assert result.exit_code == 0
    # Click's default version output includes a digit somewhere
    assert any(ch.isdigit() for ch in result.output)
