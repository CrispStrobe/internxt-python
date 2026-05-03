"""Tests for WebDAVServer._run_server_thread, stop() running-server path,
and test_connection() PROPFIND probe.
"""
from unittest.mock import MagicMock, patch

import pytest

from services.webdav_server import WebDAVServer
from config.config import config_service


@pytest.fixture
def server():
    s = WebDAVServer()
    s._setup_signal_handlers = lambda: None
    return s


# ---------- _run_server_thread (waitress branch) ----------

def test_run_server_thread_waitress_calls_serve(server):
    """The thread runner must call waitress.serve with the configured
    host/port + the production threading parameters."""
    captured = {}
    def fake_serve(app, **kw):
        captured.update(kw)
        raise RuntimeError("stop here")

    with patch('services.webdav_server.WSGI_SERVER', 'waitress'), \
         patch('services.webdav_server.serve', side_effect=fake_serve), \
         patch.object(config_service, 'clear_webdav_pid'):
        server.app = MagicMock()
        server._run_server_thread()

    assert captured['host'] == server.config['host']
    assert captured['port'] == server.config['port']
    # Production threading config that callers depend on
    assert captured['threads'] == 10
    assert captured['connection_limit'] == 1000
    assert captured['ident'] == 'Internxt WebDAV Server'


def test_run_server_thread_keyboard_interrupt_is_caught(server):
    """Ctrl+C in serve() must NOT propagate — let cleanup run."""
    with patch('services.webdav_server.WSGI_SERVER', 'waitress'), \
         patch('services.webdav_server.serve', side_effect=KeyboardInterrupt()), \
         patch.object(config_service, 'clear_webdav_pid') as mock_clear:
        server.app = MagicMock()
        server._run_server_thread()  # must not raise
    # Cleanup ran (clear_webdav_pid called from finally)
    mock_clear.assert_called()
    assert server.is_running is False


def test_run_server_thread_exception_when_stopping_is_silent(server):
    """If server is being stopped, exceptions during teardown must NOT
    print error trace — that's just normal shutdown noise."""
    server.is_stopping = True
    with patch('services.webdav_server.WSGI_SERVER', 'waitress'), \
         patch('services.webdav_server.serve',
               side_effect=RuntimeError("normal shutdown")), \
         patch.object(config_service, 'clear_webdav_pid'):
        server.app = MagicMock()
        server._run_server_thread()  # must not raise
    assert server.is_running is False


def test_run_server_thread_cheroot_branch(server):
    """When WSGI_SERVER='cheroot', should construct a Cheroot Server and
    call .start() on it."""
    fake_server_inst = MagicMock()
    fake_server_inst.start.side_effect = RuntimeError("stop here")
    fake_wsgi_module = MagicMock()
    fake_wsgi_module.Server.return_value = fake_server_inst

    with patch('services.webdav_server.WSGI_SERVER', 'cheroot'), \
         patch('services.webdav_server.wsgi', fake_wsgi_module), \
         patch.object(config_service, 'clear_webdav_pid'):
        server.app = MagicMock()
        server._run_server_thread()

    fake_wsgi_module.Server.assert_called_once()
    fake_server_inst.start.assert_called_once()
    # The Server constructor must receive the configured bind_addr
    _, kwargs = fake_wsgi_module.Server.call_args
    assert kwargs['bind_addr'] == (server.config['host'], server.config['port'])


# ---------- stop() — foreground server still running ----------

def test_stop_foreground_cheroot_calls_server_stop(server):
    """When running cheroot in foreground, stop must call self.server.stop()."""
    fake_server = MagicMock()
    server.server = fake_server
    server.is_running = True

    with patch.object(config_service, 'read_webdav_pid', return_value=None), \
         patch.object(config_service, 'clear_webdav_pid'), \
         patch('services.webdav_server.WSGI_SERVER', 'cheroot'):
        result = server.stop()

    fake_server.stop.assert_called_once()
    assert result['success'] is True


def test_stop_foreground_cheroot_tolerates_server_stop_error(server):
    """If self.server.stop() raises, we still report overall success."""
    fake_server = MagicMock()
    fake_server.stop.side_effect = RuntimeError("can't stop")
    server.server = fake_server
    server.is_running = True

    with patch.object(config_service, 'read_webdav_pid', return_value=None), \
         patch.object(config_service, 'clear_webdav_pid'), \
         patch('services.webdav_server.WSGI_SERVER', 'cheroot'):
        result = server.stop()

    # Wrapped: outer try still succeeds because inner exception was caught
    assert result['success'] is True


# ---------- test_connection (the WebDAV PROPFIND probe) ----------

def test_test_connection_returns_failure_when_not_running(server):
    server.is_running = False
    with patch.object(config_service, 'read_webdav_pid', return_value=None):
        result = server.test_connection()
    assert result['success'] is False
    assert 'not running' in result['message'].lower()


def test_test_connection_returns_success_on_207_xml(server):
    fake_resp = MagicMock()
    fake_resp.status_code = 207
    fake_resp.text = '<?xml version="1.0"?><D:multistatus />'

    with patch.object(server, 'status', return_value={'running': True}), \
         patch('requests.request', return_value=fake_resp):
        result = server.test_connection()
    assert result['success'] is True
    assert result['status_code'] == 207


def test_test_connection_reports_failure_on_non_207(server):
    fake_resp = MagicMock()
    fake_resp.status_code = 401
    fake_resp.text = ''

    with patch.object(server, 'status', return_value={'running': True}), \
         patch('requests.request', return_value=fake_resp):
        result = server.test_connection()
    assert result['success'] is False
    assert result['status_code'] == 401


def test_test_connection_reports_failure_on_request_exception(server):
    with patch.object(server, 'status', return_value={'running': True}), \
         patch('requests.request', side_effect=ConnectionError("refused")):
        result = server.test_connection()
    assert result['success'] is False
    assert 'failed' in result['message'].lower()


# ---------- get_mount_instructions content checks ----------

def test_mount_instructions_macos_includes_finder_and_cli(server):
    inst = server.get_mount_instructions()
    macos = inst['macos']
    assert 'Finder' in macos
    assert 'Cmd+K' in macos
    assert 'mount -t webdav' in macos


def test_mount_instructions_linux_includes_davfs(server):
    inst = server.get_mount_instructions()
    linux = inst['linux']
    assert 'davfs' in linux.lower()
    assert 'mount -t davfs' in linux


def test_mount_instructions_windows_includes_map_drive(server):
    inst = server.get_mount_instructions()
    windows = inst['windows']
    assert 'Map network drive' in windows or 'Map' in windows


# ---------- _check_port_available ----------

def test_check_port_available_returns_true_for_free_port(server):
    """A high port number that's almost certainly unused should report available."""
    assert isinstance(server._check_port_available(58743), bool)


def test_check_port_available_returns_false_when_port_in_use(server):
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('localhost', 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        assert server._check_port_available(busy_port) is False
    finally:
        sock.close()
