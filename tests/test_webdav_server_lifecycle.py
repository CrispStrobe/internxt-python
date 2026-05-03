"""Tests for WebDAVServer lifecycle methods: stop, status, mount instructions."""
from unittest.mock import MagicMock, patch

import pytest

from services.webdav_server import WebDAVServer
from config.config import config_service


@pytest.fixture
def server():
    s = WebDAVServer()
    # Avoid leaving real signal handlers behind
    s._setup_signal_handlers = lambda: None
    return s


# ---------- stop() ----------

def test_stop_returns_message_when_not_running_and_no_pid(server):
    with patch.object(config_service, 'read_webdav_pid', return_value=None):
        server.is_running = False
        result = server.stop()
    assert result['success'] is False
    assert 'not running' in result['message'].lower()


def test_stop_kills_background_process_when_psutil_available(server):
    fake_proc = MagicMock()

    def fake_clear():
        pass

    with patch.object(config_service, 'read_webdav_pid', return_value=12345), \
         patch.object(config_service, 'clear_webdav_pid', side_effect=fake_clear) as mock_clear, \
         patch.dict('sys.modules', {'psutil': MagicMock(Process=lambda pid: fake_proc)}):
        result = server.stop()
    assert result['success'] is True
    fake_proc.terminate.assert_called_once()
    fake_proc.wait.assert_called_once_with(timeout=5)
    mock_clear.assert_called()


def test_stop_handles_missing_psutil(server):
    """No psutil installed → graceful failure with manual kill instruction."""
    with patch.object(config_service, 'read_webdav_pid', return_value=12345), \
         patch.dict('sys.modules', {'psutil': None}):
        # Force ImportError on `import psutil`
        import sys
        sys.modules.pop('psutil', None)
        sys.modules['psutil'] = None
        try:
            result = server.stop()
        finally:
            sys.modules.pop('psutil', None)
    assert result['success'] is False
    assert '12345' in result['message']  # pid mentioned in manual-kill hint


def test_stop_clears_stale_pid_when_process_already_dead(server):
    """psutil.Process raises → clear the stale pid file."""
    bad_psutil = MagicMock()
    bad_psutil.Process.side_effect = RuntimeError("no such process")

    with patch.object(config_service, 'read_webdav_pid', return_value=99999), \
         patch.object(config_service, 'clear_webdav_pid') as mock_clear, \
         patch.dict('sys.modules', {'psutil': bad_psutil}):
        result = server.stop()
    assert result['success'] is False
    mock_clear.assert_called()


# ---------- status() ----------

def test_status_returns_not_running_when_no_pid_and_not_active(server):
    with patch.object(config_service, 'read_webdav_pid', return_value=None):
        server.is_running = False
        status = server.status()
    assert status['running'] is False
    assert 'not running' in status['message'].lower()


def test_status_returns_foreground_when_is_running_set(server):
    with patch.object(config_service, 'read_webdav_pid', return_value=None):
        server.is_running = True
        status = server.status()
    assert status['running'] is True
    assert status['mode'] == 'foreground'
    assert status['port'] == server.config['port']
    assert status['url'].startswith('http://')


def test_status_reports_background_when_pid_alive(server):
    fake_proc = MagicMock()
    fake_proc.is_running.return_value = True
    fake_psutil = MagicMock(Process=lambda pid: fake_proc)

    with patch.object(config_service, 'read_webdav_pid', return_value=42), \
         patch.dict('sys.modules', {'psutil': fake_psutil}):
        status = server.status()
    assert status['running'] is True
    assert status['mode'] == 'background'
    assert status['pid'] == 42


def test_status_clears_stale_pid_when_process_gone(server):
    """psutil.Process(pid) for a dead pid raises — status must clear and report not running."""
    bad_psutil = MagicMock()
    bad_psutil.Process.side_effect = RuntimeError("dead")

    with patch.object(config_service, 'read_webdav_pid', return_value=99999), \
         patch.object(config_service, 'clear_webdav_pid') as mock_clear, \
         patch.dict('sys.modules', {'psutil': bad_psutil}):
        server.is_running = False
        status = server.status()
    assert status['running'] is False
    mock_clear.assert_called()


def test_status_without_psutil_assumes_running_if_pid_present(server):
    """Conservative fallback when psutil isn't available: trust the PID file."""
    with patch.object(config_service, 'read_webdav_pid', return_value=42):
        import sys
        old = sys.modules.pop('psutil', None)
        sys.modules['psutil'] = None
        try:
            status = server.status()
        finally:
            sys.modules.pop('psutil', None)
            if old is not None:
                sys.modules['psutil'] = old
    assert status['running'] is True
    assert status['pid'] == 42
    assert 'unverified' in status['mode']


# ---------- _get_server_url ----------

def test_get_server_url_uses_configured_host_and_port(server):
    server.config['host'] = '127.0.0.1'
    server.config['port'] = 4242
    assert server._get_server_url() == 'http://127.0.0.1:4242/'


# ---------- get_mount_instructions ----------

def test_mount_instructions_include_all_platforms(server):
    inst = server.get_mount_instructions()
    assert 'macos' in inst and 'windows' in inst and 'linux' in inst


def test_mount_instructions_embed_server_url(server):
    server.config['host'] = '127.0.0.1'
    server.config['port'] = 8080
    inst = server.get_mount_instructions()
    url = 'http://127.0.0.1:8080/'
    for platform_text in inst.values():
        assert url in platform_text


def test_mount_instructions_mention_credentials(server):
    inst = server.get_mount_instructions()
    for platform_text in inst.values():
        assert 'internxt' in platform_text  # username
        assert 'internxt-webdav' in platform_text  # password


# ---------- _cleanup_on_exit ----------

def test_cleanup_on_exit_clears_pid_when_running(server):
    server.is_running = True
    with patch.object(config_service, 'clear_webdav_pid') as mock_clear:
        server._cleanup_on_exit()
    mock_clear.assert_called_once()
    assert server.is_running is False


def test_cleanup_on_exit_noop_when_not_running(server):
    server.is_running = False
    with patch.object(config_service, 'clear_webdav_pid') as mock_clear:
        server._cleanup_on_exit()
    mock_clear.assert_not_called()


# ---------- _check_port_available / _find_available_port ----------

def test_find_available_port_returns_first_free(server):
    # Patch _check_port_available to claim first three ports busy.
    seq = iter([False, False, False, True])
    with patch.object(server, '_check_port_available', side_effect=lambda p: next(seq)):
        port = server._find_available_port(8000)
    assert port == 8003


def test_find_available_port_raises_when_none_free(server):
    with patch.object(server, '_check_port_available', return_value=False):
        with pytest.raises(RuntimeError, match="No available ports"):
            server._find_available_port(8000)
